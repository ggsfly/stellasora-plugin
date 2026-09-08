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
from typing import Any, Dict, List, Optional, Tuple

import json
import logging
import re
import threading

from cache import CacheManager
from dict_lookup import DictLookup
from fetcher_google_doc import GoogleDocFetcher
from fetcher_stelladb import StelladbFetcher, _read_offline_file
import term_replace as _term_replace_module
from text_clean import detect_element, strip_game_markup

logger = logging.getLogger("stellasora.service")

# 数据目录模块级常量：dict.json/names.json 等数据文件的唯一归属地
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# 离线 infodocs 目录常量：how 新链路（query_how_rows）按行读取
# data/offline/infodocs/{element}.json 的唯一数据定位（不依赖缓存目录）
_INFODOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "offline" / "infodocs"

# 统一队伍-槽位表缓存（data/offline/presets/team_table.json）
_team_table_cache: Optional[Dict[str, Any]] = None

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


def _scan_character_hits(text: str) -> list:
    """掩码扫描命中角色：(命中区间首字符索引, 官方名)，按匹配序返回。

    匹配前先做别名替换预处理（支持 config.overrides.aliases 与
    data/overrides.json），将玩家俗称/变体映射为官方角色名——替换后的
    text 即扫描对象，索引为替换后文本的位置（保序语义以此为准）。

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

    # 3. 匹配角色名并做掩码去重叠（记录命中区间首字符索引）
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
                found.append((idx, name))
                for k in range(idx, idx + len(name)):
                    masked[k] = "#"
            start = idx + 1
    return found


def find_character_names(text: str) -> list:
    """返回 text 中命中的角色名列表（字典原名，长名优先防子串误配）。

    在匹配角色名前先做别名替换预处理（支持 config.overrides.aliases 与
    data/overrides.json），将玩家俗称/变体映射为官方角色名，避免多角色联合
    查询识别失败。

    命中区间做掩码去重叠（如 "NazuNazuka" 中 Nazuna/Nazuka 区间重叠时，
    先命中的长名保留、被覆盖区间的短名跳过）。掩码 None = 未占用，
    "#" = 已被更长名占用。
    """
    return [name for _idx, name in _scan_character_hits(text)]


def find_character_names_ordered(text: str) -> list:
    """返回 text 中命中的角色名列表，按问句首次出现位置升序（保序键=命中
    区间首字符最小索引，与长度排序/字典遍历序解耦；等长名先后由文本位置
    唯一决定；每角色最多一次，掩码去重叠语义保留）。"""
    return [name for _idx, name in sorted(_scan_character_hits(text), key=lambda t: t[0])]


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


# 区块锚点行内的导航片段：'⏏ Back to Top ⏏'（含前后空格），行内队名保留
_TOP_ANCHOR_RE = re.compile(r"\s*⏏\s*Back to Top\s*⏏\s*", re.IGNORECASE)

# 索引页行内导航段（队名行中夹带的翻页按钮，不属于任何元素队伍）
_NAV_CELLS = {"<< Prev", "Next >>"}


def _split_cells(line: str) -> list:
    return [c.strip() for c in line.split(" | ") if c.strip()]


def _parse_block_body(block_lines: list, name_res: list) -> tuple:
    """解析单支队伍区块体 → (members, roles, segments)（单区块版结构化解析引擎）。

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
            if cells and r.match(cells[0]) and any("★" in c for c in cells):
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


def load_team_table() -> Dict[str, Any]:
    """加载统一队伍-槽位表（data/offline/presets/team_table.json）。

    缺失或损坏时记录警告日志并返回空表结构 {"rows": [], "report": {}}，
    不抛出异常。读取成功后缓存于 _team_table_cache。
    """
    global _team_table_cache
    if _team_table_cache is not None:
        return _team_table_cache

    table_path = _DATA_DIR / "offline" / "presets" / "team_table.json"
    if not table_path.is_file():
        logger.warning("team_table.json not found: %s", table_path)
        _team_table_cache = {"rows": [], "report": {}}
        return _team_table_cache

    try:
        with table_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "rows" in data:
            _team_table_cache = data
        else:
            logger.warning("team_table.json invalid structure: root dict missing 'rows'")
            _team_table_cache = {"rows": [], "report": {}}
    except Exception as e:
        logger.warning("Failed to load team_table.json: %s", e)
        _team_table_cache = {"rows": [], "report": {}}

    return _team_table_cache


