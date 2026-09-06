#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务层：把查询核心逻辑提炼为可复用函数。

插件（plugin.py）与本地实验脚本共用本模块，
保证独立运行与插件运行行为一致。

缓存目录参数化：插件运行时用 MaiBot 分配的 runtime_dir，
CLI 运行时用 data/.cache。
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Dict, Optional

from cache import CacheManager
from dict_lookup import DictLookup
from fetcher_google_doc import GoogleDocFetcher
from fetcher_stelladb import StelladbFetcher
import term_replace as _term_replace_module
from text_clean import detect_element, strip_game_markup

logger = logging.getLogger("stellasora.service")

ELEMENT_SECTIONS = {"Aqua", "Ignis", "Ventus", "Terra", "Lux", "Umbra"}
ELEMENT_CN = {
    "Aqua": "水", "Ignis": "火", "Ventus": "风",
    "Terra": "地", "Lux": "光", "Umbra": "暗",
}

# 数据目录模块级常量：dict.json/names.json 等数据文件的唯一归属地
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# 模块级单例（按数据目录缓存，避免每次调用重载 8.8MB 字典）；
# 值形状 = (lookup, last_cache_dir, st_fetcher, gd_fetcher, replacer)：
# cache_dir 变化时仅重建两个 fetcher，lookup 与 replacer 全进程复用
_instances: Dict[str, tuple] = {}
# check-then-init 竞态保护：Fix D 之后这些函数跑在线程池里，无锁并发首调
# 会双份解析 8.8MB 字典（【Metis 修订 #11】）
_init_lock = threading.Lock()
_pending_aliases: Dict[str, str] = {}


def configure_overrides(
    aliases: Optional[Dict[str, str]] = None,
) -> None:
    """配置运行时的中文别名映射（由 plugin.py 或配置加载时调用）。

    仅作用于 lookup_term 查询解析阶段：用户在群里用简称/俗称提问时，
    自动映射到官方中文名再查攻略。
    """
    global _pending_aliases
    with _init_lock:
        if aliases is not None:
            _pending_aliases = dict(aliases)

        key = str(_DATA_DIR)
        if key in _instances:
            lookup, _last, _st, _gd, _replacer = _instances[key]
            if aliases is not None:
                lookup.set_custom_aliases(_pending_aliases)


def _get_services(cache_dir: Path) -> tuple:
    """获取/构建共享服务元组 (lookup, last_cache_dir, st_fetcher, gd_fetcher, replacer)。

    锁内 check-then-init：lookup 只建一次并显式预热，replacer 基于已加载字典预建；
    cache_dir 与上次不同时仅重建 fetcher（lookup/replacer 复用）。
    """
    key = str(_DATA_DIR)
    with _init_lock:
        if key not in _instances:
            lookup = DictLookup(_DATA_DIR, custom_aliases=_pending_aliases)
            # 先显式完成字典加载，再基于 _main_dict 构建替换器——不依赖
            # "构建 TermReplacer 隐含触发 _load" 的顺序假设，避免 preloaded_dict
            # 传到 None（【双审 SH-6】）。锁释放前 _main_dict/_name_index/
            # _lowercase_index/_character_names_cache 均已就绪，后续线程读取无竞态
            # （【双审 SH-6】）
            lookup._load()
            # 单一事实源注入：service 持有的 replacer 是全进程唯一实例，
            # 避免 term_replace 模块级懒加载再自行 parse 一份 8.8MB 字典
            # （【Metis 修订 #11】）
            replacer = _term_replace_module.TermReplacer(
                _DATA_DIR,
                preloaded_dict=lookup._main_dict,
            )
            _term_replace_module._replacer = replacer
            _instances[key] = (
                lookup,
                cache_dir,
                StelladbFetcher(cache_dir),
                GoogleDocFetcher(cache_dir),
                replacer,
            )
        else:
            lookup, last_cache_dir, _st, _gd, replacer = _instances[key]
            if cache_dir != last_cache_dir:
                # 仅重建 fetcher（各自持有缓存目录），lookup/replacer 全进程复用
                _instances[key] = (
                    lookup,
                    cache_dir,
                    StelladbFetcher(cache_dir),
                    GoogleDocFetcher(cache_dir),
                    replacer,
                )
    return _instances[key]


