#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务层：把查询核心逻辑提炼为可复用函数。

插件（plugin.py）与本地实验脚本共用本模块，
保证独立运行与插件运行行为一致。

缓存目录参数化：插件运行时用 MaiBot 分配的 runtime_dir，
CLI 运行时用 data/.cache。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Dict, Optional, Set, Tuple

import json
import logging
import re
import threading

from dict_lookup import DictLookup
from fetcher_google_doc import GoogleDocFetcher
from fetcher_stelladb import StelladbFetcher, _read_offline_file
from text_clean import strip_game_markup
import term_replace as _term_replace_module

logger = logging.getLogger("stellasora.service")

# 数据目录模块级常量：dict.json/names.json 等数据文件的唯一归属地
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# 离线 infodocs 目录常量：how 链路按行读取
# data/offline/infodocs/{element}.json 的数据定位（不依赖缓存目录）
_INFODOCS_DIR = Path(__file__).resolve().parents[1] / "data" / "offline" / "infodocs"

# 统一队伍-槽位表缓存（data/offline/presets/team_table.json）
_team_table_cache: Optional[Dict[str, Any]] = None
_team_table_cache_mtime: Optional[float] = None

# 元素与战斗弱抗常量（what 扩展 / 角色 / 秘纹 / boss 渲染）
_ELEMENT_CN: Dict[str, str] = {
    "Ignis": "火",
    "Aqua": "水",
    "Terra": "地",
    "Ventus": "风",
    "Lux": "光",
    "Umbra": "暗",
    "None": "无",
}
_WEAK_LABEL = "弱点"
_RESIST_LABEL = "抗性"


def _element_cn(name: Any) -> str:
    """元素英文名 → 中文简称；未收录原样返回，非字符串转为字符串（None → 空串）。

    显式收窄返回类型为 str：数据源取出的元素字段为 Any，直接 dict.get(Any, Any)
    会被推断为 str | None，导致 "、".join(...) 类型不匹配。
    """
    if not isinstance(name, str):
        return "" if name is None else str(name)
    return _ELEMENT_CN.get(name, name)

# 讨伐术语中文常量（用户审定定稿）：what 渲染 blitz/raid 首领资料时，
# 字典未收录的英文术语在此映射为中文。类职业
# （class=Vanguard/Support/Versatile）已被字典 CharacterTag 覆盖，不需重复。
_TERM_CN = {
    "Damage Per Score": "单分伤害",
    "Cumulative HP": "累计生命",
    "Estimated Score Damage": "预估得分伤害",
    "Hit Rate": "命中率",
    "Attack Speed": "攻击速度",
    "Final DMG Taken": "最终受到伤害",
    "Blitz": "联合讨伐",
    "Raid": "终焉绝响",
    "Ranged": "远程",
    "Melee": "近战",
    "Mechanic": "机制",
}

# 决斗（duel）首领类型局部映射：仅怪物渲染器内部消费，不得并入 _TERM_CN
# （O15 硬断言 _TERM_CN 恒 11 项）
_DUEL_TYPE_CN = {
    "Overlord": "霸主",
}


def _term_cn(s: str) -> str:
    """讨伐术语 → 中文：先精确命中 _TERM_CN，否则做区分大小写的子串级替换。

    与 _resolve_key 第一步"精确别名优先"的定位语义一致：整体未命中再逐项
    子串替换，覆盖 stat 键/type/mechanic 嵌词场景（如 "Cumulative HP: 5000"、
    "Final DMG Taken (base)"）。无命中返回原串；空串直接返回。
    """
    if not s:
        return s
    if s in _TERM_CN:
        return _TERM_CN[s]
    for en, cn in _TERM_CN.items():
        s = s.replace(en, cn)
    return s

# 模块级单例（按数据目录缓存，避免每次调用重载 8.8MB 字典）；
# 值形状 = (lookup, last_cache_dir, st_fetcher, gd_fetcher, replacer)：
# cache_dir 变化时仅重建两个 fetcher，lookup 与 replacer 全进程复用
_instances: Dict[str, tuple] = {}
# 线程安全保护：跑在线程池中时，避免无锁并发首次调用重复解析字典
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


def _merged_alias_map() -> Dict[str, str]:
    """合并别名来源：overrides.json 别名打底，config 别名覆盖同名。

    供 DictLookup 构建/热更新 custom_aliases 使用——保证插件启动时即便
    config 未配置别名，overrides.json 中的人工底层修正（如「鹿鸣」→
    Item.214015.1）也不会被空 config 覆盖而丢失，避免秘纹/词条错位。
    """
    merged = dict(_get_overrides_json_aliases())
    merged.update(_pending_aliases)
    return merged


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
            lookup = _instances[key][0]
            if aliases is not None:
                lookup.set_custom_aliases(_merged_alias_map())


def _get_services(cache_dir: Path) -> tuple:
    """获取/构建共享服务元组 (lookup, last_cache_dir, st_fetcher, gd_fetcher, replacer)。

    锁内 check-then-init：lookup 只建一次并显式预热，replacer 基于已加载字典预建；
    cache_dir 与上次不同时仅重建 fetcher（lookup/replacer 复用）。
    """
    key = str(_DATA_DIR)
    with _init_lock:
        if key not in _instances:
            lookup = DictLookup(_DATA_DIR, custom_aliases=_merged_alias_map())
            # 先显式完成字典加载，再基于 _main_dict 构建替换器——不依赖
            # "构建 TermReplacer 隐含触发 _load" 的顺序假设，避免 preloaded_dict
            # 传到 None（【双审 SH-6】）。锁释放前 _main_dict/_name_index/
            # _lowercase_index/_character_names_cache 均已就绪，后续线程读取无竞态
            # （【双审 SH-6】）
            lookup._load()
            # 替换器全进程仅此一份：以已解析的 _main_dict 预建，避免 TermReplacer
            # 再自行 json.load 一份 8.8MB 字典（preloaded_dict 传 None 的隐患，
            # 【双审 SH-6】）；实例随 _instances 复用，不另设模块级全局
            replacer = _term_replace_module.TermReplacer(
                _DATA_DIR,
                preloaded_dict=lookup._main_dict,
            )
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
            else:
                entry = lookup.get_by_id(target)
                if entry and entry.get("cat") == "Character":
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


def query_what(term: str, cache_dir: Path, question: str = "") -> str:
    """what 桶：角色/物品"是什么"，输出已中文化的攻略文本。"""
    lookup, _last, st_fetcher, _gd, replacer = _get_services(cache_dir)

    # 1. 关键词概念路由（banner / disc 概念列表；blitz 当期讨伐整页）
    route = _route_what_keywords(term)
    if route == "banner":
        text, _ = _build_banner_material(st_fetcher, lookup)
        return text
    elif route == "blitz":
        text, _ = _build_blitz_material(st_fetcher, lookup)
        return text
    elif route == "disc":
        text, _ = _build_disc_list_material(st_fetcher, lookup)
        return text

    # 2. 查词与首领怪物路由
    res = lookup.lookup_term(term)
    if not res:
        monster_id = _match_monster(term, lookup, st_fetcher)
        if monster_id:
            modules = _detect_what_modules(question, "monster")
            text, ok = _build_monster_material(st_fetcher, monster_id, lookup, modules=modules)
            if ok and text:
                return text
        # 当期联合讨伐 boss 名兜底（中/英文名直接命中当期赛季 boss）
        if _match_blitz_boss(term, st_fetcher, lookup):
            text, _ = _build_blitz_material(st_fetcher, lookup)
            return text
        return f"[{term}] 未在字典中找到。请检查拼写，或使用查词工具确认。"

    # res 存在且 cat == "MonsterManual"
    if res["cat"] == "MonsterManual":
        parts = res["id"].split(".")
        num_id = parts[1] if len(parts) > 1 else res["id"]
        raid_dataset = st_fetcher.fetch_ssdata_dataset("raid") or {}
        if num_id in raid_dataset:
            monster_id = num_id
        else:
            monster_id = _match_monster(term, lookup, st_fetcher)

        if monster_id:
            modules = _detect_what_modules(question, "monster")
            text, ok = _build_monster_material(st_fetcher, monster_id, lookup, modules=modules)
            if ok and text:
                return text

        # 当期联合讨伐 boss 名兜底（MonsterManual 命中但 raid 找不到时的链接形态）
        if _match_blitz_boss(term, st_fetcher, lookup):
            text, _ = _build_blitz_material(st_fetcher, lookup)
            return text

        # 若该兜底 _match_monster 返回 None → 落入 step 5 的"没有专属攻略页"文案
        lines = [
            "=== 字典匹配 ===",
            f"  中文: {res['cn']}",
            f"  英文: {res['en']}",
            f"  ID:   {res['id']}",
            f"  类别: {res['cat']}",
            "",
            f"[{term}] 是 {res['cat']} 类词条（{res['en']} / {res['cn']}），没有专属攻略页。",
        ]
        return "\n".join(lines)

    # 3. 角色路由（优先 ssdata descCN，失败回退 trekker HTML 攻略）
    if res["cat"] == "Character":
        parts = res["id"].split(".")
        num_id = parts[1] if len(parts) > 1 else res["id"]
        modules = _detect_what_modules(question, "character")
        text, ok = _build_character_material(st_fetcher, num_id, lookup, modules=modules)
        if ok and text:
            return text

        # 回退现有 fetch_trekker(num_id) 路径（strip_game_markup + replacer.replace，维持现在输出）
        lines = [
            "=== 字典匹配 ===",
            f"  中文: {res['cn']}",
            f"  英文: {res['en']}",
            f"  ID:   {res['id']}",
            f"  类别: {res['cat']}",
            "",
            f"=== 角色攻略 (stelladb /trekker/{num_id}) ===",
            strip_game_markup(replacer.replace(st_fetcher.fetch_trekker(num_id))),
        ]
        return "\n".join(lines)

    # 4. 秘纹（disc）路由——按数字 id 成员判断，不按 cat 字符串
    parts = res["id"].split(".")
    num_id = parts[1] if len(parts) > 1 else res["id"]
    disc_dataset = st_fetcher.fetch_ssdata_dataset("disc") or {}
    if num_id in disc_dataset:
        modules = _detect_what_modules(question, "disc")
        text, ok = _build_disc_material(st_fetcher, num_id, lookup, modules=modules)
        if ok and text:
            return text

    # 5. 其它 cat → 维持现有"没有专属攻略页"文案
    lines = [
        "=== 字典匹配 ===",
        f"  中文: {res['cn']}",
        f"  英文: {res['en']}",
        f"  ID:   {res['id']}",
        f"  类别: {res['cat']}",
        "",
        f"[{term}] 是 {res['cat']} 类词条（{res['en']} / {res['cn']}），没有专属攻略页。",
    ]
    return "\n".join(lines)


