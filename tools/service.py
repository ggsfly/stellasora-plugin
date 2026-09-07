#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务层：把查询核心逻辑提炼为可复用函数。

插件（plugin.py）与本地实验脚本共用本模块，
保证独立运行与插件运行行为一致。

缓存目录参数化：插件运行时用 MaiBot 分配的 runtime_dir，
CLI 运行时用 data/.cache。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import json
import logging
import re
import threading

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
_cached_overrides_aliases: Optional[Dict[str, str]] = None


def _get_overrides_json_aliases() -> Dict[str, str]:
    """读取 data/overrides.json 中的别名映射（人工底层修正层）。"""
    global _cached_overrides_aliases
    if _cached_overrides_aliases is None:
        path = _DATA_DIR / "overrides.json"
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _cached_overrides_aliases = {
                        k: v for k, v in (data.get("aliases", {}) or {}).items()
                        if isinstance(k, str) and not k.startswith("_")
                    }
                else:
                    _cached_overrides_aliases = {}
            except Exception as e:
                logger.warning("Failed to load overrides.json aliases: %s", e)
                _cached_overrides_aliases = {}
        else:
            _cached_overrides_aliases = {}
    return _cached_overrides_aliases


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


def find_character_names(text: str) -> list:
    """返回 text 中命中的角色名列表（字典原名，长名优先防子串误配）。

    在匹配角色名前先做别名替换预处理（支持 config.overrides.aliases 与
    data/overrides.json），将玩家俗称/变体映射为官方角色名，避免多角色联合
    查询识别失败。

    命中区间做掩码去重叠（如 "NazuNazuka" 中 Nazuna/Nazuka 区间重叠时，
    先命中的长名保留、被覆盖区间的短名跳过）。掩码 None = 未占用，
    "#" = 已被更长名占用。
    """
    if not text:
        return []
    lookup = _get_lookup()

    # 1. 收集别名映射（config.overrides.aliases 与 data/overrides.json 别名）
    alias_map: Dict[str, str] = {}
    candidate_aliases: Dict[str, str] = {}
    candidate_aliases.update(_get_overrides_json_aliases())
    candidate_aliases.update(lookup.custom_aliases)

    for alias, target in candidate_aliases.items():
        if not isinstance(alias, str) or len(alias.strip()) < 2:
            continue
        alias_clean = alias.strip()
        official_name = None
        res = lookup.lookup_term(alias_clean)
        if res and res.get("cat") == "Character":
            official_name = res.get("cn") or res.get("en")
        elif target in lookup.get_character_names():
            official_name = target
        elif isinstance(target, str):
            target_res = lookup.lookup_term(target)
            if target_res and target_res.get("cat") == "Character":
                official_name = target_res.get("cn") or target_res.get("en")
            elif target in lookup._main_dict:
                entry = lookup._main_dict[target]
                if entry.get("cat") == "Character":
                    official_name = entry.get("cn") or entry.get("en")

        if official_name and official_name != alias_clean:
            alias_map[alias_clean] = official_name

    # 2. 预处理：按别名长度从长到短在 text 中替换为官方角色名
    if alias_map:
        pattern = re.compile(
            "|".join(re.escape(k) for k in sorted(alias_map.keys(), key=len, reverse=True))
        )
        text = pattern.sub(lambda m: alias_map[m.group(0)], text)

    # 3. 匹配角色名并做掩码去重叠
    names = sorted(lookup.get_character_names(), key=len, reverse=True)
    found: list = []
    masked: list = [None] * len(text)  # None = 未占用（修复：之前是字符列表恒非 None）
    for name in names:
        if not name or len(name) < 2:
            continue
        start = 0
        while True:
            idx = text.find(name, start)
            if idx < 0:
                break
            if all(m is None for m in masked[idx:idx + len(name)]):
                found.append(name)
                for k in range(idx, idx + len(name)):
                    masked[k] = "#"
            start = idx + 1
    return found


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
def strip_infodoc_noise(text: str) -> str:
    """清理 infodoc 文本中的表格行号碎片与多余空行（token 精简）。

    注意：Potentials 标签行**不在此处删除**——parse_infodoc_teams 需要它们
    触发 disc/emblem/pot 模式切换。
    """
    if not text:
        return text
    kept = [line for line in text.split("\n") if not _ROW_NUM_LINE_RE.match(line.strip())]
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