def reload_team_table() -> None:
    """清除统一队伍-槽位表缓存，强制下次查询重新读盘。"""
    global _team_table_cache
    _team_table_cache = None


def find_team_rows(
    member_ids: list[int],
    element: Optional[str] = None,
) -> list[dict]:
    """按成员 CharId 集合与可选元素过滤查询统一队伍-槽位表中的匹配行。

    匹配规则：
        - 成员过滤：请求的所有 member_ids 必须全包含于该行 slots 的 char_id 集合中；
        - 元素过滤：若传入 element，则行 element 不区分大小写精确匹配。
    """
    table = load_team_table()
    rows = table.get("rows", [])
    if not rows:
        return []

    wanted_ids = set(member_ids)
    target_elem = element.lower() if element else None

    matches: list[dict] = []
    for row in rows:
        if target_elem and row.get("element", "").lower() != target_elem:
            continue
        row_char_ids = {
            slot["char_id"]
            for slot in row.get("slots", [])
            if isinstance(slot, dict) and "char_id" in slot
        }
        if wanted_ids <= row_char_ids:
            matches.append(row)

    return matches


def extract_block_by_name(
    infodoc_text: str,
    block_name: str,
    all_character_names: Optional[list] = None,
) -> Optional[dict]:
    """从详细页按锚点队名精确提取单个队伍区块体并结构化解析。

    复用 _TOP_ANCHOR_RE 锚点拆分与 _parse_block_body 单区块解析逻辑。

    Returns:
        匹配时返回 dict {"name": block_name, "members": members, "roles": roles, "segments": segments}，
        未命中时返回 None。
    """
    if not infodoc_text or not block_name:
        return None

    if all_character_names is None:
        all_character_names = _get_lookup().get_character_names()

    en_names = [n for n in all_character_names if isinstance(n, str) and n.isascii() and len(n) >= 2]
    name_res = [(n, re.compile(r"^" + re.escape(n) + r"(\s|\(|$)")) for n in en_names]

    lines = infodoc_text.split("\n")
    anchors: list[Tuple[int, str]] = []
    for li, line in enumerate(lines):
        if "⏏" in line or "Back to Top" in line:
            cleaned = _TOP_ANCHOR_RE.sub("", line).rstrip(" |").strip()
            cells = _split_cells(cleaned)
            if not cells:
                continue
            anchors.append((li, cells[0]))

    if not anchors:
        return None

    for idx, (start, team_name) in enumerate(anchors):
        if team_name != block_name:
            continue
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(lines)
        body_lines = [
            _TOP_ANCHOR_RE.sub("", lines[li]).rstrip(" |").strip()
            for li in range(start + 1, end)
            if not ("⏏" in lines[li] or "Back to Top" in lines[li])
        ]
        block_lines = [team_name] + [bl for bl in body_lines if _split_cells(bl)]
        members, roles, segments = _parse_block_body(block_lines, name_res)
        return {
            "name": block_name,
            "members": list(members),
            "roles": dict(roles),
            "segments": dict(segments),
        }

    return None


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


# ---------------------------------------------------------------------------
# 输出装配层噪声过滤（移植自旧 strip_infodoc_noise，d701800~1:tools/service.py）
#
# 警告：过滤**只允许**发生在 segments 已产出的字段值装配进输出文本时——
# 解析层（_parse_block_body/_split_emblem_columns/_flush_emblem_into）禁止滤噪：
# 发牌序按噪声占位校准，解析层滤噪会导致转置错位。
# 与旧实现的"Potentials 标签行不在此处删除——parse 需要它们触发模式切换"
# 依赖意识同源：行标签判断仍完全由解析层消费，本层不触碰任何标签行。
# ---------------------------------------------------------------------------


