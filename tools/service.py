#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务层：把查询核心逻辑提炼为可复用函数。

插件（plugin.py）与本地实验脚本共用本模块，
保证独立运行与插件运行行为一致。

缓存目录参数化：插件运行时用 MaiBot 分配的 runtime_dir，
CLI 运行时用 data/.cache。
"""

from __future__ import annotations

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
_pending_replacements: Dict[str, str] = {}


def configure_overrides(
    aliases: Optional[Dict[str, str]] = None,
    replacements: Optional[Dict[str, str]] = None,
) -> None:
    """配置运行时的别名与文本替换规则（由 plugin.py 或配置加载时调用）。"""
    global _pending_aliases, _pending_replacements
    with _init_lock:
        if aliases is not None:
            _pending_aliases = dict(aliases)
        if replacements is not None:
            _pending_replacements = dict(replacements)

        key = str(_DATA_DIR)
        if key in _instances:
            lookup, _last, _st, _gd, replacer = _instances[key]
            if aliases is not None:
                lookup.set_custom_aliases(_pending_aliases)
            if replacements is not None:
                replacer.set_custom_replacements(_pending_replacements)


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
                custom_replacements=_pending_replacements,
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


def query_how(term: str, cache_dir: Path, with_presets: bool = False, max_length: Optional[int] = None) -> str:
    """how 桶：配队/纹章/秘纹/技能优先度（--presets 时附加预设码）。"""
    lookup, _last, st_fetcher, gd_fetcher, replacer = _get_services(cache_dir)
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

    element: Optional[str] = None
    character_en = res["en"]
    character_cn = res["cn"]
    is_character = res["cat"] == "Character"

    if is_character:
        num_id = res["id"].split(".")[1]
        trekker_text = st_fetcher.fetch_trekker(num_id)
        element = detect_element(trekker_text)
    if element:
        lines += ["=== 元素属性 ===", f"  {character_cn}（{character_en}）是{ELEMENT_CN[element]}属性角色", ""]

    if not element:
        if res["en"] in ELEMENT_SECTIONS:
            element = res["en"]
        elif term in ELEMENT_SECTIONS:
            element = term

    # 预设码区块放在攻略正文之前：它是用户明确要求的内容（--presets），
    # 且输出可能因长度上限被截断——放在前面保证不被截掉
    preset_lines: list[str] = []
    if with_presets:
        preset_lines.append("=== 预设码推荐 (Google Docs) ===")
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
        lines.append(f"=== {ELEMENT_CN[element]}队文字攻略 (stelladb /infodoc/{element.lower()}) ===")
        infodoc_text = st_fetcher.fetch_infodoc(element.lower())
        if infodoc_text and "Error" not in infodoc_text:
            lines.append(strip_game_markup(replacer.replace(infodoc_text)))
        else:
            lines.append("  [抓取失败]")
        lines.append("")

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