def _get_lookup() -> DictLookup:
    """只取共享 DictLookup（无 fetcher/缓存目录概念）：查词类纯离线路径专用。

    已初始化时直接返回缓存实例（【Metis 修订 #9】彻底删除查词路径的
    data/.cache 传参）；仅首次调用时以模块 data/.cache 作为 fetcher 缓存
    目录委托 _get_services 构建元组（fetcher 仅攻略查询路径实际使用）。
    """
    entry = _instances.get(str(_DATA_DIR))
    if entry is None:
        _get_services(_DATA_DIR / ".cache")
        entry = _instances[str(_DATA_DIR)]
    return entry[0]


def lookup_term(term: str, custom_aliases: Optional[Dict[str, str]] = None) -> Dict:
    """查词工具核心：术语 → {id, en, cn, cat} 或 {"not_found": True}。"""
    res = _get_lookup().lookup_term(term, custom_aliases=custom_aliases)
    return res if res else {"not_found": True}


def lookup_full(item_id: str) -> Dict:
    """全量字典按 ID 查询完整文本（含描述/效果/剧情）。

    与 lookup_term 的区别：lookup_term 按名字查（返回 .1 名字条目），
    lookup_full 按 ID 查全量字典（.1 + .2/.3 描述/效果/剧情文本）。
    """
    res = _get_lookup().get_full(item_id)
    return res if res else {"not_found": True}


def count_character_names(text: str) -> int:
    """统计 text 中出现的不同角色名数量（联合查询检测）。

    命中 ≥2 个不同角色名 → 多角色联合查询 → 应回传 planner 汇总后
    单条回复，避免逐角色直发刷屏。
    """
    if not text:
        return 0
    return sum(1 for name in _get_lookup().get_character_names() if name in text)


def _fit_lines(lines: list, max_length: Optional[int]) -> str:
    """把行列表拼成文本，超长时按行边界截断并标注（绝不切在行中间）。

    how 路径的 material = trekker 页 + infodoc 全文，可达 40K+ 字符，
    旧的字符串硬切片会把最后一行切成残句。
    """
    text = "\n".join(lines)
    if max_length is None or len(text) <= max_length:
        return text
    kept: list = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > max_length:
            break
        kept.append(line)
        total += len(line) + 1
    kept.append("……")
    kept.append("[资料因长度限制被截断，需要后半部分请缩小问题范围或分段询问]")
    return "\n".join(kept)


def query_what(term: str, cache_dir: Path, max_length: Optional[int] = None) -> str:
    """what 桶：角色/物品"是什么"，输出已中文化的攻略文本。"""
    lookup, _last, st_fetcher, _gd, replacer = _get_services(cache_dir)
    res = lookup.lookup_term(term)
    if not res:
        return f"[{term}] 未在字典中找到。请检查拼写，或使用查词工具确认。"

    lines = [
        "=== 字典匹配 ===",
        f"  中文: {res['cn']}",
        f"  英文: {res['en']}",
        f"  ID:   {res['id']}",
        f"  类别: {res['cat']}",
        "",
    ]
    cat = res["cat"]
    if cat == "Character":
        num_id = res["id"].split(".")[1]
        lines.append(f"=== 角色攻略 (stelladb /trekker/{num_id}) ===")
        lines.append(strip_game_markup(replacer.replace(st_fetcher.fetch_trekker(num_id))))
    else:
        lines.append(f"[{term}] 是 {res['cat']} 类词条（{res['en']} / {res['cn']}），没有专属攻略页。")
    return _fit_lines(lines, max_length)


# 详细页 HTML 表格的行号列折叠出的纯数字行（如 "19"、"125"），对 LLM 无意义
_ROW_NUM_LINE_RE = re.compile(r"^\d{1,3}$")

# 区块锚点行内的导航片段：'⏏ Back to Top ⏏'（含前后空格），行内队名保留
_TOP_ANCHOR_RE = re.compile(r"\s*⏏\s*Back to Top\s*⏏\s*", re.IGNORECASE)

# 索引页行内导航段（队名行中夹带的翻页按钮，不属于任何元素队伍）
_NAV_CELLS = {"<< Prev", "Next >>"}

# Potentials 标签行：'Priority Potentials ...' / 'Optional Potentials ...' 开头的行。
# 该标签行只造成 LLM 把后续 "+3 levels" 数据行整理成"优先潜能/可选潜能"章节
# （实例副作用），直接删除标签行；数据行保留（秘纹/纹章/潜能在同区块内按行
# 混排、无法按行区分，由知识库规则约束 LLM 不单独整理潜能章节）
_POTENTIAL_LINE_RE = re.compile(r"^(?:Priority|Optional) Potentials\b", re.IGNORECASE)