def _filter_noise_lines(lines: list[str]) -> list[str]:
    """过滤字段行序列中的纯数字噪声行，并折叠连续多余空行（token 精简）。

    移植语义（旧 strip_infodoc_noise）：
    - 行过滤：详细页 HTML 表格的行号列折叠出的纯数字行（如 "19"、"297"），
      对 LLM 无意义——旧实现为 _ROW_NUM_LINE_RE = ^\\d{1,3}$（整页场景下
      限制 1-3 位以免误伤更长纯数字行），本层只处理字段行、无整页误伤面，
      故用 .strip().isdigit() 表达"纯数字行"的完整语义（实测全部区块
      行号均 ≤3 位，两形态在真实数据上零差异）；
    - 空行折叠：旧 re.sub(r"\\n{3,}", "\\n\\n") 在列表语义下等价于
      "连续 ≥2 个空行折叠为 1 个空行"（N 个空行 = N+1 个换行，
      3+ 连续换行折叠为 2 个换行）。

    仅作用于 description/discs 字段行；skill 为整体字符串不经此函数。
    """
    kept = [ln for ln in lines if not ln.strip().isdigit()]
    folded: list[str] = []
    blank_run = 0
    for ln in kept:
        if ln.strip() == "":
            blank_run += 1
            if blank_run <= 1:  # 连续空行只保留首个（≥2 折叠为 1）
                folded.append(ln)
        else:
            blank_run = 0
            folded.append(ln)
    return folded


def _filter_emblem_entry(entry: str) -> str:
    """过滤纹章复合串中的纯数字噪声子条目（新增逻辑，旧实现无此结构）。

    emblem 条目由 _flush_emblem_into 以 `70级：词条、词条、...` 形态拼接
    （'、'.join(col)），整串 .isdigit() 恒为 False，须剥等级前缀后逐子条目判定。
    处理规则：首个 `：` 前含 `级` → 剥前缀，余下按 `、` 拆子条目，
    纯数字子条目（行号碎片）丢弃后重组接回前缀；无 `级` 前缀（含
    `第N档` 兜底标签形态）→ 整串按子条目处理，标签随首个子条目保留。
    """
    if not entry:
        return entry
    sep_idx = entry.find("：")
    if 0 < sep_idx and "级" in entry[:sep_idx]:
        prefix = entry[: sep_idx + 1]  # 前缀含 "："
        body = entry[sep_idx + 1 :]
        return prefix + "、".join(s for s in body.split("、") if not s.strip().isdigit())
    # 无等级前缀：整体按子条目处理（全部为纯数字时返回空串，由调用方丢弃）
    return "、".join(s for s in entry.split("、") if not s.strip().isdigit())