def _parse_team_body(block_lines: list, name_res: list) -> tuple:
    """解析单支队伍区块体 → (members, roles, segments)。

    状态机（实测结构）：
        Description | Skill Upgrade Priority   ← 角色段头
        <角色> (<星级>) | <技能升级优先度>       ← 角色行（含技能优先度）
        <描述文本 / ★ Key Notes>
        Priority Potentials | Recommended Main Discs   ← 秘纹锚（数据入 discs）
        <秘纹数据>
        Optional Potentials | Emblem            ← 纹章锚（数据行转置）
        Affix Priority | <词条|数值...>          ← 纹章列模板首行
        <下一角色段头> / 区块尾

    槽位规则：区块体（锚点行之后）首个角色详情段 = 主控位，后续 = 支援位。
    Priority/Optional Potentials 的数据行**全程丢弃**（用户明确无需抓取）。
    """
    members: list = []
    roles: dict = {}
    segments: dict = {}
    seen: set = set()
    seg: Optional[dict] = None
    mode = "normal"           # normal | disc | emblem | pot
    emblem_cols: list = []    # 纹章列模板（None = 空列）
    emblem_cursor = 0         # 下一个待填充的非空列索引（轮转发牌）
    emblem_overflow: list = []  # 保留字段：防御性兼容（当前逻辑不使用）
    emblem_pending = None

    def _flush_emblem_into(target: Optional[dict]) -> None:
        """把 emblem 转置按 70/80/90 级写入目标角色段。

        溢出行按"轮转发牌"模型接续到非空列（实测 aqua 页 Suntide 段
        row126→70级列、row127→90级列、row195→80级列），无"未标注"情况。
        """
        grade_labels = ["70级", "80级", "90级"]
        if target is None:
            return
        for ci, col in enumerate(emblem_cols):
            label = grade_labels[ci] if ci < len(grade_labels) else f"第{ci + 1}档"
            if col:
                target["emblem"].append(f"{label}：{'、'.join(col)}")
            else:
                target["emblem"].append(f"{label}：无需升级")
        emblem_cols.clear()

    def _close_segment() -> None:
        """结束当前角色段：冲刷纹章转置并入队。"""
        nonlocal seg, mode
        if seg is not None and seg.get("en"):
            _flush_emblem_into(seg)
            segments[seg["en"]] = seg
            if seg["en"] not in members:
                members.append(seg["en"])
                roles[seg["en"]] = "主控位" if len(members) == 1 else "支援位"
        seg = None
        mode = "normal"

    for line in block_lines:
        low = line.lower()
        cells = _split_cells(line)

        # 区块内嵌套的锚点行（同队多 build 的第二锚）→ 视作段边界
        if "⏏" in line or "Back to Top" in line:
            _close_segment()
            continue

        # 角色段头
        if "description" in low and "skill upgrade priority" in low:
            _close_segment()
            seg = {"en": None, "skill": "", "description": [], "discs": [], "emblem": []}
            continue

        # Potentials 标签行 → 模式切换（判定顺序：Discs 优先 → Emblem → 纯 Potentials。
        # 标签行常为混排形态，如 "Priority Potentials | Recommended Main Discs"——
        # 其数据行是秘纹而非潜能，误判为 pot 会把秘纹/纹章全部丢弃。
        # 注意：标签行**不关闭角色段**——"Optional Potentials | Emblem" 出现在段中，
        # 其后的纹章数据仍属于当前角色段）
        if "recommended main discs" in low:
            mode = "disc"
            continue
        if "emblem" in low and len(line) <= 60:
            mode = "emblem"
            continue
        if re.match(r"^\s*(?:Priority|Optional) Potentials\b", line, re.IGNORECASE):
            _close_segment()
            mode = "pot"
            continue

        # 角色行（"已知角色 + ★"行）：段内遇到新角色 → 自动收尾上一段并开新段
        #（兼容两种形态：每段有 Description 头的常规布局，与无头行的变体）
        char_match = None
        for name, r in name_res:
            if r.match(cells[0]) and any("★" in c for c in cells):
                char_match = name
                break

        if char_match is not None:
            if seg is not None and seg.get("en"):
                _close_segment()
            seg = {"en": char_match, "skill": cells[1] if len(cells) > 1 else "",
                   "description": [], "discs": [], "emblem": []}
            if char_match not in members:
                members.append(char_match)
                roles[char_match] = "主控位" if len(members) == 1 else "支援位"
            continue

        if seg is None:
            continue

        # 段内数据行分派
        if mode == "disc":
            seg["discs"].append(line)
            continue
        if mode == "emblem":
            first_is_affix = cells and cells[0].lower() in ("affix priority", "词条优先级")
            if first_is_affix:
                emblem_cols = _split_emblem_columns(cells[1:])
                emblem_overflow = []
                emblem_pending = None
                continue

            # 数据行分派（Google Sheet 溢出模型，实测 row125-127）：
            # - Affix 主行的词条进列模板对应等级
            # - 溢出行（Affix 主行填满后的数据行）按"非空列轮转"接续：
            #   每对词条填入下一个非空列，对间 cursor 前进，列尾回绕。
            #   实测 aqua 页 Suntide 段：溢出行 1 对 1 → 70级列（cursor 0 起）；
            #   跨行 cursor 保持前进（row127 的 Engulfing Tide → 90级列）
            if any(col is not None for col in emblem_cols):
                pairs = _split_emblem_columns(cells)
                has_real = any(p is not None for p in pairs)
                if has_real:
                    for p in pairs:
                        if p is None:
                            continue
                        filled = False
                        for ci in range(emblem_cursor, len(emblem_cols)):
                            if emblem_cols[ci] is not None:
                                emblem_cols[ci].append(p if isinstance(p, str) else p[0])
                                emblem_cursor = ci + 1
                                filled = True
                                break
                        if not filled:
                            # cursor 后无非空列 → 回绕到最前（溢出条目超过列容量）
                            for ci in range(len(emblem_cols)):
                                if emblem_cols[ci] is not None:
                                    emblem_cols[ci].append(p if isinstance(p, str) else p[0])
                                    emblem_cursor = ci + 1
                                    filled = True
                                    break
                    continue
        if mode == "pot":
            # 潜能数据行（'+3 levels' 结尾的短行）丢弃；叙述行恢复段内描述
            if cells and all(c.endswith("levels") or re.match(r"^[\d.]+%$", c) for c in cells if c):
                continue
            mode = "normal"
            if seg is not None:
                seg["description"].append(line)
            continue
        # normal：描述文本
        seg["description"].append(line)

    _close_segment()
    return members, roles, segments