def strip_infodoc_noise(text: str) -> str:
    """清理 infodoc 文本中的表格行号碎片、Potentials 标签行与多余空行（token 精简）。"""
    if not text:
        return text
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        if _ROW_NUM_LINE_RE.match(stripped):
            continue
        if _POTENTIAL_LINE_RE.match(stripped):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _split_cells(line: str) -> list:
    return [c.strip() for c in line.split(" | ") if c.strip()]


def _collect_members(lines_slice: list, name_res: list, seen: set, members: list) -> None:
    """从若干行中按出现顺序收集未收录的已知角色名（就地追加到 members）。"""
    for line in lines_slice:
        for c in _split_cells(line):
            for name, name_re in name_res:
                if name not in seen and name_re.match(c):
                    seen.add(name)
                    members.append(name)


def extract_team_blocks(infodoc_text: str, character_en: str, all_character_names: list) -> list:
    """从详细页收集**所有包含问询角色的队伍区块**。

    详细页为单元素纵向布局，每个队伍区块结构（实测）：
        <队伍名角色> (<build>) | ⏏ Back to Top ⏏   ← 区块锚点行（队名 ≠ 主控！）
        <主控角色> (<星级>) | 技能优先度            ← 区块体首个角色详情段 = 主控位
        ...主控 build（描述/秘纹/纹章）...
        <支援角色1> (<星级>)                       ← 区块体后续角色 = 支援位
        <下一队> (...) | ⏏ Back to Top ⏏           ← 下一队伍区块开始

    槽位判定规则：**区块体（锚点行之后）内第一个角色详情段 = 主控位，后续角色均为
    支援位**；锚点行队名角色不参与成员提取（队伍命名可 ≠ 主控，如暗队
    Otoha (Laser) 的主控是 Cosette）。

    问询角色可能出现在**多个队伍**（如珂赛特既是 Otoha (Laser) 队主控，又是
    花铃/翡冷翠等队的支援）：她作为锚点行队名 → 该区块收集（asker_role=main）；
    她作为区块体成员 → 其所属区块也收集（asker_role=support）。

    返回区块信息列表 [{name, text, members, asker_role}, ...]，按页面出现顺序
    排列；未命中时返回 []。
    """
    if not infodoc_text:
        return []

    char_re = re.compile(r"^" + re.escape(character_en) + r"(\s|\(|$)")
    en_names = [n for n in all_character_names if isinstance(n, str) and n.isascii() and len(n) >= 2]
    name_res = [(n, re.compile(r"^" + re.escape(n) + r"(\s|\(|$)")) for n in en_names]

    lines = infodoc_text.split("\n")

    # 1. 收集全部锚点行及其队名（剥离 ⏏ 后的首个非空单元格）
    anchors: list = []  # (行号, 队名)
    for li, line in enumerate(lines):
        if "⏏" in line or "Back to Top" in line:
            cleaned = _TOP_ANCHOR_RE.sub("", line).rstrip(" |").strip()
            cells = _split_cells(cleaned)
            if not cells:
                continue  # 页尾 "⏏ BACK TO TOP ⏏" 等纯导航行
            anchors.append((li, cells[0]))
    if not anchors:
        return []

    # 2. 逐区块确定边界与成员；收集包含问询角色的区块
    results: list = []
    for idx, (start, team_name) in enumerate(anchors):
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(lines)

        # 区块体 = 锚点行之后到下一锚点行之前（排除其他锚点行）
        body_lines = [
            _TOP_ANCHOR_RE.sub("", lines[li]).rstrip(" |").strip()
            for li in range(start + 1, end)
            if not ("⏏" in lines[li] or "Back to Top" in lines[li])
        ]
        block_lines = [team_name] + [bl for bl in body_lines if bl]

        members: list = []
        seen: set = set()
        _collect_members(block_lines[1:], name_res, seen, members)

        # 收集条件：问询角色是队名角色 或 区块体成员
        if char_re.match(team_name) or character_en in seen:
            block = re.sub(r"\n{3,}", "\n\n", "\n".join(block_lines)).strip()
            # asker_role：区块体首个角色详情段 = 主控位；问询角色排首位则主控
            asker_role = "main" if members and members[0] == character_en else "support"
            results.append({
                "name": team_name,
                "text": block,
                "members": list(members),
                "asker_role": asker_role,
            })

    return results