# 区块锚点行内的导航片段：'⏏ Back to Top ⏏'（含前后空格），行内队名保留
_TOP_ANCHOR_RE = re.compile(r"\s*⏏\s*Back to Top\s*⏏\s*", re.IGNORECASE)

# 索引页行内导航段（队名行中夹带的翻页按钮，不属于任何元素队伍）
_NAV_CELLS = {"<< Prev", "Next >>"}


def _split_cells(line: str) -> list:
    if line.startswith("| "):
        line = " " + line
    elif line.startswith("|"):
        line = line.lstrip("|").lstrip()
    return [c.strip() for c in line.split(" | ") if c.strip() and c.strip() != "|"]


def _split_cells_keep_empty(line: str) -> list[str]:
    """按 ' | ' 切分，对每个 segment 执行 strip，保留中间与首部空字符串（emblem 分支专用）。"""
    if line.startswith("| "):
        line = " " + line
    return [c.strip() for c in line.split(" | ")]


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
    col_affix_idx: dict[int, int] = {}  # 列索引 k -> Affix 行词条格在 cs 中的绝对下标 j
    emblem_band_anchor: Optional[int] = None  # 横向列带首个词条绝对下标锚点
    band_col_start: int = 0   # 横向列带起始列号（右侧列带延续）

    def _flush_emblem_into(target: Optional[dict]) -> None:
        """把 emblem 转置按 70/80/90 级写入目标角色段。

        各列按网格绝对列索引与横向列带延续分派，保持 70/80/90 级或无需升级格式。
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
        col_affix_idx.clear()

    def _close_segment() -> None:
        """结束当前角色段：冲刷纹章转置并入队。"""
        nonlocal seg, mode, emblem_band_anchor, band_col_start
        if seg is not None and seg.get("en"):
            _flush_emblem_into(seg)
            segments[seg["en"]] = seg
            if seg["en"] not in members:
                members.append(seg["en"])
                roles[seg["en"]] = "主控位" if len(members) == 1 else "支援位"
        seg = None
        mode = "normal"
        emblem_band_anchor = None
        band_col_start = 0

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
        # 放宽长度阈值至 120 以容纳网格展开后的占位空段（如 " | | | Optional Potentials | Emblem"）
        if "emblem" in low and len(line) <= 120:
            mode = "emblem"
            continue
        if re.match(r"^\s*(?:Priority|Optional) Potentials\b", line.lstrip(" |"), re.IGNORECASE):
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
            seg["discs"].append(" | ".join(_split_cells(line)))
            continue
        if mode == "emblem":
            cs = _split_cells_keep_empty(line)
            non_empty = [c for c in cs if c]
            # 跳过全空行与纯数字单 cell 行（行号噪声）
            if not non_empty:
                continue
            if len(non_empty) == 1 and non_empty[0].isdigit():
                continue

            first_is_affix = non_empty[0].lower() in ("affix priority", "词条优先级")
            if first_is_affix:
                L = cs.index(non_empty[0])
                emblem_cols = []
                col_affix_idx = {}
                emblem_band_anchor = None
                band_col_start = 0
                j = L + 1
                k = 0
                while j < len(cs):
                    c = cs[j]
                    if not c:
                        j += 1
                        continue
                    if _EMPTY_EMBLEM_RE.match(c):
                        emblem_cols.append(None)
                        col_affix_idx[k] = j
                        k += 1
                        j += 1
                    elif j + 1 < len(cs) and _EMBLEM_VALUE_RE.match(cs[j + 1]):
                        emblem_cols.append([f"{c} {cs[j + 1]}".strip()])
                        col_affix_idx[k] = j
                        k += 1
                        j += 2
                    else:
                        emblem_cols.append([c])
                        col_affix_idx[k] = j
                        k += 1
                        j += 1
                continue

            # 数据行分派
            if not emblem_cols:
                continue

            # 提取所有非空、非纯数字的词条格 (j, cs[j]) 及其数值格 cs[j+1]
            pairs = []
            j = 0
            while j < len(cs):
                c = cs[j]
                if c and not c.isdigit() and not _EMBLEM_VALUE_RE.match(c):
                    val = cs[j + 1] if j + 1 < len(cs) and _EMBLEM_VALUE_RE.match(cs[j + 1]) else ""
                    entry = f"{c} {val}".strip() if val else c
                    pairs.append((j, entry))
                    j += 2 if val else 1
                else:
                    j += 1

            if not pairs:
                continue

            idx_to_col = {v: k for k, v in col_affix_idx.items()}

            is_arithmetic = False
            if len(pairs) >= 1:
                diffs = [pairs[i + 1][0] - pairs[i][0] for i in range(len(pairs) - 1)]
                is_arithmetic = all(d == 2 for d in diffs)

            for j_pos, entry in pairs:
                if j_pos in idx_to_col:
                    k = idx_to_col[j_pos]
                    if emblem_cols[k] is None:
                        emblem_cols[k] = [entry]
                    else:
                        emblem_cols[k].append(entry)
                elif is_arithmetic:
                    if emblem_band_anchor is not None and j_pos >= emblem_band_anchor:
                        pass
                    else:
                        emblem_band_anchor = pairs[0][0]
                        band_col_start = max(0, len(emblem_cols) - len(pairs))
                    k = band_col_start + (j_pos - emblem_band_anchor) // 2
                    if k < len(emblem_cols):
                        if emblem_cols[k] is None:
                            emblem_cols[k] = [entry]
                        else:
                            emblem_cols[k].append(entry)
                    else:
                        logger.warning("无法定位纹章列索引 k=%d（列上限 %d），追加至末列：%s", k, len(emblem_cols) - 1, entry)
                        if emblem_cols[-1] is None:
                            emblem_cols[-1] = [entry]
                        else:
                            emblem_cols[-1].append(entry)
                else:
                    logger.warning("纹章数据行格子下标非等差带且未命中列锚点（j=%d），追加至末列：%s", j_pos, entry)
                    if emblem_cols[-1] is None:
                        emblem_cols[-1] = [entry]
                    else:
                        emblem_cols[-1].append(entry)
            continue

        if mode == "pot":
            # 潜能数据行（'+3 levels' 结尾的短行）丢弃；叙述行恢复段内描述
            if cells and all(c.endswith("levels") or re.match(r"^[\d.]+%$", c) for c in cells if c):
                continue
            mode = "normal"
            if seg is not None:
                seg["description"].append(" | ".join(_split_cells(line)))
            continue
        # normal：描述文本
        seg["description"].append(" | ".join(_split_cells(line)))

    _close_segment()
    return members, roles, segments


def load_team_table() -> Dict[str, Any]:
    """加载统一队伍-槽位表（data/offline/presets/team_table.json）。

    缓存绑定文件 mtime 自愈：外部进程（bat 更新脚本/手动重建）覆写表文件后，
    下次查询自动重读——不依赖插件进程内的 reload_team_table() 调用
    （实例事故：独立进程重建新表后，运行中进程的内存缓存仍是旧表，
    priority 字段全空导致热门过滤静默失效）。mtime 为 None 视为测试注入，信任缓存。

    缺失或损坏时记录警告日志并返回空表结构 {"rows": [], "report": {}}，
    不抛出异常。
    """
    global _team_table_cache, _team_table_cache_mtime
    table_path = _DATA_DIR / "offline" / "presets" / "team_table.json"
    try:
        mtime = table_path.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _team_table_cache is not None and (
        _team_table_cache_mtime is None or _team_table_cache_mtime == mtime
    ):
        return _team_table_cache

    if not table_path.is_file():
        logger.warning("team_table.json not found: %s", table_path)
        _team_table_cache = {"rows": [], "report": {}}
        _team_table_cache_mtime = mtime
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
    _team_table_cache_mtime = mtime
    return _team_table_cache


def reload_team_table() -> None:
    """清除统一队伍-槽位表缓存，强制下次查询重新读盘。"""
    global _team_table_cache, _team_table_cache_mtime
    _team_table_cache = None
    _team_table_cache_mtime = None


def preheat_services() -> None:
    """预热共享服务：字典单例（含替换引擎编译）与统一队伍-槽位表。

    供插件 on_load 后台调用——消除首次用户查询的秒级冷启动
    （字典 8.8MB JSON 解析 + TermReplacer 编译 + 表加载），
    实例日志证实暖态单次链路仅 0.05-0.3s。
    """
    _get_lookup()
    load_team_table()


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


# 纯属性泛查元素词表（中文元素字 → team_table 的 element 代码；"土" 为 "地" 俗称）
_ELEMENT_QUERY_KEYWORDS = {
    "水": "aqua",
    "火": "ignis",
    "光": "lux",
    "地": "terra",
    "土": "terra",
    "暗": "umbra",
    "风": "ventus",
}

# 触发属性泛查的队伍类后缀词（元素字必须与其同现才判为属性泛查）
_ELEMENT_QUERY_SUFFIXES = ("队", "系", "属性")


def detect_element_query(text: str) -> Optional[str]:
    """纯属性泛查检测：文本含「元素词 + 队/系/属性」组合时返回元素代码，否则 None。

    仅在问句未命中任何角色名时调用——风影 等角色名含元素字，由角色提取先行拦截，
    不会误入本检测。多元素同现（如"风水队"）按词表顺序取首个。
    """
    if not any(w in text for w in _ELEMENT_QUERY_SUFFIXES):
        return None
    for kw, elem in _ELEMENT_QUERY_KEYWORDS.items():
        if kw in text:
            return elem
    return None


def _row_block_key(row: dict) -> Optional[tuple]:
    """行的区块归组键 (element, block)；无 guide_ref（孤码行）返回 None。"""
    ref = row.get("guide_ref")
    if not ref:
        return None
    return (row.get("element", ""), str(ref.get("block", "")))


def apply_priority_filter(rows: list, question: str) -> list:
    """按热门优先级（row.priority）过滤命中行——组级过滤，同区块行整组保留。

    统一规则（单角色/多角色/纯属性泛查一致，与 team_priority.txt 标记约定对齐；
    当前每属性仅 2 个热门队，不补齐则结果过少）：
    - 问句含全量触发词（全部/所有/完整/详细）→ 不过滤原样返回
    - 热门行全保留（不做数量上限检查）
    - 热门区块数 < 3 时按表序补冷门区块至 3 个；0 热门行时回退全量
      （角色无热门队时不至空结果）

    过滤在 find_team_rows 之后、query_how_rows 之前执行。
    """
    if any(w in question for w in ("全部", "所有", "完整", "详细")):
        return rows
    hot = [r for r in rows if r.get("priority")]
    if not hot:
        return rows
    # 热门区块不足 3 时按表序补冷门区块（组级补齐，保组完整）
    keep = {key for key in (_row_block_key(r) for r in hot) if key}
    for r in rows:
        if r.get("priority"):
            continue
        key = _row_block_key(r)
        if key is None or key in keep:
            continue
        if len(keep) >= 3:
            break
        keep.add(key)
    return [r for r in rows if _row_block_key(r) in keep]


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
            cleaned = _TOP_ANCHOR_RE.sub("", line).strip(" |").strip()
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


# ---------------------------------------------------------------------------
# 输出装配层噪声过滤
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

    return "\n".join(lines)


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


# =====================================================================
# ss-data 数据集 material 渲染器与中文反查
# =====================================================================

def _cn_by_en(lookup: Any, en_name: str) -> str:
    """用 lookup.lookup_term(en_name) 反查中文名；查不到原样返回 en_name。

    lookup_term 内部已覆盖 main_dict 精确命中、names.json 大小写索引与
    自定义别名三层解析，故无需在本函数重复访问其私有索引（旧兜底与
    lookup_term 的解析路径完全重合，恒不可达）。
    """
    if not en_name:
        return ""
    if not isinstance(en_name, str):
        return str(en_name)
    if lookup is None:
        return en_name
    try:
        res = lookup.lookup_term(en_name)
        if res and isinstance(res, dict) and res.get("cn"):
            return res["cn"]
    except Exception:
        pass
    return en_name


# 面板数值渲染共用常量：三围中文标签（第一优先）与噪声键（对 LLM 无意义的元数据列）
_STAT_LABELS: Tuple[Tuple[str, str], ...] = (("HP", "生命"), ("ATK", "攻击"), ("DEF", "防御"))
# 秘纹面板只有生命/攻击两围（无防御），单独一组避免渲染出空的防御项
_STAT_LABELS_HP_ATK: Tuple[Tuple[str, str], ...] = (("HP", "生命"), ("ATK", "攻击"))
_STAT_SKIP_KEYS: Tuple[str, ...] = ("Type", "HP Bar", "Score", "Max Score")


def _stat_parts(
    stat_dict: dict,
    label_keys: Tuple[Tuple[str, str], ...] = _STAT_LABELS,
    key_cn: Optional[Callable[[str], str]] = None,
    skip_keys: Tuple[str, ...] = (),
    limit: int = 3,
) -> list:
    """从面板数值 dict 提取展示片段（角色/秘纹/首领三源共用）。

    第一优先按 label_keys 中的键取中文标签值（如 HP/ATK/DEF 三围）；一个都不命中时
    按原键回退（最多 limit 项），回退键经 key_cn 转中文（None=原样），并跳过 skip_keys
    噪声键。统一此处逻辑可避免三源面板各自实现漂移。
    """
    parts = [f"{label} {stat_dict[k]}" for k, label in label_keys if k in stat_dict]
    if not parts:
        conv = key_cn or (lambda k: k)
        for k, v in stat_dict.items():
            if k in skip_keys:
                continue
            parts.append(f"{conv(k)} {v}")
            if len(parts) >= limit:
                break
    return parts


def _render_skill_block(sk: dict, label: str, lookup: Any) -> list:
    """渲染单个技能块（名称/描述/数值表），角色四技能与秘纹主/副技能共用。

    取值优先级：nameCN/descCN（官方中文）→ name/desc 经 _cn_by_en 反查；
    params 为 list 时以 ' / ' 连接，否则原样输出。
    """
    lines: list[str] = []
    name = sk.get("nameCN") or _cn_by_en(lookup, sk.get("name", ""))
    desc = sk.get("descCN") or sk.get("desc", "")
    lines.append(f"【{label}】{name}")
    if desc:
        lines.append(f"描述：{desc}")
    params = sk.get("params")
    if params:
        if isinstance(params, list):
            lines.append(f"数值表：{' / '.join(str(p) for p in params)}")
        else:
            lines.append(f"数值表：{params}")
    return lines


# 游戏等级上限：旅人与秘纹均为 90 级。ss-data 原始表越过上限仍残留内容——
# 旅人 stat 尾部含内部溢出条目（Level 91，数值与 90 级相同），
# 秘纹 stat 无 Level 字段且含 8 个突破重复行（90 级 + 8 = 98 行）。
_MAX_LEVEL = 90
# 突破（升阶）等级：每 10 级一次；每个突破点在 stat 表内于同一等级占两行
# （突破前 / 突破后），这是「下标 → 等级」非线性映射的成因。
_ASCENSION_LEVELS: Tuple[int, ...] = (10, 20, 30, 40, 50, 60, 70, 80)


def _stat_levels(stat_list: list) -> list:
    """求 stat 数组各行的等级标签。

    优先取条目自带 Level 字段（旅人）；缺失时按突破规则由下标反推（秘纹
    stat 无 Level 字段，不能以 idx+1 当等级）。突破点在序列中连续出现两次
    （突破前 / 突破后），故 90 级 + 8 突破 = 98 项，与秘纹 stat 条目数一致。
    """
    fallback: list = []
    level = 1
    while len(fallback) < len(stat_list):
        fallback.append(level)
        if level in _ASCENSION_LEVELS:
            fallback.append(level)
        level += 1
    levels: list = []
    for i, entry in enumerate(stat_list):
        lv = entry.get("Level") if isinstance(entry, dict) else None
        if not isinstance(lv, int):
            lv = fallback[i] if i < len(fallback) else i + 1
        levels.append(lv)
    return levels


def _render_stat_rows(
    stat_list: list,
    lookup: Any,
    label_keys: Tuple[Tuple[str, str], ...],
) -> list:
    """渲染面板数值「首级 + 满级」两行（角色 / 秘纹共用）。

    满级行按游戏等级上限定位，不得直取末行：ss-data 旅人 stat 尾部残留
    Level 91 溢出条目、秘纹末行下标 97 实为 90 级（其前有 8 个突破重复行），
    直取 len-1 会显示 91 级 / 98 级并可能取到越界数据。
    """
    levels = _stat_levels(stat_list)
    max_idx = len(stat_list) - 1
    for i in range(len(stat_list) - 1, -1, -1):
        if levels[i] <= _MAX_LEVEL:
            max_idx = i
            break
    rows: list = []
    shown: set = set()
    for idx in (0, max_idx):
        if idx in shown or not (0 <= idx < len(stat_list)):
            continue
        shown.add(idx)
        entry = stat_list[idx]
        if not isinstance(entry, dict):
            continue
        parts = _stat_parts(entry, label_keys=label_keys, key_cn=lambda k: _cn_by_en(lookup, k))
        if not parts:
            continue
        rows.append(f"  {levels[idx]}级：{' | '.join(parts)}")
    return rows


def _build_character_material(
    st: StelladbFetcher,
    num_id: str,
    lookup: Any,
    modules: Optional[Collection[str]] = None,
) -> Tuple[str, bool]:
    """渲染角色官方中文资料（来源于 ss-data 的 character.json）。

    返回 (material_text, True)；若 dataset 缺失或对应 id 不存在则返回 ("", False)。
    modules 给定（模块化查询）时只渲染 overview（恒出）+ modules 命中的区块，
    overview 在模块化模式下补 风格/势力/CV/生日/获取途径，并附带 details 面板
    与升级材料、talents 天赋轶闻；modules=None 保持既有全量输出逐字节不变。
    """
    if not st:
        return "", False
    dataset = st.fetch_ssdata_dataset("character")
    if not dataset or not isinstance(dataset, dict):
        return "", False

    char = dataset.get(num_id) or dataset.get(str(num_id))
    if not char or not isinstance(char, dict):
        return "", False

    lines: list[str] = []

    # overview：名称/星级/属性/职业 恒渲染；模块化查询时补 风格/势力/CV/生日/获取途径
    en_name = char.get("name", "")
    cn_name = _cn_by_en(lookup, en_name)
    if en_name and en_name != cn_name:
        lines.append(f"【角色】{cn_name}（{en_name}）")
    else:
        lines.append(f"【角色】{cn_name or en_name}")

    star = char.get("star")
    if star:
        lines.append(f"星级：{star}星")

    elem = char.get("element", "")
    elem_cn = _element_cn(elem)
    if elem_cn:
        lines.append(f"属性：{elem_cn}")

    cls = char.get("class", "")
    cls_cn = _cn_by_en(lookup, cls)
    if cls_cn:
        lines.append(f"职业：{cls_cn}")

    if modules is not None:
        # 模块化 overview 扩展字段（缺失省略；style/force/source 经字典反查中文名）
        style = char.get("style", "")
        if style:
            lines.append(f"风格：{_cn_by_en(lookup, style)}")
        force = char.get("force", "")
        if force:
            lines.append(f"势力：{_cn_by_en(lookup, force)}")
        cv = char.get("cnCv") or char.get("jpCv", "")
        if cv:
            lines.append(f"CV：{cv}")
        birthday = char.get("birthday", "")
        if birthday:
            lines.append(f"生日：{birthday}")
        source = char.get("source", [])
        if isinstance(source, list) and source:
            source_cn = [_cn_by_en(lookup, s) for s in source if s]
            if source_cn:
                lines.append(f"获取途径：{'、'.join(source_cn)}")

    # details 模块（仅模块化路径新增）：面板数值（首级 + 90 级满级，不取 fixedStat）
    # + 升级材料（upgrade）
    if modules is not None and "details" in modules:
        stat_list = char.get("stat", [])
        if isinstance(stat_list, list) and stat_list:
            lines.append("【面板数值】")
            lines.extend(_render_stat_rows(stat_list, lookup, _STAT_LABELS))
        upgrade = char.get("upgrade", [])
        if isinstance(upgrade, list) and upgrade:
            lines.append("【升级材料】")
            for idx, item in enumerate(upgrade, start=1):
                if isinstance(item, dict):
                    parts = [f"{_cn_by_en(lookup, k)}: {v}" for k, v in item.items()]
                    lines.append(f"  阶段{idx}：{'、'.join(parts)}")

    # 技能块：normalAtk / skill / supportSkill / ultimate
    if modules is None or "skills" in modules:
        for sk_key, sk_label in (
            ("normalAtk", "普攻"),
            ("skill", "主控技能"),
            ("supportSkill", "援护技能"),
            ("ultimate", "绝招"),
        ):
            sk = char.get(sk_key)
            if sk and isinstance(sk, dict):
                lines.extend(_render_skill_block(sk, sk_label, lookup))

    # 潜能：potential.mainCore / mainNormal / common / supportCore / supportNormal
    if modules is None or "potentials" in modules:
        pot = char.get("potential")
        if pot and isinstance(pot, dict):
            pot_configs = [
                ("mainCore", "主控核心潜能"),
                ("mainNormal", "主控普通潜能"),
                ("common", "通用潜能"),
                ("supportCore", "援护核心潜能"),
                ("supportNormal", "援护普通潜能"),
            ]
            for pk_key, pk_label in pot_configs:
                pot_list = pot.get(pk_key)
                if pot_list and isinstance(pot_list, list):
                    lines.append(f"【{pk_label}】")
                    for item in pot_list:
                        if isinstance(item, dict):
                            p_name = item.get("nameCN") or _cn_by_en(lookup, item.get("name", ""))
                            p_desc = item.get("descCN") or item.get("desc", "")
                            if p_name or p_desc:
                                lines.append(f"  {p_name}：{p_desc}")

    # 天赋：talent；模块化查询时追加天赋轶闻（Item.{num_id}.3 命中且含 cn 才输出）
    if modules is None or "talents" in modules:
        talents = char.get("talent")
        if talents and isinstance(talents, list):
            lines.append("【天赋】")
            for t in talents:
                if isinstance(t, dict):
                    if t.get("nameCN") or t.get("descCN"):
                        t_name = t.get("nameCN") or _cn_by_en(lookup, t.get("name", ""))
                        t_desc = t.get("descCN") or t.get("desc", "")
                        lines.append(f"  {t_name}：{t_desc}")
                    elif t.get("boost") and isinstance(t["boost"], list):
                        for b in t["boost"]:
                            if isinstance(b, dict) and (b.get("nameCN") or b.get("descCN")):
                                b_name = b.get("nameCN") or _cn_by_en(lookup, b.get("name", ""))
                                b_desc = b.get("descCN") or b.get("desc", "")
                                lines.append(f"  {b_name}：{b_desc}")
        if modules is not None:
            # 天赋轶闻：查询失败不兜底造数据，命中且含中文才追加
            flavor = lookup.lookup_term(f"Item.{num_id}.3")
            if flavor and isinstance(flavor, dict) and flavor.get("cn"):
                lines.append(f"天赋轶闻：{flavor['cn']}")

    # 礼物：loveGift / hateGift
    if modules is None or "gifts" in modules:
        love_gifts = char.get("loveGift", [])
        if isinstance(love_gifts, list) and love_gifts:
            love_cn = [_cn_by_en(lookup, g) for g in love_gifts if g]
            if love_cn:
                lines.append(f"喜好礼物：{'、'.join(love_cn)}")
        hate_gifts = char.get("hateGift", [])
        if isinstance(hate_gifts, list) and hate_gifts:
            hate_cn = [_cn_by_en(lookup, g) for g in hate_gifts if g]
            if hate_cn:
                lines.append(f"厌恶礼物：{'、'.join(hate_cn)}")

    # 约会分支：date
    if modules is None or "dates" in modules:
        dates = char.get("date", [])
        if isinstance(dates, list) and dates:
            lines.append("【约会分支】")
            for d in dates:
                if isinstance(d, dict):
                    d_name = _cn_by_en(lookup, d.get("name", ""))
                    d_clue = _cn_by_en(lookup, d.get("clue", ""))
                    d_choice = _cn_by_en(lookup, d.get("secondChoice", ""))
                    parts = []
                    if d_name:
                        parts.append(f"事件：{d_name}")
                    if d_clue:
                        parts.append(f"解锁线索：{d_clue}")
                    if d_choice:
                        parts.append(f"分支选择：{d_choice}")
                    if parts:
                        lines.append(f"  {' | '.join(parts)}")

    raw_text = "\n".join(lines)
    clean_text = strip_game_markup(raw_text)
    return clean_text, True


def _build_disc_material(
    st: StelladbFetcher,
    num_id: str,
    lookup: Any,
    modules: Optional[Collection[str]] = None,
) -> Tuple[str, bool]:
    """渲染秘纹官方中文资料（来源于 ss-data 的 disc.json）。

    返回 (material_text, True)；若 dataset 缺失或对应 id 不存在则返回 ("", False)。
    modules 给定（模块化查询）时只渲染 overview（恒出）+ modules 命中的区块
    （details=面板数值+潜能加成+升级消耗；melody=主技能+副技能+支援旋律），
    dupe/upgrade 与副技能的条件耦合仅存在于 modules=None 全量路径；
    modules=None 保持既有全量输出逐字节不变。
    """
    if not st:
        return "", False
    dataset = st.fetch_ssdata_dataset("disc")
    if not dataset or not isinstance(dataset, dict):
        return "", False

    disc = dataset.get(num_id) or dataset.get(str(num_id))
    if not disc or not isinstance(disc, dict):
        return "", False

    lines: list[str] = []

    # overview：名称/星级/属性/标签/适配角色 恒渲染；模块化查询时补 获取途径（source）
    en_name = disc.get("name", "")
    cn_name = _cn_by_en(lookup, en_name)
    if en_name and en_name != cn_name:
        lines.append(f"【秘纹】{cn_name}（{en_name}）")
    else:
        lines.append(f"【秘纹】{cn_name or en_name}")

    star = disc.get("star")
    if star:
        lines.append(f"星级：{star}星")

    elem = disc.get("element", "")
    elem_cn = _element_cn(elem)
    if elem_cn:
        lines.append(f"属性：{elem_cn}")

    tags = disc.get("tag", [])
    if isinstance(tags, list) and tags:
        tag_cn = [_cn_by_en(lookup, t) for t in tags if t]
        if tag_cn:
            lines.append(f"标签：{'、'.join(tag_cn)}")

    chars = disc.get("char", [])
    if isinstance(chars, list) and chars:
        char_cn = [_cn_by_en(lookup, c) for c in chars if c]
        if char_cn:
            lines.append(f"适配角色：{'、'.join(char_cn)}")

    if modules is not None:
        # 模块化 overview 扩展字段（缺失省略；source 经字典反查中文名）
        source = disc.get("source", [])
        if isinstance(source, list) and source:
            source_cn = [_cn_by_en(lookup, s) for s in source if s]
            if source_cn:
                lines.append(f"获取途径：{'、'.join(source_cn)}")

    # melody 模块（主技能 + 副技能）：副技能判定仅在 modules=None 全量路径用于
    # dupe/upgrade 的既有条件耦合，模块化路径各区块独立渲染
    has_secondary = False
    if modules is None or "melody" in modules:
        main_skill = disc.get("mainSkill")
        if main_skill and isinstance(main_skill, dict):
            lines.extend(_render_skill_block(main_skill, "主技能", lookup))

        for sec_key, sec_label in [("secondarySkill1", "副技能1"), ("secondarySkill2", "副技能2")]:
            sec_skill = disc.get(sec_key)
            if sec_skill and isinstance(sec_skill, dict):
                has_secondary = True
                lines.extend(_render_skill_block(sec_skill, sec_label, lookup))

    # details 模块（模块化新增）：面板数值（首级 + 90 级满级；秘纹 stat 无 Level
    # 字段，等级按下标按突破规则反推，不可用 idx+1）
    if modules is not None and "details" in modules:
        stat_list = disc.get("stat", [])
        if isinstance(stat_list, list) and stat_list:
            lines.append("【面板数值】")
            lines.extend(_render_stat_rows(stat_list, lookup, _STAT_LABELS_HP_ATK))

    # 潜能加成（dupe）+ 升级消耗（upgrade）：modules=None 保持"无副技能才展示"的
    # 既有耦合行为；模块化路径归属 details 区块，独立渲染不再受副技能抑制
    render_dupe_upgrade = (not has_secondary) if modules is None else ("details" in modules)
    if render_dupe_upgrade:
        dupe = disc.get("dupe")
        if dupe and isinstance(dupe, list):
            lines.append("【潜能加成】")
            for idx, item in enumerate(dupe, start=1):
                if isinstance(item, dict):
                    parts = [f"{_cn_by_en(lookup, k)} +{v}" for k, v in item.items()]
                    lines.append(f"  第{idx}层：{'、'.join(parts)}")
        upgrade = disc.get("upgrade")
        if upgrade and isinstance(upgrade, list):
            lines.append("【升级消耗】")
            for idx, item in enumerate(upgrade, start=1):
                if isinstance(item, dict):
                    parts = [f"{_cn_by_en(lookup, k)}: {v}" for k, v in item.items()]
                    lines.append(f"  阶段{idx}：{'、'.join(parts)}")

    # 支援旋律（supportNote）：melody 模块尾部（modules=None 全量路径保持
    # 既有"主技能→副技能→潜能/升级→支援旋律"顺序逐字节不变）
    if modules is None or "melody" in modules:
        support_notes = disc.get("supportNote")
        if support_notes and isinstance(support_notes, list):
            lines.append("【支援旋律】")
            for item in support_notes:
                if isinstance(item, dict):
                    parts = [f"{_cn_by_en(lookup, k)} (等级 {v})" for k, v in item.items()]
                    lines.append(f"  {'、'.join(parts)}")
                elif isinstance(item, str):
                    lines.append(f"  {_cn_by_en(lookup, item)}")

    raw_text = "\n".join(lines)
    clean_text = strip_game_markup(raw_text)
    return clean_text, True


# what 模块词表：路由 = 查哪类（_route_what_keywords 先于实体解析决定走哪条
# 渲染链路，banner/blitz/disc 概念列表整页输出）；模块 = 问哪块（本词表 + 下方
# _detect_what_modules），实体确定后按问句命中哪些模块区块。二者正交，模块检测
# 不反向影响路由。"属性/弱点"等泛览类词天然归 overview（overview 无触发词、
# 恒在返回集），不设为任何模块触发词。
_MODULE_KEYWORDS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "character": {
        "details": ("数值", "面板", "stats", "升级材料", "突破材料", "培养材料", "升满", "满级", "毕业"),
        "skills": ("技能", "普攻", "大招", "绝招", "必杀", "援护", "主控", "奥义", "技能升级"),
        "potentials": ("潜能", "核心潜能", "共鸣"),
        "talents": ("天赋", "命座", "命之座", "被动特性"),
        "gifts": ("礼物", "喜好", "喜欢", "厌恶", "讨厌", "赠送", "送什么"),
        "dates": ("约会", "邀约", "剧情", "好感", "分支", "攻略线"),
    },
    "disc": {
        "details": ("数值", "面板", "stats", "升级材料", "突破材料", "升满", "满级"),
        "melody": ("旋律", "主旋律", "音符", "支援音符", "曲调", "技能"),
    },
    "monster": {
        "stats": ("数值", "面板", "血量", "生命", "攻击", "防御", "stats", "属性值"),
        # 注意：不得含"怎么打/攻略/打法"——那是 how 工具（stellasora_how 配队/打法）
        # 的意图词，加入会造成 what/how 意图竞争、误路由到怪物机制
        "mechanic": ("机制", "技能", "难点", "核心机制"),
    },
}


def _detect_what_modules(question: str, entity_type: str) -> Set[str]:
    """检测问句命中的 what 渲染模块集合（overview 常驻基底）。

    按 entity_type 查 _MODULE_KEYWORDS 词表：对每个模块的触发词做 question.lower()
    子串匹配（大小写不敏感），同类型内多模块命中取并集；overview 无触发词、
    恒在返回集；空问句、零命中或未知 entity_type → 仅 {"overview"}。
    """
    modules: Set[str] = {"overview"}
    if not question:
        return modules
    lowered = question.lower()
    for module_name, keywords in _MODULE_KEYWORDS.get(entity_type, {}).items():
        if any(kw.lower() in lowered for kw in keywords):
            modules.add(module_name)
    return modules


def _route_what_keywords(term: str) -> Optional[str]:
    """根据关键词路由 what 查询意图。

    banner: 卡池 / 池子 / up池 / UP池
    blitz: 联合讨伐 / 当期讨伐 / 讨伐 / boss / blitz（先排除 raid 意图）
    disc: 秘纹 / 旋律（严格排除纹章——那是 how 侧词汇）
    其它: None
    """
    if not term:
        return None
    lower_term = term.lower()
    # 1. banner 关键词
    if any(k in lower_term for k in ("卡池", "池子", "up池")):
        return "banner"
    # 2. blitz 关键词（当期联合讨伐）。boss/blitz 等词也会出现在终焉绝响（FE raid）
    #    语境中，命中判定前先排除 raid 意图——那些查询应落入首领怪物路由而非讨伐意图
    if not any(k in lower_term for k in ("终焉", "绝响", "raid")):
        if any(k in lower_term for k in ("联合讨伐", "当期讨伐", "讨伐", "boss", "blitz")):
            return "blitz"
    # 3. disc 关键词（注意不得命中 纹章——那是 how 侧词汇）
    if "纹章" in lower_term:
        return None
    if any(k in lower_term for k in ("秘纹", "旋律")):
        return "disc"
    return None


def _build_banner_material(
    st: StelladbFetcher,
    lookup: Any,
) -> Tuple[str, bool]:
    """渲染卡池资讯（进行中 + 最近 1 期已结束，最多 3 期）。

    返回 (material_text, True)；若 dataset 缺失返回 ("", False)。
    """
    if not st:
        return "", False
    dataset = st.fetch_ssdata_dataset("gacha")
    if not dataset or not isinstance(dataset, dict):
        return "", False

    char_dataset = st.fetch_ssdata_dataset("character") or {}
    disc_dataset = st.fetch_ssdata_dataset("disc") or {}

    def _parse_dt(dt_str: str) -> Optional[datetime]:
        if not dt_str:
            return None
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _format_date(dt_str: str) -> str:
        dt = _parse_dt(dt_str)
        if dt:
            return dt.strftime("%Y-%m-%d")
        return dt_str[:10] if len(dt_str) >= 10 else dt_str

    banners = [b for b in dataset.values() if isinstance(b, dict)]
    sorted_banners = sorted(
        banners,
        key=lambda b: str(b.get("startTime", "")),
        reverse=True,
    )
    if not sorted_banners:
        return "", False

    now = datetime.now(timezone.utc)
    ongoing: list[dict] = []
    ended: list[dict] = []
    for b in sorted_banners:
        st_dt = _parse_dt(b.get("startTime", ""))
        ed_dt = _parse_dt(b.get("endTime", ""))
        if st_dt and ed_dt:
            if st_dt <= now <= ed_dt:
                ongoing.append(b)
            elif ed_dt < now:
                ended.append(b)
        else:
            ended.append(b)

    # 取当前进行中 + 最近 1 期已结束（共最多 3 期）
    selected_banners: list[dict] = list(ongoing)
    if ended:
        selected_banners.append(ended[0])
    if not ongoing and ended:
        selected_banners = ended[:min(3, len(ended))]
    elif not selected_banners:
        selected_banners = sorted_banners[:3]
    selected_banners = selected_banners[:3]

    lines: list[str] = ["【卡池资讯】"]
    for b in selected_banners:
        b_type = b.get("type", "")
        en_name = b.get("name", "")
        cn_name = _cn_by_en(lookup, en_name)
        st_date = _format_date(b.get("startTime", ""))
        ed_date = _format_date(b.get("endTime", ""))
        type_label = "角色卡池" if b_type == "character" else ("秘纹卡池" if b_type == "disc" else f"{b_type}卡池")
        if cn_name and cn_name != en_name:
            lines.append(f"【{type_label}】{cn_name}（{en_name}，{st_date} ~ {ed_date}）")
        else:
            lines.append(f"【{type_label}】{cn_name or en_name}（{st_date} ~ {ed_date}）")

        for star_label, up_key in [("5★ UP", "rateUp5Star"), ("4★ UP", "rateUp4Star")]:
            up_list = b.get(up_key, [])
            if isinstance(up_list, list) and up_list:
                item_descs = []
                for item in up_list:
                    if not isinstance(item, dict):
                        continue
                    i_id = str(item.get("id", ""))
                    i_name = item.get("name", "")
                    if b_type == "character":
                        c_info = char_dataset.get(i_id, {}) if isinstance(char_dataset, dict) else {}
                        c_en = c_info.get("name") or i_name
                        i_cn = _cn_by_en(lookup, c_en)
                    elif b_type == "disc":
                        d_info = disc_dataset.get(i_id, {}) if isinstance(disc_dataset, dict) else {}
                        d_en = d_info.get("name") or i_name
                        i_cn = _cn_by_en(lookup, d_en)
                    else:
                        i_cn = _cn_by_en(lookup, i_name)
                    elem = item.get("element", "")
                    elem_cn = _element_cn(elem)
                    if elem_cn:
                        item_descs.append(f"{i_cn}（{elem_cn}）")
                    else:
                        item_descs.append(f"{i_cn}")
                if item_descs:
                    lines.append(f"  - {star_label}：{'、'.join(item_descs)}")

    raw_text = "\n".join(lines)
    clean_text = strip_game_markup(raw_text)
    return clean_text, True


def _build_disc_list_material(
    st: StelladbFetcher,
    lookup: Any,
) -> Tuple[str, bool]:
    """渲染秘纹列表概要（前 20 条）。

    返回 (material_text, True)；若 dataset 缺失返回 ("", False)。
    """
    if not st:
        return "", False
    dataset = st.fetch_ssdata_dataset("disc")
    if not dataset or not isinstance(dataset, dict):
        return "", False

    def _get_id(item: dict) -> int:
        try:
            return int(item.get("id", 0))
        except (ValueError, TypeError):
            return 0

    sorted_discs = sorted(
        [d for d in dataset.values() if isinstance(d, dict)],
        key=_get_id,
    )
    selected = sorted_discs[:20]
    if not selected:
        return "", False

    lines: list[str] = ["【秘纹列表（前20条）】"]
    for d in selected:
        en_name = d.get("name", "")
        cn_name = _cn_by_en(lookup, en_name)
        star = d.get("star", 0)
        elem = d.get("element", "")
        elem_cn = _element_cn(elem)
        star_str = f"{star}星" if star else ""
        elem_str = f"{elem_cn}属性" if elem_cn else ""
        attr_parts = [p for p in [star_str, elem_str] if p]
        attr_desc = f"（{' / '.join(attr_parts)}）" if attr_parts else ""
        lines.append(f"- {cn_name}{attr_desc}")

    raw_text = "\n".join(lines)
    clean_text = strip_game_markup(raw_text)
    return clean_text, True


def _match_in_dataset(
    dataset: dict,
    norm_term: str,
    lookup: Any,
    raw_term: str = "",
) -> Optional[str]:
    """在单个 ssdata 数据集内做归一化子串与中文反查匹配，返回命中 id 或 None。

    raw_term 为原始查询词（未归一化），供 duel 首领同名???变体的原串级匹配：
    归一化会吞掉全角？，无法区分「猫眼」与「猫眼？？？」，故内层名反查结果
    必须回到原串级判定并带长度阈值，避免裸角色名劫持到 boss。
    """
    for m_id, m_val in dataset.items():
        if not isinstance(m_val, dict):
            continue
        raw_name = m_val.get("name", "")
        # 去除 [方括号] 标点，规范化
        norm_name = _normalize(raw_name)
        if norm_term in norm_name:
            return str(m_id)
        # 兼容中文反查名命中
        if lookup:
            cn_name = _cn_by_en(lookup, raw_name)
            if cn_name != raw_name:
                norm_cn = _normalize(cn_name)
                if norm_term in norm_cn:
                    return str(m_id)
            elif raw_term:
                # duel 首领名形如 "[Cat-stle Siege] Chaton ???"：整名字典反查
                # 落空，剥赛季前缀（[...]）取内层名 "Chaton ???" 二次反查官方中文
                # 收录名「猫眼？？？」。原串级判定防误桥——「猫眼？？？」归一化
                # 后与「猫眼」同形，故查询原串必须与中文收录名相等/包含它，反向
                # 包含仅当查询长度 >=4（裸「猫眼」仅 2 字符直接拒绝）。
                inner = re.sub(r"^\[[^\]]*\]\s*", "", raw_name)
                if inner != raw_name:
                    cn_full = _cn_by_en(lookup, inner)
                    if cn_full != inner:
                        clean_term = raw_term.strip()
                        if cn_full == clean_term or cn_full in clean_term \
                                or (clean_term in cn_full and len(clean_term) >= 4):
                            return str(m_id)
    return None


def _match_monster(
    term: str,
    lookup: Any,
    st: StelladbFetcher,
) -> Optional[str]:
    """匹配首领怪物 ID（raid ∪ duel 两数据源）。

    先通过 lookup 查 MonsterManual 且要求 id 存在于 raid/duel 任一键集；
    若未命中或不在任一键集，落入 raid → duel 数据集的大小写不敏感子串/精确 id
    匹配（blitz 当期讨伐由 _match_blitz_boss 单独兜底，不并入本函数）。
    """
    if not term or not st:
        return None

    raid_dataset = st.fetch_ssdata_dataset("raid")
    if not raid_dataset or not isinstance(raid_dataset, dict):
        return None
    duel_dataset = st.fetch_ssdata_dataset("duel")
    if not duel_dataset or not isinstance(duel_dataset, dict):
        duel_dataset = {}

    # 合并键集：MonsterManual 反查 / 精确 id 命中 raid/duel 任一即算命中
    all_ids = set(raid_dataset) | set(duel_dataset)

    # 1. 尝试 lookup_term 反查 MonsterManual
    if lookup:
        res = lookup.lookup_term(term)
        if res and isinstance(res, dict) and res.get("cat") == "MonsterManual":
            raw_id = str(res.get("id", ""))
            parts = raw_id.split(".")
            if len(parts) > 1:
                num_id = parts[1]
                if num_id in all_ids:
                    return num_id

    # 2. 精确 id 匹配（raid/duel 任一键集）
    clean_term = term.strip()
    if clean_term in all_ids:
        return clean_term

    norm_term = _normalize(clean_term)
    if not norm_term:
        return None

    # 3. 归一化子串/中文反查匹配：先 raid（保持既有匹配语义），后 duel
    hit = _match_in_dataset(raid_dataset, norm_term, lookup, clean_term)
    if hit is not None:
        return hit
    if duel_dataset:
        return _match_in_dataset(duel_dataset, norm_term, lookup, clean_term)
    return None


def _monster_stat_dict(boss: dict) -> dict:
    """从首领数据解包面板数值（raid/duel/blitz 三源统一为平铺 dict）。

    duel: stat 直接平铺 dict；blitz: stat list[平铺 dict]（取首项）；
    raid: diff[].stat 两层嵌套（[[{...}]]，取首个难度的首组）。无可用数据
    返回空 dict。
    """
    stat = boss.get("stat")
    if isinstance(stat, dict):
        return stat
    if isinstance(stat, list) and stat:
        first = stat[0]
        if isinstance(first, dict):
            return first
        if isinstance(first, list) and first and isinstance(first[0], dict):
            return first[0]
    diff = boss.get("diff")
    if isinstance(diff, list) and diff:
        d0 = diff[0]
        if isinstance(d0, dict):
            entries = d0.get("stat", [])
            if isinstance(entries, list) and entries:
                first = entries[0]
                if isinstance(first, list) and first and isinstance(first[0], dict):
                    return first[0]
                if isinstance(first, dict):
                    return first
    return {}


def _build_monster_material(
    st: StelladbFetcher,
    num_id: str,
    lookup: Any,
    modules: Optional[Collection[str]] = None,
) -> Tuple[str, bool]:
    """渲染首领怪物官方中文资料（raid/duel/blitz 三源）。

    返回 (material_text, True)；num_id 在 raid → duel → blitz 三个数据集中均不
    存在返回 ("", False)。modules 给定（模块化查询）时渲染 overview（恒出）+
    modules 命中的 stats/mechanic 区块；modules=None 保持既有全量输出（raid
    fixture 输出逐字节不变，O8 兜底）。
    """
    if not st:
        return "", False

    # source 探测：依次查 raid → duel → blitz 数据集定位所在源
    monster: Optional[dict] = None
    source = ""
    for src in ("raid", "duel", "blitz"):
        dataset = st.fetch_ssdata_dataset(src)
        if not dataset or not isinstance(dataset, dict):
            continue
        m = dataset.get(num_id) or dataset.get(str(num_id))
        if isinstance(m, dict):
            monster = m
            source = src
            break
    if monster is None:
        return "", False

    lines: list[str] = []

    # overview：名称/类型/弱点/抗性 恒渲染（duel 类型经 _DUEL_TYPE_CN 局部映射翻译）
    en_name = monster.get("name", "")
    cn_name = _cn_by_en(lookup, en_name)
    if en_name and en_name != cn_name:
        lines.append(f"【首领】{cn_name}（{en_name}）")
    else:
        # duel 首领全名（如 "[Cat-stle Siege] Chaton ???"）字典整名反查常落空：
        # 剥赛季前缀取内层名 "Chaton ???" 二次反查官方中文收录名「猫眼？？？」，
        # 命中即以 CN+括注原名展示；raid/blitz 无赛季前缀结构，行为零改动
        display_cn = cn_name or en_name
        display_en = ""
        if source == "duel" and en_name:
            inner = re.sub(r"^\[[^\]]*\]\s*", "", en_name)
            if inner != en_name:
                inner_cn = _cn_by_en(lookup, inner)
                if inner_cn != inner:
                    display_cn = inner_cn
                    display_en = en_name
        if display_en:
            lines.append(f"【首领】{display_cn}（{display_en}）")
        else:
            lines.append(f"【首领】{display_cn or en_name}")

    m_type = monster.get("type", "")
    if m_type:
        if source == "duel":
            m_type = _DUEL_TYPE_CN.get(m_type, m_type)
        lines.append(f"类型：{m_type}")

    weak_to = monster.get("weakTo", [])
    if isinstance(weak_to, list) and weak_to:
        weak_cn = [_element_cn(w) for w in weak_to]
        lines.append(f"{_WEAK_LABEL}：{'、'.join(weak_cn)}")

    resist_to = monster.get("resistTo")
    if resist_to:
        if isinstance(resist_to, list):
            resist_cn = [_element_cn(r) for r in resist_to]
            lines.append(f"{_RESIST_LABEL}：{'、'.join(resist_cn)}")
        elif isinstance(resist_to, str) and resist_to.lower() != "none":
            lines.append(f"{_RESIST_LABEL}：{_element_cn(resist_to)}")
        else:
            lines.append(f"{_RESIST_LABEL}：无")
    else:
        lines.append(f"{_RESIST_LABEL}：无")

    # mechanic 区块：raid/blitz 平铺机制列表；duel 分组列表（组序渲染、affix 合并
    # 输出）；机制全量输出不截断
    if modules is None or "mechanic" in modules:
        mechanics = monster.get("mechanic", [])
        if isinstance(mechanics, list) and mechanics:
            lines.append("【首领机制】")
            if source == "duel":
                for group in mechanics:
                    # 真实 ss-data duel schema：每组为「裸列表」（同一 affix 族按
                    # diff 的多个变体）；同时兼容 {"affix": [...]} 包装形态
                    # （测试 fixture 形态），两种形态并存的 schema 演进不丢组
                    if isinstance(group, dict):
                        affixes = group.get("affix", [])
                    elif isinstance(group, list):
                        affixes = group
                    else:
                        continue
                    for a in affixes:
                        if not isinstance(a, dict):
                            continue
                        a_name = _cn_by_en(lookup, a.get("name", ""))
                        a_desc = a.get("desc", "")
                        lines.append(f"  - {a_name}")
                        if a_desc:
                            lines.append(f"    描述：{a_desc}")
            else:
                for m in mechanics:
                    if not isinstance(m, dict):
                        continue
                    m_name = _cn_by_en(lookup, m.get("name", ""))
                    m_desc = m.get("desc", "")
                    lines.append(f"  - {m_name}")
                    if m_desc:
                        lines.append(f"    描述：{m_desc}")

    # stats 区块：raid 按 diff 多难度渲染（既有两层解包）；duel/blitz 渲染平铺 stat
    if modules is None or "stats" in modules:
        if source == "raid":
            diff_list = monster.get("diff", [])
            if isinstance(diff_list, list) and diff_list:
                lines.append("【难度与属性】")
                for d in diff_list:
                    if not isinstance(d, dict):
                        continue
                    d_name = d.get("name", "")
                    stat_entries = d.get("stat", [])
                    stat_dict = {}
                    if stat_entries and isinstance(stat_entries, list):
                        first = stat_entries[0]
                        if isinstance(first, list) and first and isinstance(first[0], dict):
                            stat_dict = first[0]
                        elif isinstance(first, dict):
                            stat_dict = first

                    # 面板数值三围优先，退化时按原键取最多 3 项并跳过元数据噪声键
                    stats_parts = _stat_parts(stat_dict, skip_keys=_STAT_SKIP_KEYS)
                    stat_str = " | ".join(stats_parts) if stats_parts else "无属性详情"
                    lines.append(f"  - {d_name}：{stat_str}")
        else:
            # duel/blitz：平铺 stat dict 渲染面板（键经 _term_cn 翻译）
            stat_dict = _monster_stat_dict(monster)
            if stat_dict:
                lines.append("【面板数值】")
                stats_parts = _stat_parts(stat_dict, key_cn=_term_cn, skip_keys=_STAT_SKIP_KEYS)
                if stats_parts:
                    lines.append(f"  {' | '.join(stats_parts)}")

    raw_text = "\n".join(lines)
    clean_text = strip_game_markup(raw_text)
    return clean_text, True


def _normalize(s: str) -> str:
    """名称归一化（boss 名匹配用）：去除非字母数字（保留中英文字符与数字），转小写。"""
    return re.sub(r"[\W_]+", "", s.lower(), flags=re.UNICODE)


def _resolve_current_blitz(st: StelladbFetcher) -> Optional[Tuple[str, dict]]:
    """定位当期联合讨伐（Boss Blitz）赛季键与其 floor 数据块。

    返回 (bb_key, floor_dict)；无当期赛季时返回 None。bb_key 缺失时从
    fetch_leaderboard_meta 键表取 ^bb\\d+$ 且 NN 最大者离线降级（仅降级读取，
    不写数据库）。
    """
    if not st:
        return None
    meta = st.fetch_leaderboard_meta()
    if not meta or not isinstance(meta, dict):
        return None

    season = st.fetch_leaderboard_season()
    bb_key = (season or {}).get("BB_SEASON")
    if bb_key is None or bb_key not in meta:
        candidates = sorted(
            (k for k in meta if re.match(r"^bb\d+$", k)),
            key=lambda k: int(k[2:]),
            reverse=True,
        )
        bb_key = candidates[0] if candidates else None
    if bb_key is None:
        return None

    block = meta.get(bb_key)
    if not isinstance(block, dict):
        return None
    floors = block.get("floor")
    if not isinstance(floors, dict) or not floors:
        return None
    return bb_key, floors


def _build_blitz_material(
    st: StelladbFetcher,
    lookup: Any,
) -> Tuple[str, bool]:
    """渲染当期联合讨伐 boss 资料（两 boss 一次输出，紧凑两段式结构）。

    先输出【当期联合讨伐】header 与各 boss 概览行（序号/名称/类型/弱点/抗性/
    单分伤害），再逐 boss 输出【机制】明细（stat 摘要 + 机制列表）。
    boss 名与 meta floor name 不一致时以 meta 名为准并标注机制数据可能滞后、
    省略机制明细。返回 (material_text, True)；无当期赛季或 blitz dataset 缺失
    返回 ("", False)。
    """
    if not st:
        return "", False
    resolved = _resolve_current_blitz(st)
    if not resolved:
        return "", False
    bb_key, floors = resolved

    dataset = st.fetch_ssdata_dataset("blitz")
    if not dataset or not isinstance(dataset, dict):
        return "", False

    # 当期 floor 数字升序排列
    def _floor_sort_key(k: Any) -> int:
        try:
            return int(k)
        except (ValueError, TypeError):
            return 0

    ordered_floors = sorted(floors.keys(), key=_floor_sort_key)
    season_num = bb_key[2:] if bb_key.startswith("bb") else bb_key

    # 先统一计算每个 floor 的展示名与数据滞后标记（概览行与明细共用）
    entries: list[dict] = []
    for fl in ordered_floors:
        fl_info = floors[fl]
        fl_name = fl_info.get("name", "") if isinstance(fl_info, dict) else ""
        boss = dataset.get(str(fl)) or dataset.get(fl)
        entry: dict = {
            "floor": fl,
            "meta_name": fl_name,
            "boss": boss if isinstance(boss, dict) else None,
            "display_en": fl_name,
            "lagging": False,
            "en_name": "",
        }
        if entry["boss"] is not None:
            en_name = entry["boss"].get("name", "") or ""
            entry["en_name"] = en_name
            # 名称为空时退用 blitz 数据源名称展示
            entry["display_en"] = fl_name if fl_name else en_name
            # oracle M4：blitz 名与 meta floor name 一致性校验，不一致时机制数据视为滞后
            norm_boss = _normalize(en_name)
            norm_meta = _normalize(fl_name)
            if norm_boss and norm_meta and norm_boss != norm_meta:
                entry["lagging"] = True
                entry["display_en"] = fl_name
        entries.append(entry)

    lines: list[str] = [f"【当期联合讨伐 · 第{season_num}赛季】"]

    # 1. boss 概览行（序号/名称/类型/弱点/抗性/单分伤害）
    for idx, e in enumerate(entries, start=1):
        display_en = e["display_en"]
        cn_name = _cn_by_en(lookup, display_en)
        if cn_name and cn_name != display_en:
            name_str = f"{cn_name}（{display_en}）"
        else:
            name_str = cn_name or display_en

        overview_parts = [f"  {idx}. {name_str}"]
        if e["boss"] is not None:
            m_type = _term_cn(e["boss"].get("type", ""))
            if m_type:
                overview_parts.append(f"类型：{m_type}")
            weak_to = e["boss"].get("weakTo", [])
            weak_str = (
                "、".join(_element_cn(w) for w in weak_to)
                if isinstance(weak_to, list) and weak_to
                else "无"
            )
            resist_to = e["boss"].get("resistTo")
            if isinstance(resist_to, list):
                resist_str = "、".join(_element_cn(r) for r in resist_to) if resist_to else "无"
            elif resist_to:
                resist_str = _element_cn(resist_to)
            else:
                resist_str = "无"
            dps = e["boss"].get("damagePerScore")
            if dps:
                overview_parts.append(f"单分伤害：{dps}")
            overview_parts.append(f"弱点：{weak_str}")
            overview_parts.append(f"抗性：{resist_str}")
        else:
            # boss 缺失但 meta floor 存在：仅输出名称 + 暂无详细机制标注
            overview_parts.append("暂无详细机制")
        if e["lagging"]:
            overview_parts.append("（机制数据可能滞后）")
        lines.append("｜".join(overview_parts))

    # 2. 逐 boss【机制】明细（stat 摘要 + 机制列表）
    detail_lines: list[str] = []
    for idx, e in enumerate(entries, start=1):
        boss = e["boss"]
        if boss is None:
            continue
        display_en = e["display_en"]
        cn_name = _cn_by_en(lookup, display_en)
        if cn_name and cn_name != display_en:
            name_str = f"{cn_name}（{display_en}）"
        else:
            name_str = cn_name or display_en
        detail_lines.append(f"【机制】{idx}. {name_str}")

        # stat 摘要：复用 _monster_stat_dict 三源解包（嵌套 dict/list 兼容），
        # 键经 _term_cn 翻译，每 boss 最多 3 项关键值
        stat_dict = _monster_stat_dict(boss)
        stats_parts = _stat_parts(stat_dict, key_cn=_term_cn, skip_keys=_STAT_SKIP_KEYS)
        if stats_parts:
            detail_lines.append(f"  - {' | '.join(stats_parts)}")

        # 机制列表：名称 _term_cn + descCN（缺则 desc 经 _term_cn），全量输出；
        # 数据滞后时省略机制明细
        if e["lagging"]:
            continue
        mechanics = boss.get("mechanic", [])
        if not isinstance(mechanics, list) or not mechanics:
            continue
        for m in mechanics:
            if not isinstance(m, dict):
                continue
            m_name = m.get("name", "")
            m_name_cn = _term_cn(m_name) if m_name else ""
            desc = m.get("descCN") or ""
            if not desc:
                desc = _term_cn(m.get("desc", ""))
            if not desc:
                detail_lines.append(f"  - {m_name_cn or m_name}")
                continue
            # 先清理游戏标记再输出：<color> 等闭合标签完整保留，杜绝 </colo 断片
            desc = strip_game_markup(desc)
            detail_lines.append(f"  - {m_name_cn or m_name}：{desc}")

    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)

    raw_text = "\n".join(lines)
    clean_text = strip_game_markup(raw_text)
    return clean_text, True


def _match_blitz_boss(
    term: str,
    st: StelladbFetcher,
    lookup: Any,
) -> bool:
    """当期联合讨伐 boss 名兜底匹配（签名含 lookup，支持中/英文名）。

    对当期 floor 的 meta floor name 与 blitz[floor]["name"] 做大小写不敏感
    子串/精确匹配（经 _normalize），并镜像 _match_monster 的中文反查——
    命中"其中一个 boss 名（中/英）"即返回 True。
    """
    if not term or not st:
        return False
    resolved = _resolve_current_blitz(st)
    if not resolved:
        return False
    _bb_key, floors = resolved

    dataset = st.fetch_ssdata_dataset("blitz")
    if not dataset or not isinstance(dataset, dict):
        return False

    norm_term = _normalize(term.strip())
    if not norm_term:
        return False

    for fl, fl_info in floors.items():
        if not isinstance(fl_info, dict):
            continue
        fl_name = fl_info.get("name", "")
        names = [fl_name]
        boss = dataset.get(str(fl)) or dataset.get(fl)
        if isinstance(boss, dict) and boss.get("name"):
            names.append(boss["name"])
        for name in names:
            if not name:
                continue
            norm_name = _normalize(name)
            if norm_term == norm_name or (norm_name and norm_term in norm_name):
                return True
            # 镜像 _match_monster 的中文反查：中文名子串命中
            if lookup:
                cn_name = _cn_by_en(lookup, name)
                if cn_name and cn_name != name:
                    norm_cn = _normalize(cn_name)
                    if norm_term == norm_cn or (norm_cn and norm_term in norm_cn):
                        return True
    return False