def extract_team_blocks(infodoc_text: str, character_en: str, all_character_names: list) -> list:
    """从详细页收集**所有包含问询角色的队伍区块**（结构化段级解析）。

    详细页为单元素纵向布局，每个队伍区块结构（实测）：
        <队伍名角色> (<build>) | ⏏ Back to Top ⏏   ← 区块锚点行（队名 ≠ 主控！）
        Description | Skill Upgrade Priority         ← 角色段头
        <主控角色> (<星级>) | <技能升级优先度>        ← 区块体首个角色详情段 = 主控位
        <描述文本 / ★ Key Notes>
        Priority Potentials | Recommended Main Discs ← 秘纹锚（数据入 discs）
        <秘纹数据>
        Optional Potentials | Emblem                 ← 纹章锚（数据行转置）
        Affix Priority | <词条|数值...>               ← 纹章列模板首行
        <下一角色段头> / <下一队伍锚点行>

    槽位判定规则：**区块体（锚点行之后）内第一个角色详情段 = 主控位，后续角色均为
    支援位**；锚点行队名角色不参与成员提取（队伍命名可 ≠ 主控，如暗队
    Otoha (Laser) 的主控是 Cosette）。

    问询角色可能出现在**多个队伍**（如珂赛特既是 Otoha (Laser) 队主控，又是
    花铃/翡冷翠等队的支援）：她作为锚点行队名 → 该区块收集（asker_role=main）；
    她作为区块体成员 → 其所属区块也收集（asker_role=support）。

    Returns:
        区块信息列表 [{name, members, roles, segments, asker_role}...]，
        按页面出现顺序排列；未命中时返回 []。segments[en] = {"skill": str,
        "description": [行], "discs": [行], "emblem": [行]}。
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

    # 2. 逐区块解析；收集包含问询角色的区块
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

        members, roles, segments = _parse_team_body(block_lines, name_res)

        # 收集条件：问询角色是队名角色 或 区块体成员
        asker_hit = char_re.match(team_name) or character_en in members
        if asker_hit:
            # asker_role：区块体首个角色详情段 = 主控位；问询角色排首位则主控
            asker_role = "main" if members and members[0] == character_en else "support"
            results.append({
                "name": team_name,
                "members": list(members),
                "roles": dict(roles),
                "segments": dict(segments),
                "asker_role": asker_role,
            })

    return results


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


# 空列标记（单格，无值配对）：该纹章等级无推荐内容
_EMPTY_EMBLEM_RE = re.compile(r"^(?:Not needed|无需升级)。?$", re.IGNORECASE)
# 纹章数值格形态：百分比 / 纯数字 / +N levels / 无需升级
_EMBLEM_VALUE_RE = re.compile(r"^[\d.]+\s*%$|^[\d.]+$|^\+\d+\s*levels?$|^无需升级$", re.IGNORECASE)


def _split_emblem_columns(cells: list) -> list:
    """把 Affix 行（去标签后）的格子流切成列模板。

    每列 = [词条, 数值] 两格，或 'Not needed/无需升级' 单格空列（None 占位）。
    判定依据：数值形态的格子是值格；空标记格自成单格列。
    """
    columns: list = []
    i = 0
    n = len(cells)
    while i < n:
        c = cells[i]
        if _EMPTY_EMBLEM_RE.match(c):
            columns.append(None)  # 空列
            i += 1
        elif i + 1 < n and _EMBLEM_VALUE_RE.match(cells[i + 1]):
            columns.append([f"{c} {cells[i + 1]}".strip()])
            i += 2
        else:
            columns.append([c])  # 无值词条（罕见）
            i += 1
    return columns

def find_teams_by_members(terms: list, cache_dir: Path) -> Optional[dict]:
    """联合查询：判定 2-3 个角色是否共属同一配队。

    流程（用户约定）：把现有队伍做成"仅成员集合"字典 → 全部角色命中同一队伍
    才放行；否则返回 None（由调用方回"未找到"）。

    **队伍可以跨元素混编**（如小禾-多娜-苍兰队），因此候选页为全部六元素
    详情页逐一扫描，不做"首角色元素"捷径。

    Args:
        terms: 问句中命中的角色名列表（中英文均可，内部 lookup_term 归一为
            英文名；归一后去重，不足 2 个或超过 3 个视为不命中）
        cache_dir: 缓存目录（fetcher 参数）

    Returns:
        命中时 {"team_name": 完整队名, "members": [英文名按序],
        "element": 队伍元素, "page_text": 剥离噪声后的元素页全文}；
        任一角色未命中或无共同队伍时 None。
    """
    if not terms:
        return None
    lookup, _last, st_fetcher, _gd, _replacer = _get_services(cache_dir)
    en_names: list = []
    for term in terms:
        res = lookup.lookup_term(term)
        if not res or res.get("cat") != "Character":
            return None  # 非角色词条直接判不命中
        if res["en"] not in en_names:
            en_names.append(res["en"])
    if len(en_names) < 2 or len(en_names) > 3:
        return None  # 归一后不是 2-3 个角色

    name_res = [(n, re.compile(r"^" + re.escape(n) + r"(\s|\(|$)")) for n in en_names]
    wanted = set(en_names)

    # 全元素页逐一扫描（抓取缓存 1h TTL，重复查询零成本）
    for element in ELEMENT_SECTIONS:
        infodoc_text = st_fetcher.fetch_infodoc(element.lower())
        if not infodoc_text or "Error" in infodoc_text:
            continue
        clean = strip_infodoc_noise(infodoc_text)
        lines = clean.split("\n")

        # 队伍字典：{队名: 成员英文名列表}，单页扫描一次
        current_name = ""
        current_members: list = []
        teams: dict = {}
        order: list = []
        for line in lines:
            if "⏏" in line or "Back to Top" in line:
                cleaned = _TOP_ANCHOR_RE.sub("", line).rstrip(" |").strip()
                cells = _split_cells(cleaned)
                if cells:
                    if current_name:
                        teams[current_name] = current_members
                    current_name = cells[0]
                    current_members = []
                    if current_name not in teams:
                        order.append(current_name)
                continue
            if not current_name:
                continue
            for c in _split_cells(line):
                for n, r in name_res:
                    if n not in current_members and r.match(c):
                        current_members.append(n)
        if current_name:
            teams[current_name] = current_members

        # 匹配：所有角色都在同一队伍里
        for name in order:
            if wanted.issubset(set(teams.get(name, []))):
                return {
                    "team_name": name,
                    "members": teams[name],
                    "element": element,
                    "page_text": clean,
                }
    return None


def query_how(
    term: str,
    cache_dir: Path,
    with_presets: bool = False,
    max_length: Optional[int] = None,
    question: str = "",
    members: Optional[list] = None,
    element_override: Optional[str] = None,
) -> str:
    """how 桶：配队/纹章/秘纹/技能优先度（--presets 时附加预设码）。

    资料层采用全量提供策略（B 路线）：所有匹配队伍及角色四类字段（描述、技能、
    秘纹、纹章）均完整载入资料，输出裁剪与格式控制交由上层 LLM Prompt 完成。

    Args:
        term: 查询词（角色名/元素名，经 lookup_term 归一）
        cache_dir: 本地缓存目录路径
        with_presets: 是否在输出前附加预设码推荐内容
        max_length: 输出文本最大字符数限制（None 表示不限制）
        question: 用户原话（上层透传参数，本函数全量提供资料，输出裁剪交由 Prompt 控制）
        members: 多角色联合查询的角色英文名列表（2-3 个）；非空时仅输出
            同时包含全部成员的队伍，"本角色"标签覆盖所有问询角色
        element_override: 多角色联合查询时由 find_teams_by_members 确定的队伍
            元素页（问询角色可能各自属于多个元素页，队伍所在页以匹配结果为准）

    Returns:
        包含攻略文本（与可选预设码）的格式化字符串，未找到时返回提示信息
    """
    lookup, _last, st_fetcher, gd_fetcher, replacer = _get_services(cache_dir)
    res = lookup.lookup_term(term)
    if not res:
        return f"[{term}] 未在字典中找到。请检查拼写，或使用查词工具确认。"

    element: Optional[str] = None
    character_en = res["en"]
    is_character = res["cat"] == "Character"

    # 查询侧兜底：非角色条目命中但存在 cn 前缀匹配的唯一 Character 条目时，
    # 自动改路由到该角色（如 "薇洛" 命中 Item，但 "薇洛（盛夏）" 是 Character）
    if not is_character:
        candidates = [
            c_name for c_name in lookup.get_character_names()
            if c_name.startswith(res["cn"]) or res["en"] in c_name
        ]
        char_hits = []
        for c_name in candidates:
            char_res = lookup.lookup_term(c_name)
            if char_res and char_res.get("cat") == "Character":
                char_hits.append(char_res)
        uniq = {r["en"] for r in char_hits}
        if len(uniq) == 1:
            res = char_hits[0]
            term = res["cn"]  # 后续提示用角色本名
            character_en = res["en"]
            is_character = True

    if element_override:
        element = element_override
    elif is_character:
        num_id = res["id"].split(".")[1]
        trekker_text = st_fetcher.fetch_trekker(num_id)
        element = detect_element(trekker_text)

        # trekker 页的 detect_element 是全文关键词搜索，可能被页面里其他元素
        # 关键词误判（如薇洛（盛夏）trekker 页含 Lux 关联字但实际是 Aqua 队）。
        # 权威判定：全元素页扫描找角色真实所在队伍页；扫描确认后才采信
        # trekker 的判定结果，扫描发现不一致时以扫描为准。
        scanned = None
        for elem in ELEMENT_SECTIONS:
            page = st_fetcher.fetch_infodoc(elem.lower())
            if not page or "Error" in page:
                continue
            char_re_probe = re.compile(r"^" + re.escape(character_en) + r"(\s|\(|$)")
            for probe_line in strip_infodoc_noise(page).split("\n"):
                probe_cells = _split_cells(probe_line)
                if any(char_re_probe.match(c) for c in probe_cells):
                    scanned = elem
                    break
            if scanned:
                break
        if scanned and element and scanned != element:
            logger.info(
                "element 修正: %s trekker=%s → 扫描=%s", character_en, element, scanned
            )
            element = scanned
        elif scanned:
            element = scanned

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
                also = [m for m in (members or []) if m != character_en]
                teams = extract_team_blocks(infodoc_text, character_en, lookup.get_character_names())
                # 多角色联合查询：只保留同时包含全部成员的队伍
                if also:
                    wanted = set(also) | {character_en}
                    teams = [t for t in teams if wanted.issubset(set(t["members"]))]
                if teams:
                    # 输出顺序：问询角色（们）排区块首位的队伍在前——保持页序即可，
                    # 槽位与定位已在各成员标签中体现
                    # Rotation 块：仅索引页提供；知识库规则约定默认不转述，
                    # 用户明确询问输出手法时由 LLM 取用
                    rotation = extract_rotation(index_text, character_en)
                    if rotation:
                        lines.append("=== 输出手法（Rotation，索引页） ===")
                        lines.append(strip_game_markup(replacer.replace(rotation)))
                        lines.append("")

                    asker_set = set(members) if members else {character_en}

                    for team_idx, team in enumerate(teams):
                        # 队名行（过字典替换，保留 build 流派信息）
                        lines.append(f"配队{team_idx + 1}（{strip_game_markup(replacer.replace(team['name']))}）")

                        # 成员顺序：问询角色优先，其余按区块内出现顺序
                        ordered = [m for m in team["members"] if m in asker_set]
                        ordered += [m for m in team["members"] if m not in asker_set]

                        for m in ordered:
                            seg = team["segments"].get(m)
                            if not seg:
                                continue
                            role = team["roles"].get(m, "支援位")
                            is_asker = m in asker_set
                            tag = "本角色" if is_asker else "队友"
                            member_res = lookup.lookup_term(m)
                            member_cn = member_res["cn"] if member_res else m
                            lines.append(f"{tag}{member_cn}（{role}）")

                            # 资料层全量（B 路线）：所有成员的所有字段一律进资料，
                            # 只过 replacer（字典译名）+ strip_game_markup；
                            # 输出裁剪完全由 prompt 规则 4 指引 LLM 自行完成
                            if seg["description"]:
                                lines.append("描述：")
                                lines.extend(
                                    strip_game_markup(replacer.replace(d))
                                    for d in seg["description"]
                                )
                            if seg["skill"]:
                                lines.append(f"技能升级优先度：{strip_game_markup(replacer.replace(seg['skill']))}")
                            if seg["discs"]:
                                lines.append("推荐主位秘纹：")
                                lines.extend(
                                    strip_game_markup(replacer.replace(d))
                                    for d in seg["discs"]
                                )
                            if seg["emblem"]:
                                lines.append("纹章推荐：")
                                lines.extend(
                                    strip_game_markup(replacer.replace(d))
                                    for d in seg["emblem"]
                                )
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


# 预设码文档行识别：20+ 位大写字母数字串 = 预设码；标签行含 Trekker/Preset Code/Slot
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