def _extract_member_segment(block_lines: list, char_re) -> list:
    """从队伍区块中截取**问询角色的详情子段**（支援位问询的资料精简）。

    角色详情段 = 该角色的 'X (星级)' 行（或其 Description 段头行）起，
    到下一个 Description 段头行 / 区块尾。未定位到角色行时返回原区块（回退）。
    """
    char_idx = -1
    for i, line in enumerate(block_lines):
        cells = _split_cells(line)
        if any(char_re.match(c) for c in cells):
            char_idx = i
            break
    if char_idx < 0:
        return block_lines
    seg_start = 0
    for i in range(char_idx, -1, -1):
        if block_lines[i].strip().lower().startswith("description"):
            seg_start = i
            break
    seg_end = len(block_lines)
    for i in range(char_idx + 1, len(block_lines)):
        if block_lines[i].strip().lower().startswith("description"):
            seg_end = i
            break
    return block_lines[seg_start:seg_end]


def extract_rotation(index_text: str, character_en: str) -> str:
    """从索引页提取目标角色所在队伍的 Rotation（输出手法）——索引页唯一用途。

    索引页按队伍区块分行（队名行 / Rotation 标签行 / Rotation 内容行 / 槽位行 / ...），
    行内各段按六元素顺序排列：
    - 队名行可能夹带导航段（"<< Prev" / "Next >>"），剔除后与 Rotation 行段序一致
    - 定位队名行中 character_en 的段序 i，取 Rotation 内容行的第 i 段

    段数不一致（空缺压缩错位）或未定位到锚点时返回空串，由调用方如实说明缺失。
    """
    if not index_text:
        return ""
    char_re = re.compile(r"^" + re.escape(character_en) + r"(\s|\(|$)", re.IGNORECASE)

    anchor_i = -1
    rotation_label_i = -1
    rotation_content = ""

    for li, line in enumerate(index_text.split("\n")):
        cells = _split_cells(line)
        if not cells:
            continue

        # 队名行：含 character_en 段 → 记录剔除导航段后的段序
        if anchor_i < 0 and any(char_re.match(c) for c in cells):
            kept = [c for c in cells if c not in _NAV_CELLS]
            for ci, c in enumerate(kept):
                if char_re.match(c):
                    anchor_i = ci
                    break
            continue

        # Rotation 标签行：多数非空段以 Rotation 开头
        if rotation_label_i < 0 and len(cells) >= 3 and sum(1 for c in cells if c.lower().startswith("rotation")) >= 3:
            rotation_label_i = li
            continue

        # Rotation 内容行：标签行之后的首个多段行
        if rotation_label_i >= 0 and li > rotation_label_i and len(cells) >= 3:
            rotation_content = line
            break

    if anchor_i < 0 or not rotation_content:
        return ""

    content_cells = _split_cells(rotation_content)
    if anchor_i >= len(content_cells):
        return ""
    return content_cells[anchor_i]


def _is_emblem_data_line(cells: list) -> bool:
    """判定一行是否为纹章词条数据行：'词条 | 数值' 交替。

    - 数值形态：数字/百分比/'+'N levels/'无需升级'/任意短词后跟数值等，
      以**首个值位**（第 2 格）匹配数值模式为准
    - 行首可带 'Affix Priority'/'词条优先级' 标签格（跳过后再判）
    - 单格长句（描述/Key Notes）返回 False
    """
    if len(cells) < 2:
        return False
    work = list(cells)
    if work[0].lower() in ("affix priority", "词条优先级"):
        work = work[1:]
    if len(work) < 2:
        return False
    value_re = re.compile(
        r"^[\d.]+\s*%$|^[\d.]+$|^\+\d+\s*levels?$|^无需升级$|^Not needed\.?$",
        re.IGNORECASE,
    )
    # 首个值位必须像数值；其后所有奇数位（值位）也须像数值或至少非常短
    if not value_re.match(work[1]):
        return False
    return all(
        value_re.match(work[i]) or len(work[i]) <= 12
        for i in range(3, len(work), 2)
    )


