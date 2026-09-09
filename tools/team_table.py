#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星塔旅人（Stella Sora）统一队伍-槽位表构建器。

纯函数模块，无网络请求，负责：
  1. 预设码 base64 解码为 3 槽位 CharId；
  2. 预设码文档解析与清洗（提取元素、队名、码、WIP 状态）；
  3. 离线 infodoc 区块扫描与成员提取；
  4. 码行与区块固化关联（主控一致 + 成员包含 + 名字相似度 tiebreaker）；
  5. 关联后未匹配区块生成无码行（共享前缀拆行）；
  6. 行有效性校验（3 槽全满 + 队名并集非空）；
  7. 固化每行主控 rotation。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple
import base64
import difflib
import logging
import re
import struct
import time

if __package__:
    from .service import extract_rotation
else:
    from service import extract_rotation

logger = logging.getLogger("stellasora.team_table")

FIXED_ELEMENTS = ["aqua", "ignis", "ventus", "terra", "lux", "umbra"]
ELEMENT_SECTIONS = {"Aqua", "Ignis", "Ventus", "Terra", "Lux", "Umbra"}
_LABEL_LINE_RE = re.compile(r"Trekker|Preset Code|Slot", re.IGNORECASE)
_CODE_LINE_RE = re.compile(r"^[A-Za-z0-9\-_+/]{20,}$")


def decode_preset_code(code: str) -> Optional[List[int]]:
    """解码预设码字符串为 3×32bit 大端 CharId 列表。"""
    if not code:
        return None
    cleaned = code.strip().replace("-", "+").replace("_", "/")
    if not cleaned:
        return None
    pad = (4 - len(cleaned) % 4) % 4
    try:
        raw = base64.b64decode(cleaned + "=" * pad, validate=True)
        if len(raw) < 12:
            return None
        ids = list(struct.unpack(">III", raw[:12]))
        return None if any(cid == 0 for cid in ids) else ids
    except Exception:
        return None


def parse_presets_doc(text: str) -> List[Dict[str, Any]]:
    """解析预设文档文本为条目列表。

    使用 "Main Trekker" 标签行作为新队伍主标题的确认标志：
    非标签文本行先暂存为 pending_team，仅在后续遇到 "Main Trekker" 时才提升为
    current_team；否则视为子标题/版本标签（如 Teresa Version、AoE (Mobbing)），
    不覆盖当前队名。这样嵌套标题下的预设码行仍归属于主标题队名。
    """
    current_elem: Optional[str] = None
    current_team, saw_wip = "", False
    pending_team: Optional[str] = None
    items: List[Dict[str, Any]] = []

    for line in text.splitlines():
        s = line.strip()
        if s in ELEMENT_SECTIONS:
            current_elem, current_team, saw_wip = s.lower(), "", False
            pending_team = None
            continue
        if not current_elem:
            continue
        if s.upper() == "WIP":
            saw_wip = True
            continue
        if _CODE_LINE_RE.fullmatch(s):
            items.append({
                "element": current_elem,
                "team_name_raw": current_team,
                "code": s,
                "wip": saw_wip or ("WIP" in current_team.upper()),
            })
            pending_team = None
            continue
        # "Main Trekker" 标签行 → 确认 pending_team 为新队伍主标题
        if re.search(r"Main\s+Trekker", s, re.IGNORECASE):
            if pending_team is not None:
                current_team = pending_team
                saw_wip = False
                pending_team = None
            continue
        if _LABEL_LINE_RE.search(s):
            continue
        if re.search(r"[A-Za-z]", s):
            pending_team = s

    return items


