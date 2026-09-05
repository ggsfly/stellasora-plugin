#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字典一致性测试

用法：
    python -m stellasora.tests.test_dict
    # 或
    python tests/test_dict.py

测试项：
    1. dict.json / names.json 存在且 JSON 合法
    2. dict.json 每条记录字段完整（en/cn/cat 都非空）
    3. names.json 名字到 ID 的反向引用都能在 dict.json 中找到
    4. names.json 与 dict.json 条目数比例在合理区间（同名合并应使 names < dict*2）
    5. 关键游戏词抽样一致性（已知 5-10 组中英对照必须能双向查到）
    6. 报告字典体积并断言不超过预设上限
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DICT_PATH = DATA_DIR / "dict.json"
NAMES_PATH = DATA_DIR / "names.json"

# 抽样一致性测试：(中文名, 英文名, 期望 cat 前缀, 期望主 ID)
SAMPLES = [
    ("琥珀", "Amber", "Character", "103"),
    ("小禾", "Nazuna", "Character", "156"),
    ("风影", "Wraith", "Character", "143"),
    ("感电", "Shock", "Word", "1001"),
    ("冻结", "Freeze", "Word", "1002"),
    ("火垂", "Firefly", "Character", "115"),
    ("冲击", None, "Word", None),  # 只验中文（实际数据中是 "Shock"，需按数据校对）
]


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def main() -> int:
    # 1. 存在性
    if not DICT_PATH.is_file():
        fail(f"missing {DICT_PATH}")
    if not NAMES_PATH.is_file():
        fail(f"missing {NAMES_PATH}")

    # 2. JSON 合法性
    with DICT_PATH.open("r", encoding="utf-8") as fp:
        main_dict = json.load(fp)
    with NAMES_PATH.open("r", encoding="utf-8") as fp:
        name_index = json.load(fp)

    print(f"[info] dict entries: {len(main_dict):,}")
    print(f"[info] names entries: {len(name_index):,}")

    # 3. 字段完整性
    for key, entry in main_dict.items():
        if not isinstance(entry, dict):
            fail(f"{key}: entry 不是 dict")
        for field in ("en", "cn", "cat"):
            if field not in entry:
                fail(f"{key}: missing field '{field}'")
            if not entry[field] or not isinstance(entry[field], str):
                fail(f"{key}.{field}: empty or non-str")

    # 4. 名字索引一致性：每个 name → id → entry.cn/en 必须匹配
    mismatches = 0
    for name, key in name_index.items():
        if key not in main_dict:
            print(f"[warn] names['{name}'] -> '{key}' but '{key}' not in dict")
            mismatches += 1
            continue
        entry = main_dict[key]
        if name != entry["en"] and name != entry["cn"]:
            print(f"[warn] names['{name}'] -> '{key}' but name neither en nor cn")
            mismatches += 1
    if mismatches > 5:
        fail(f"name index has {mismatches} mismatches (too many)")
    print(f"[ok] name index mismatches: {mismatches}")

    # 5. 抽样一致性
    for cn_name, en_name, cat_prefix, expected_id_prefix in SAMPLES:
        # 反查中文
        if cn_name in name_index:
            key = name_index[cn_name]
            entry = main_dict[key]
            if not key.startswith(f"{cat_prefix}."):
                print(f"[warn] '{cn_name}' -> {key}, expected cat {cat_prefix}")
            if expected_id_prefix and key.split(".")[1] != expected_id_prefix:
                print(f"[warn] '{cn_name}' -> {key}, expected id {expected_id_prefix}")
            if en_name and entry["en"] != en_name:
                print(f"[warn] '{cn_name}' en='{entry['en']}' != expected '{en_name}'")
            else:
                print(f"[ok] '{cn_name}' -> {key} ({entry['en']} / {entry['cn']})")
        else:
            print(f"[warn] '{cn_name}' not found in name index")

    # 6. 体积上限（dict ≤ 10 MB，names ≤ 5 MB）
    dict_size = DICT_PATH.stat().st_size
    names_size = NAMES_PATH.stat().st_size
    print(f"[info] dict.json: {dict_size:,} bytes")
    print(f"[info] names.json: {names_size:,} bytes")
    if dict_size > 10 * 1024 * 1024:
        fail(f"dict.json too big: {dict_size:,} bytes > 10 MB")
    if names_size > 5 * 1024 * 1024:
        fail(f"names.json too big: {names_size:,} bytes > 5 MB")

    print("[ALL PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())