def _restructure_block(body_lines: list) -> list:
    """角色段结构化（token 精简 + 消除 LLM 分列歧义）：

    - Potentials 标签行删除（防 LLM 自组"优先潜能/可选潜能"章节）；
      其后纯潜能数据行（以 "+3 levels" 结尾的短行）也删除（用户明确无需抓取）
    - "Recommended Main Discs" 标签行 → "=== 推荐主位秘纹 ===" 锚
    - "Emblem" 标签行或 "Affix Priority" 数据首行 → "=== 纹章 ===" 锚；其后
      **数据形状**行（'词条 | 数值' 交替）转置重组为 70级/80级/90级 行
      （每行 = 该等级的全部词条+数值），消除"无列头表格被 LLM 自由分列"的歧义；
      描述/Key Notes 等非数据行原样保留，不参与转置
    """
    out: list = []
    mode = "normal"           # normal | disc | emblem | pot
    emblem_rows: list = []    # 每元素 = 该行剥离标签格后的单元格列表

    def _flush_emblem() -> None:
        """纹章转置：行 = 优先序层，列 = 纹章等级（70/80/90）。"""
        if not emblem_rows:
            return
        grade_labels = ["70级", "80级", "90级"]
        max_pairs = max((len(r) + 1) // 2 for r in emblem_rows)
        for gi in range(max_pairs):
            label = grade_labels[gi] if gi < len(grade_labels) else f"第{gi + 1}档"
            items = []
            for r in emblem_rows:
                wi, vi = gi * 2, gi * 2 + 1
                if wi < len(r):
                    items.append(f"{r[wi]} {r[vi]}" if vi < len(r) else r[wi])
            if items:
                out.append(f"{label}：{'、'.join(items)}")
        emblem_rows.clear()

    for line in body_lines:
        low = line.lower()
        cells = _split_cells(line)

        # Potentials 纯标签行（无秘纹/纹章语义）→ 潜能数据模式（数据行丢弃）
        if re.match(r"^\s*(?:Priority|Optional) Potentials\b", line, re.IGNORECASE) and "disc" not in low and "emblem" not in low:
            _flush_emblem()
            mode = "pot"
            continue
        # 推荐主位秘纹锚
        if "recommended main discs" in low:
            _flush_emblem()
            mode = "disc"
            out.append("=== 推荐主位秘纹 ===")
            continue
        # 纹章锚：短标签行（"Optional Potentials | Emblem" 等），或 Affix Priority
        # 数据首行（部分区块无 Emblem 标签）。长叙述句中的 "emblem" 单词不触发。
        first_is_affix = cells and cells[0].lower() in ("affix priority", "词条优先级")
        is_emblem_label = "emblem" in low and len(line) <= 60
        if is_emblem_label or (first_is_affix and mode != "emblem"):
            _flush_emblem()
            mode = "emblem"
            if not out or not out[-1].startswith("=== 纹章 ==="):
                out.append("=== 纹章 ===")
            if first_is_affix:
                emblem_rows.append(cells[1:])  # 首行即数据：跳过标签格收录
            continue
        # 新角色子段/区块边界 → 冲刷并回到 normal
        if "skill upgrade priority" in low or "⏏" in line or "back to top" in low:
            _flush_emblem()
            mode = "normal"
            out.append(line)
            continue

        if mode == "emblem":
            # 只有数据形状的行才参与转置；描述/Key Notes 行原样输出并退出 emblem 模式
            if _is_emblem_data_line(cells):
                # 行首标签格（Affix Priority/词条优先级）跳过——否则会混入转置词条
                if cells[0].lower() in ("affix priority", "词条优先级"):
                    cells = cells[1:]
                emblem_rows.append(cells)
            else:
                _flush_emblem()
                mode = "normal"
                out.append(line)
            continue
        if mode == "pot":
            # 潜能数据行（'+3 levels' 结尾的短行）丢弃；叙述行恢复 normal
            if cells and all(c.endswith("levels") or re.match(r"^[\d.]+%$", c) for c in cells if c):
                continue
            mode = "normal"
            out.append(line)
            continue
        out.append(line)

    _flush_emblem()
    return out


def query_how(term: str, cache_dir: Path, with_presets: bool = False, max_length: Optional[int] = None) -> str:
    """how 桶：配队/纹章/秘纹/技能优先度（--presets 时附加预设码）。"""
    lookup, _last, st_fetcher, gd_fetcher, replacer = _get_services(cache_dir)
    res = lookup.lookup_term(term)
    if not res:
        return f"[{term}] 未在字典中找到。请检查拼写，或使用查词工具确认。"

    element: Optional[str] = None
    character_en = res["en"]
    character_cn = res["cn"]
    is_character = res["cat"] == "Character"

    if is_character:
        num_id = res["id"].split(".")[1]
        trekker_text = st_fetcher.fetch_trekker(num_id)
        element = detect_element(trekker_text)

    if not element:
        if res["en"] in ELEMENT_SECTIONS:
            element = res["en"]
        elif term in ELEMENT_SECTIONS:
            element = term

    lines: list[str] = []

    # 预设码区块放在攻略正文之前：它是用户明确要求的内容（--presets），
    # 且输出可能因长度上限被截断——放在前面保证不被截掉
    if with_presets:
        preset_lines: list[str] = ["=== 预设码推荐 (Google Docs) ==="]
        presets = gd_fetcher.fetch_presets()
        if "Error" in presets:
            preset_lines.append("  [预设码抓取失败]")
        elif is_character:
            block = extract_preset_block(presets, character_en)
            if block:
                preset_lines.append(strip_game_markup(replacer.replace(block)))
            elif element:
                section = extract_element_preset_section(presets, element)
                preset_lines.append(strip_game_markup(replacer.replace(section)) if section else f"  预设码文档中未找到 {character_en} 相关内容。")
            else:
                preset_lines.append(f"  预设码文档中未找到 {character_en} 相关内容。")
        elif element:
            section = extract_element_preset_section(presets, element)
            preset_lines.append(strip_game_markup(replacer.replace(section)) if section else f"  预设码文档中未找到 {element} 相关内容。")
        preset_lines.append("")
        lines += preset_lines

    if element:
        # 索引页唯一作用：提取输出手法（Rotation）——仅角色查询需要；
        # 槽位不回查（详细页区块体首个角色详情段即主控位，后续均为支援位）
        index_text = st_fetcher.fetch_infodoc_index() if is_character else ""

        lines.append(f"=== {ELEMENT_CN[element]}队文字攻略 (stelladb /infodoc/{element.lower()}) ===")
        infodoc_text = st_fetcher.fetch_infodoc(element.lower())
        if infodoc_text and "Error" not in infodoc_text:
            infodoc_text = strip_infodoc_noise(infodoc_text)
            if is_character:
                teams = extract_team_blocks(infodoc_text, character_en, lookup.get_character_names())
                if teams:
                    # 队伍槽位块：逐队伍列出主控/支援；成员集合相同的连续多 build
                    # 队伍合并为一行（队名并列），避免槽位重复刷屏
                    lines.append("=== 队伍槽位（按队伍） ===")
                    slot_groups: list = []  # [(队名列表, 成员列表)]
                    for team in teams:
                        member_display = []
                        for name in team["members"]:
                            member_res = lookup.lookup_term(name)
                            member_display.append(member_res["cn"] if member_res else name)
                        if slot_groups and slot_groups[-1][1] == member_display:
                            slot_groups[-1][0].append(team["name"])
                        else:
                            slot_groups.append(([team["name"]], member_display))
                    for names, member_display in slot_groups:
                        # 队名过字典替换（角色名+build 名均中文化，保留流派信息）
                        team_names = [strip_game_markup(replacer.replace(n)) for n in names]
                        slot_line = f"[{team_names[0] if len(team_names) == 1 else ' / '.join(team_names)}] 主控位：{member_display[0]}"
                        if len(member_display) > 1:
                            slot_line += "；支援位：" + "、".join(member_display[1:])
                        lines.append(slot_line)
                    lines.append("")

                    # Rotation 块：仅索引页提供；知识库规则约定默认不转述，
                    # 用户明确询问输出手法时由 LLM 取用
                    rotation = extract_rotation(index_text, character_en)
                    if rotation:
                        lines.append("=== 输出手法（Rotation，索引页） ===")
                        lines.append(strip_game_markup(replacer.replace(rotation)))
                        lines.append("")

                    # 各队伍正文：
                    # - 问询角色为主控位的区块 → 全文（她的 build 与配队完整资料）
                    # - 问询角色为支援位的区块 → 只发队名 + 她的详情段
                    #   （避免多队全量资料撑爆直发 LLM 的 300 字输出）
                    char_re = re.compile(r"^" + re.escape(character_en) + r"(\s|\(|$)")
                    for team in teams:
                        body_lines = team["text"].split("\n")
                        if team["asker_role"] == "support":
                            body_lines = _extract_member_segment(body_lines, char_re)
                        restructured = _restructure_block(body_lines)
                        lines.append(strip_game_markup(replacer.replace("\n".join(restructured))))
                        lines.append("")
                else:
                    # 区块未命中（如问询角色不在本元素页）：回退整页
                    lines.append(strip_game_markup(replacer.replace(infodoc_text)))
            else:
                lines.append(strip_game_markup(replacer.replace(infodoc_text)))
        else:
            lines.append("  [抓取失败]")
        lines.append("")

    if not lines:
        lines.append(f"[{term}] 是 {res['cat']} 类词条（{res['en']} / {res['cn']}），没有专属攻略页。")

    return _fit_lines(lines, max_length)


_PRESET_CODE_RE = re.compile(r"[A-Za-z0-9]{20,}")
_PRESET_LABEL_RE = re.compile(r"Trekker|Preset Code|Slot", re.IGNORECASE)


def extract_preset_block(presets_text: str, character_en: str) -> str:
    """按角色名提取预设码区块。

    预设码文档的实际结构（Google Sheet 空单元格压缩后）：
        角色名行（如 "Wraith (Melee)"）
        标签行（Main Trekker / ... / Preset Code）
        （空行——原表格占位格）
        预设码行（AAAA...）
        （空行）
        下一个角色名行 …

    旧实现按空行分块，导致"角色名+标签"与"预设码"被空行切成不同块，
    命中的块只有占位标签没有码（LLM 报"资料里只有占位栏位"）。
    现改为按角色名行分节：从角色名行收集到下一个角色名行/文档尾，
    跨越空行；整节不含真实预设码的占位节丢弃。
    """
    lines = presets_text.split("\n")
    element_titles = set(ELEMENT_SECTIONS)

    def _is_code_line(line: str) -> bool:
        return bool(_PRESET_CODE_RE.fullmatch(line.strip()))

    def _is_label_line(line: str) -> bool:
        return bool(_PRESET_LABEL_RE.search(line))

    def _is_name_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped or _is_code_line(stripped) or _is_label_line(stripped):
            return False
        return bool(re.search(r"[A-Za-z]", stripped))

    name_idxs = [
        i for i, line in enumerate(lines)
        if character_en in line and _is_name_line(line)
    ]
    if not name_idxs:
        return ""

    segs: list = []
    for start in name_idxs:
        seg = [lines[start]]
        has_code = False
        for j in range(start + 1, len(lines)):
            line = lines[j]
            if _is_name_line(line):
                break  # 下一个角色名行 = 本节结束
            seg.append(line)
            if _is_code_line(line):
                has_code = True
        # 元素标题行归入本节末尾即可终止（下一节从它开始也无妨，这里简化：
        # 元素标题行本身也是"名字行"，上面的 _is_name_line 已终止本节）
        if has_code:
            segs.append("\n".join(seg).strip())

    return "\n\n---\n\n".join(segs)


def extract_element_preset_section(presets_text: str, element: str) -> str:
    """按元素区块标题定位，返回该元素下的全部预设队伍。"""
    in_section = False
    section_lines: list[str] = []
    for line in presets_text.split("\n"):
        stripped = line.strip()
        if stripped in ELEMENT_SECTIONS:
            if in_section and section_lines:
                return "\n".join(section_lines)
            in_section = (stripped == element)
            if in_section:
                section_lines = [stripped]
            continue
        if in_section:
            section_lines.append(line)
    if in_section and section_lines:
        return "\n".join(section_lines)
    return ""


def check_permission(
    mode: str,
    whitelist: list[str],
    blacklist: list[str],
    *,
    group_id: str = "",
    user_id: str = "",
) -> bool:
    """黑白名单鉴权。

    mode:
      - "whitelist": 仅 group_id/user_id 在白名单内才允许
      - "blacklist": group_id/user_id 在黑名单内则拒绝，其余放行
      - "off":       全放行（不限制模式）
    群聊按 group_id 判断；私聊按 user_id 判断。
    """
    mode = (mode or "off").strip().lower()
    if mode == "off":
        return True

    # 群聊有 group_id 就用 group_id；否则用 user_id（私聊）
    target = (group_id or user_id or "").strip()
    if not target:
        return mode == "blacklist"  # 无身份信息时：黑名单模式放行，白名单模式拒绝

    if mode == "whitelist":
        return target in whitelist
    if mode == "blacklist":
        return target not in blacklist
    return False