def iter_infodoc_blocks(infodoc_text: str, en_names: List[str]) -> List[Dict[str, Any]]:
    """扫描元素详细页文本中的所有攻略区块与有序成员列表。"""
    sorted_en = sorted(en_names, key=len, reverse=True)
    blocks: List[Dict[str, Any]] = []
    curr_name: Optional[str] = None
    curr_members: List[str] = []

    for line in infodoc_text.splitlines():
        if "⏏" in line:
            if curr_name and curr_name.upper() != "BACK TO TOP":
                blocks.append({"name": curr_name, "members_ordered": curr_members})
            parts = [c.strip() for c in line.split("|") if "⏏" not in c and c.strip()]
            curr_name, curr_members = (parts[0] if parts else None), []
            continue
        if curr_name and "★" in line:
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells:
                first_cell = cells[0]
                for en in sorted_en:
                    if re.match(r"^" + re.escape(en) + r"(\s|\(|$)", first_cell):
                        if en not in curr_members:
                            curr_members.append(en)
                        break

    if curr_name and curr_name.upper() != "BACK TO TOP":
        blocks.append({"name": curr_name, "members_ordered": curr_members})
    return blocks


def _normalize_name(name: str) -> str:
    """规范化队名用于关联相似度对比。"""
    n = name.replace("\u2019", "'").replace("\u2018", "'")
    n = re.sub(r"\bDMG\b", "Damage", n, flags=re.IGNORECASE)
    return re.sub(r"[\s\-_\(\)]", "", n).lower()


def _resolve_char(lookup: Any, char_idx: Dict[str, str], val: Any) -> Optional[Tuple[int, str, str]]:
    """解析成员为 (char_id, en, cn)，非 Character 或不存在时返回 None。"""
    if isinstance(val, int):
        cid = val
        entry = lookup.get_by_id(f"Character.{cid}.1")
    else:
        item_id = char_idx.get(str(val))
        if not item_id:
            return None
        cid = int(item_id.split(".")[1])
        entry = lookup.get_by_id(item_id)
    if not entry or entry.get("cat") != "Character":
        return None
    return cid, entry.get("en", ""), entry.get("cn", "")


def _make_slots(chars: List[Tuple[int, str, str]]) -> List[Dict[str, Any]]:
    """根据已解析成员三元组列表构造标准 3 槽位结构。"""
    return [
        {"char_id": c[0], "slot": "主控位" if i == 0 else "支援位", "en": c[1], "cn": c[2]}
        for i, c in enumerate(chars)
    ]


def _load_team_overrides() -> Dict[str, Any]:
    """从 data/overrides.json 中加载队伍人工修正配置。"""
    from pathlib import Path
    import json
    overrides_path = Path(__file__).resolve().parents[1] / "data" / "overrides.json"
    if overrides_path.is_file():
        try:
            with overrides_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("team_overrides", {})
        except Exception as e:
            logger.warning("Failed to load team_overrides from %s: %s", overrides_path, e)
    return {}