def query_how_rows(
    rows: list[dict],
    with_presets: bool = False,
    max_length: Optional[int] = None,
    question: str = "",
) -> str:
    """how 桶（表驱动新链路）：命中行按 guide_ref 区块归组，逐组抓取 infodoc 区块输出。

    资料层采用全量提供策略（B 路线）：同区块命中行并为一组（组序 = 首次出现序），
    每组只读取其 guide_ref 指向的区块一次，四类字段（描述、技能、秘纹、纹章）完整
    载入资料；缺段成员与后续行新增成员由组内队友并集 rescue，输出裁剪与格式控制
    交由上层 LLM Prompt 完成——不读整页、不做元素判定、不做全元素扫描，
    token 消耗随命中队伍规模线性增长。

    详略策略：问句命中 1-2 个角色时仅第一个问询角色详述（其余成员并入"队友"
    并集行）；≥3 个角色、问句含全量触发词（完整/详细/全部/所有）或未命中
    角色时全部成员详述。

    Args:
        rows: find_team_rows 命中的统一队伍-槽位表行列表
           （含 slots/team_name_*/guide_ref/rotation/preset_code 字段）
        with_presets: 是否在每组尾附加预设码行（码原文保真）
        max_length: 输出文本最大字符数限制（None 表示不限制）
        question: 用户原话——用于推导问询角色（问句命中角色排成员首位）

    Returns:
        包含攻略文本（与可选预设码行）的格式化字符串；无可用行时返回空串
    """
    lookup = _get_lookup()  # 确保共享查词服务已初始化
    # replacer 与 lookup 同源：_get_lookup 初始化时构建的共享实例（元组第 5 位）
    replacer = _instances[str(_DATA_DIR)][4]

    # 问句 → 问询角色 EN 名有序 list（保序版提取；多角色详略依赖顺序）
    asker_ens: list = []
    for cn_name in find_character_names_ordered(question):
        res = lookup.lookup_term(cn_name)
        if res and res.get("cat") == "Character":
            en = res["en"]
            if en not in asker_ens:
                asker_ens.append(en)

    # 详略策略（用户规则）：问句命中 1-2 角色→仅第一个详述；≥3→全部详述；
    # 含全量触发词（与 prompt 4a 词表一致）→全员详述（不设限，含非问询成员）；
    # 字段筛选类问法（配队/纹章/秘纹/技能/升级，对应 prompt 4b-4e 的"各成员"
    # 语义）要求各成员字段齐全，同样不设详略限制；
    # 空角色集→回退 T2 行为（展开行全部 slots 详述，detail_ens=None 表示不设限）
    detail_ens: Optional[set] = None
    if asker_ens:
        if any(w in question for w in ("完整", "详细", "全部", "所有", "配队", "纹章", "秘纹", "技能", "升级")) or len(asker_ens) >= 3:
            detail_ens = None
        else:
            detail_ens = {asker_ens[0]}

    # 同元素页文本与同区块结构化结果在本次调用内复用（省 IO/CPU）
    page_cache: Dict[str, str] = {}
    block_cache: Dict[Tuple[str, str], Optional[dict]] = {}

    def _get_block(element: str, block_name: str) -> Optional[dict]:
        """按行 guide_ref 精准读取对应元素页并提取该区块（同区块缓存复用）。"""
        cache_key = (element, block_name)
        if cache_key not in block_cache:
            if element not in page_cache:
                page_cache[element] = _read_offline_file(_INFODOCS_DIR / f"{element}.json") or ""
            page_text = page_cache[element]
            block_cache[cache_key] = (
                extract_block_by_name(page_text, block_name) if page_text else None
            )
        return block_cache[cache_key]

    # Rotation 块：构建时已固化进行内 rotation 字段，运行时零索引页解析；
    # 多行主控相同时按出现顺序去重（知识库规则约定默认不转述，用户明确
    # 询问输出手法时由 LLM 取用）
    rotations: list[str] = []
    for row in rows:
        rot = str(row.get("rotation") or "").strip()
        if rot and rot not in rotations:
            rotations.append(rot)

    lines: list[str] = []
    if rotations:
        lines.append("=== 输出手法（Rotation，索引页） ===")
        lines.extend(strip_game_markup(replacer.replace(r)) for r in rotations)
        lines.append("")

    # 行 → 组归并（Design X）：同 guide_ref 区块（或同预设码的未关联行）跨行并为一组，
    # 组序 = 行序中首次出现序；归组键 = (element, block)，未关联行 = ("", 码/main_key)
    groups: list = []  # [(key, [row, ...]), ...] 保序
    group_index: Dict[tuple, int] = {}
    for row in rows:
        ref = row.get("guide_ref")
        if ref:
            key = (str(ref.get("element", "")), str(ref.get("block", "")))
        else:
            key = ("", str(row.get("preset_code") or row.get("main_key") or ""))
        if key in group_index:
            groups[group_index[key]][1].append(row)
        else:
            group_index[key] = len(groups)
            groups.append((key, [row]))

    for group_no, (_key, group_rows) in enumerate(groups, 1):
        first_row = group_rows[0]
        slots = [s for s in first_row.get("slots", []) if isinstance(s, dict) and s.get("en")]
        if not slots:
            continue

        # 组头：编号 + 队名（infodoc 区块名优先，缺则回退预设表队名，过字典替换保留流派信息）
        team_name = first_row.get("team_name_infodoc") or first_row.get("team_name_preset") or ""
        lines.append(f"{group_no}. {strip_game_markup(replacer.replace(team_name))}")

        # 角色定位取首行槽位：首位=主控位，其余=支援位
        role_by_en = {
            s["en"]: "主控位" if si == 0 else "支援位" for si, s in enumerate(slots)
        }
        # 成员顺序：问询角色优先，其余按槽位序
        ordered = [s for s in slots if s["en"] in asker_ens]
        ordered += [s for s in slots if s["en"] not in asker_ens]

        guide_ref = first_row.get("guide_ref")
        block = (
            _get_block(guide_ref.get("element", ""), guide_ref.get("block", ""))
            if guide_ref
            else None
        )

        detailed_ids: set = set()
        for slot in ordered:
            en = slot["en"]
            if detail_ens is not None and en not in detail_ens:
                continue  # 非详述成员（含未详述的问询角色）不输出名字行，
                          # 由下方队友并集扫描 rescue——"其他角色简要说明"
            seg = block.get("segments", {}).get(en) if block else None
            if block is not None and seg is None:
                # 区块已解析但缺该成员段（关联规则保证 slots ⊆ 区块成员，
                # 此处仅防御数据漂移）：不输出名字行，由下方队友并集扫描 rescue
                continue
            member_cn = slot.get("cn") or en
            lines.append(f"{member_cn}（{role_by_en.get(en, '支援位')}）")
            if slot.get("char_id") is not None:
                detailed_ids.add(slot["char_id"])
            if seg is None:
                continue

            # 资料层全量（B 路线）：所有成员的所有字段一律进资料，
            # 只过 replacer（字典译名）+ strip_game_markup；
            # 输出裁剪完全由 prompt 规则 4 指引 LLM 自行完成
            # 噪声过滤在装配层进行（纯数字行号碎片），解析层保持原样——
            # 发牌序按噪声占位校准，解析层滤噪会导致转置错位
            if seg["description"]:
                lines.append("描述：")
                lines.extend(
                    strip_game_markup(replacer.replace(d))
                    for d in _filter_noise_lines(seg["description"])
                )
            if seg["skill"]:
                # skill 整体保留不做子条目过滤：`1/10/1/10 (...)` 的 `/` 分隔
                # 结构非行号噪声形态
                lines.append(f"技能升级优先度：{strip_game_markup(replacer.replace(seg['skill']))}")
            if seg["discs"]:
                lines.append("推荐主位秘纹：")
                lines.extend(
                    strip_game_markup(replacer.replace(d))
                    for d in _filter_noise_lines(seg["discs"])
                )
            if seg["emblem"]:
                lines.append("纹章推荐：")
                for entry in seg["emblem"]:
                    cleaned = _filter_emblem_entry(entry)
                    if cleaned:
                        lines.append(strip_game_markup(replacer.replace(cleaned)))
            lines.append("")

        # 队友并集：扫描组内全部行（含首行——缺段成员 rescue），char_id 不在 detailed_ids 的成员
        teammate_seen: set = set()
        teammate_entries: list = []
        for row in group_rows:
            row_slots = [s for s in row.get("slots", []) if isinstance(s, dict) and s.get("en")]
            for si, s in enumerate(row_slots):
                cid = s.get("char_id")
                ident = cid if cid is not None else f"en:{s['en']}"
                if ident in detailed_ids or ident in teammate_seen:
                    continue
                teammate_seen.add(ident)
                pos = "主控位" if si == 0 else "支援位"
                teammate_entries.append(f"{s.get('cn') or s['en']}（{pos}）")
        if teammate_entries:
            lines.append(f"队友：{'、'.join(teammate_entries)}")

        # 组尾预设码行（用户明确要求时）：组内按行序收集去重码，
        # 成员描述用该码首次出现行的 slots（码原文保真，replacer 不改码）
        if with_presets:
            seen_codes: set = set()
            for row in group_rows:
                code = row.get("preset_code")
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                r_slots = [s for s in row.get("slots", []) if isinstance(s, dict) and s.get("en")]
                members_desc = "、".join(
                    [f"主控{r_slots[0].get('cn') or r_slots[0]['en']}", f"援护{r_slots[1].get('cn') or r_slots[1]['en']}"]
                    + [s.get("cn") or s["en"] for s in r_slots[2:]]
                )
                lines.append(f"预设码：{strip_game_markup(replacer.replace(code))}（{members_desc}）")
                lines.append("")

    return _fit_lines(lines, max_length)


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