def build_team_table(
    presets_text: str,
    infodocs: Dict[str, str],
    lookup: Any,
    index_text: str,
    team_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建全量队伍-槽位统一表。"""
    if team_overrides is None:
        team_overrides = _load_team_overrides()
    char_idx: Dict[str, str] = lookup._build_character_index() if hasattr(lookup, "_build_character_index") else {}
    en_names = sorted(list(char_idx.keys()), key=len, reverse=True)
    blocks_by_element = {elem: iter_infodoc_blocks(infodocs.get(elem, ""), en_names) for elem in FIXED_ELEMENTS}

    rows: List[Dict[str, Any]] = []
    invalid_rows: List[Dict[str, Any]] = []
    correlated_block_keys: Set[Tuple[str, str]] = set()
    preset_count, codeless_count = 0, 0

    # 1. 码行处理与固化关联
    for item in parse_presets_doc(presets_text):
        code, elem = item["code"], item["element"].lower()
        team_name_preset = item["team_name_raw"] or None
        char_ids = decode_preset_code(code)
        if not char_ids or len(char_ids) != 3:
            invalid_rows.append({"code": code, "reason": "缺槽位（解码失败或槽位数不足）", "detail": item})
            continue

        resolved = [_resolve_char(lookup, char_idx, cid) for cid in char_ids]
        if any(r is None for r in resolved):
            invalid_rows.append({"code": code, "reason": "成员未知（非Character类目或不存在）", "detail": item})
            continue
        valid_resolved = [r for r in resolved if r is not None]
        slot_ens = [r[1] for r in valid_resolved]

        candidates = [
            b for b in blocks_by_element.get(elem, [])
            if b.get("members_ordered") and b["members_ordered"][0] == slot_ens[0] and set(slot_ens).issubset(set(b["members_ordered"]))
        ]

        matched_block: Optional[Dict[str, Any]] = None
        if len(candidates) == 1:
            matched_block = candidates[0]
        elif len(candidates) > 1:
            norm_p = _normalize_name(team_name_preset or "")
            scored = sorted(
                [(difflib.SequenceMatcher(None, norm_p, _normalize_name(c.get("name", ""))).ratio(), c) for c in candidates],
                key=lambda x: x[0],
                reverse=True,
            )
            if scored[0][0] > scored[1][0]:
                matched_block = scored[0][1]

        # 人工修正层：支持根据预设码显式解绑或重定向攻略区块
        code_override = team_overrides.get(code, {})
        if code_override.get("unlink"):
            matched_block = None
        elif code_override.get("block"):
            target_block_name = code_override["block"]
            for b in blocks_by_element.get(elem, []):
                if b.get("name") == target_block_name:
                    matched_block = b
                    break

        team_name_infodoc = matched_block["name"] if matched_block else None
        if matched_block:
            correlated_block_keys.add((elem, matched_block["name"]))

        if not (team_name_preset or team_name_infodoc):
            invalid_rows.append({"code": code, "reason": "缺名字（孤码且未关联到区块）", "detail": item})
            continue

        main_en = valid_resolved[0][1]
        rows.append({
            "main_key": code,
            "preset_code": code,
            "element": elem,
            "slots": _make_slots(valid_resolved),
            "team_name_preset": team_name_preset,
            "team_name_infodoc": team_name_infodoc,
            "rotation": extract_rotation(index_text, main_en) if main_en else "",
            "guide_ref": {"element": elem, "block": matched_block["name"]} if matched_block else None,
        })
        preset_count += 1

    # 2. 无码区块共享前缀拆分
    for elem in FIXED_ELEMENTS:
        for b in blocks_by_element.get(elem, []):
            block_name = b["name"]
            if (elem, block_name) in correlated_block_keys:
                continue

            mems = b.get("members_ordered", [])
            if len(mems) < 3:
                invalid_rows.append({"block": block_name, "element": elem, "reason": "缺槽位（段数少于3无法构成3槽位）", "detail": mems})
                continue

            r0 = _resolve_char(lookup, char_idx, mems[0])
            r1 = _resolve_char(lookup, char_idx, mems[1])
            if not r0 or not r1:
                invalid_rows.append({"block": block_name, "element": elem, "reason": "成员未知（非Character类目或不存在）", "detail": mems[:2]})
                continue

            rotation = extract_rotation(index_text, r0[1]) if r0[1] else ""
            for supp2_en in mems[2:]:
                r2 = _resolve_char(lookup, char_idx, supp2_en)
                if not r2:
                    invalid_rows.append({"block": block_name, "element": elem, "reason": "成员未知（非Character类目或不存在）", "detail": supp2_en})
                    continue

                rows.append({
                    "main_key": f"{elem}::{block_name}::{supp2_en}",
                    "preset_code": None,
                    "element": elem,
                    "slots": _make_slots([r0, r1, r2]),
                    "team_name_preset": None,
                    "team_name_infodoc": block_name,
                    "rotation": rotation,
                    "guide_ref": {"element": elem, "block": block_name},
                })
                codeless_count += 1

    report = {
        "generated_at": time.time(),
        "stats": {
            "total_rows": len(rows),
            "preset_rows": preset_count,
            "codeless_rows": codeless_count,
            "correlated_blocks": len(correlated_block_keys),
            "total_blocks": sum(len(b) for b in blocks_by_element.values()),
            "invalid_rows_count": len(invalid_rows),
        },
        "invalid_rows": invalid_rows,
    }
    return {"rows": rows, "report": report}
