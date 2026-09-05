#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全字段字典生成脚本

职责：
    读取 <StellaSoraData 根目录> 下 EN/language/en_US/ 与 CN/language/zh_CN/ 同名 JSON，
    保留所有字段（.1 名字 + .2/.3 描述/效果/剧情文本等），按 ID 对齐合并后输出两个文件：

    - dict.json   : 主表，{ "<id>": {"en": "...", "cn": "...", "cat": "..."} }（全字段）
    - names.json  : 名字索引，{ "<en 或 cn 名>": "<id>" }，仅索引 .1 名字字段，
                    同名 id 出现多个 cat 时按 cat 顺序优先

可选参数：
    --source <path>   : 本地 StellaSoraData 根目录（必填）
    --output <dir>    : 输出目录（默认 plugins/stellasora/data）
    --languages a,b   : 源语言目录列表（默认 en,cn）。JP/KR/TW 不提交进 dict，仅生成到 _archive/
    --archive-dir <d> : JP/KR/TW 归档目录（默认 _archive）

数据形态约定（已实际抽样验证）：
    语言文件 key 形如 "Character.103.1" / "Skill.100010302.2"
    - 类目.主ID.序号；.1 为名称字段，.2/.3/... 为描述、效果、剧情等文本
    - 同一文件 EN 与 CN 的 key 集合高度对齐，未对齐的 ID 跳过
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable

# 游戏文本中的 UI 样式标签（<color=#xxx>...</color>）——纯文本输出无意义，
# 且会让 term_replace 的 .2 整段模式与 stelladb 清洗后文本错位，构建时剥离
_COLOR_TAG_RE = re.compile(r"</?color=[^>]*>", re.IGNORECASE)


def _normalize_game_text(value: str) -> str:
    """规范化游戏解包文本：剥离 color 标签、\x0b（软换行）归一为换行。"""
    value = _COLOR_TAG_RE.sub("", value)
    return value.replace("\x0b", "\n")


# 当同名 ID 在多个 cat 中出现，名字索引按此顺序优先保留（角色/技能 > 物品 > 标签 > UI > 其它）
CAT_PRIORITY = [
    "Character",
    "Skill",
    "MainSkill",
    "SecondarySkill",
    "SubNoteSkill",
    "CharacterTag",
    "Force",
    "Potential",
    "Item",
    "Word",
    "FateCard",
    "DictionaryEntry",
    "DictionaryTab",
    "GachaType",
    "Title",
    "Honor",
    "UIText",
]


def detect_category(key: str) -> str:
    """从 'Character.103.1' 这类 key 中抽出 'Character'。"""
    return key.split(".", 1)[0] if "." in key else key


def load_all_entries(lang_dir: Path) -> Dict[str, str]:
    """读取一个语言目录（如 EN/language/en_US）下所有 *.json，合并为单层 dict。

    保留全部条目（.1 名字 + .2/.3 描述/效果/剧情文本等）；
    同名 key 后到者覆盖前者（实际数据中无重名）。
    """
    merged: Dict[str, str] = {}
    if not lang_dir.is_dir():
        return merged
    for json_file in sorted(lang_dir.glob("*.json")):
        try:
            with json_file.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] skip {json_file}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            merged[key] = value
    return merged


def build_dict(
    en_data: Dict[str, str],
    cn_data: Dict[str, str],
    languages: Iterable[str],
) -> Dict[str, Dict[str, str]]:
    """按 ID 对齐 EN 与 CN 专有名词。仅保留两边都存在的 key。

    返回结构：
        { "<id>": {"en": "...", "cn": "...", "cat": "..."} }
    """
    common_keys = sorted(set(en_data.keys()) & set(cn_data.keys()))
    out: Dict[str, Dict[str, str]] = {}
    for key in common_keys:
        en_value = _normalize_game_text(en_data[key].strip())
        cn_value = _normalize_game_text(cn_data[key].strip())
        # 跳过 EN/CN 文本都为空、或占位符 Null 的条目
        if not en_value or not cn_value:
            continue
        if en_value.lower() == "null" or cn_value == "Null":
            continue
        out[key] = {
            "en": en_value,
            "cn": cn_value,
            "cat": detect_category(key),
        }
    return out


def build_name_index(
    main_dict: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    """构建名字索引：{ "琥珀": "Character.103.1", "Amber": "Character.103.1", ... }。

    仅索引 .1 名字字段——.2/.3 是描述/效果/剧情等长文本，不适合做查词键
    （全字段主表中若不过滤，会产生上万条长文本死重量键）。
    处理重名冲突：同一名字在多个 cat 中出现时，按 CAT_PRIORITY 顺序择优。
    """
    # 名字索引冲突时优先保留高优先级 cat。
    # 排序规则：(cat_priority_rank DESC, key ASC)，先填入低优先级，让高优先级覆盖。
    index: Dict[str, str] = {}
    priority_rank = {cat: i for i, cat in enumerate(CAT_PRIORITY)}
    max_rank = len(CAT_PRIORITY)

    def sort_key(item):
        key = item[0]
        cat = detect_category(key)
        # rank 越大优先级越高，先取负实现 DESC
        return (-priority_rank.get(cat, max_rank), key)

    sorted_items = sorted(main_dict.items(), key=sort_key)
    for key, entry in sorted_items:
        if not key.endswith(".1"):
            continue  # 只索引名字字段
        en_name = entry["en"]
        cn_name = entry["cn"]
        # 后写入的高优先级条目覆盖低优先级同名条目
        index[en_name] = key
        index[cn_name] = key
    return index


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 使用 ensure_ascii=False 保留中文可读；压缩 separators 减少体积
    with path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
    print(f"[ok] wrote {path} ({path.stat().st_size:,} bytes, {len(payload):,} entries)")


def load_overrides(output_dir: Path) -> Dict:
    """读取 data/overrides.json（人工修正层）；文件不存在返回空配置。

    结构：
        { "entries": { "<id>": {"cn": "...", ...} },
          "aliases": { "<俗称>": "<id>" },
          "replacements": { "<旧子串>": "<新子串>" },
          "_comment": "..." }
    """
    path = output_dir / "overrides.json"
    if not path.is_file():
        return {"entries": {}, "aliases": {}, "replacements": {}}
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"overrides.json 顶层必须是对象: {path}")
    return {
        "entries": data.get("entries", {}) or {},
        "aliases": data.get("aliases", {}) or {},
        "replacements": data.get("replacements", {}) or {},
    }


def apply_entry_overrides(
    main_dict: Dict[str, Dict[str, str]], entries: Dict
) -> Dict[str, Dict[str, str]]:
    """按 ID 覆盖主表条目字段（en/cn/cat）。

    用于修正上游解包数据笔误（如 CharacterDes.157.1 的"花玲"→官方"花铃"）。
    必须在 build_name_index 之前调用，使名字索引用修正后的 cn 构建。
    """
    for entry_id, patch in entries.items():
        if entry_id.startswith("_"):
            continue  # 跳过 _comment 等说明键
        if entry_id in main_dict and isinstance(patch, dict):
            main_dict[entry_id].update(patch)
    return main_dict


def apply_replacement_overrides(
    main_dict: Dict[str, Dict[str, str]], replacements: Dict[str, str]
) -> Dict[str, Dict[str, str]]:
    """对全部条目的 cn 做子串替换（游戏内术语改名归一，如 心钥→心链）。

    必须在 build_name_index 之前调用，使名字索引与 term_replace 映射
    都拿到替换后的官方新称。
    """
    if not replacements:
        return main_dict
    for entry in main_dict.values():
        cn = entry.get("cn", "")
        if not cn:
            continue
        for old, new in replacements.items():
            if old in cn:
                entry["cn"] = cn.replace(old, new)
    return main_dict


def apply_alias_overrides(name_index: Dict[str, str], aliases: Dict) -> Dict[str, str]:
    """向名字索引追加别名（俗称/变体写法 → 主表 ID）。

    使"花玲"这类解包文本变体也能路由到正确的 Character 条目。
    目标 ID 不在主表时跳过（防止索引指向失效条目）。
    """
    for alias, target_id in aliases.items():
        if alias.startswith("_"):
            continue
        if target_id in name_index.values():
            name_index[alias] = target_id
    return name_index


def collect_language_subdirs(
    data_root: Path, lang_code: str
) -> tuple[Path, Path]:
    """根据 lang_code 返回 (bin_dir, lang_dir)。

    例如 lang_code='en' → (EN/bin, EN/language/en_US)
        lang_code='cn' → (CN/bin, CN/language/zh_CN)
    """
    if lang_code == "en":
        return data_root / "EN" / "bin", data_root / "EN" / "language" / "en_US"
    if lang_code == "cn":
        return data_root / "CN" / "bin", data_root / "CN" / "language" / "zh_CN"
    if lang_code == "jp":
        return data_root / "JP" / "bin", data_root / "JP" / "language" / "ja_JP"
    if lang_code == "kr":
        return data_root / "KR" / "bin", data_root / "KR" / "language" / "ko_KR"
    if lang_code == "tw":
        return data_root / "TW" / "bin", data_root / "TW" / "language" / "zh_TW"
    raise ValueError(f"unsupported language code: {lang_code}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build StellaSora EN-CN dictionary.")
    parser.add_argument(
        "--source",
        default=None,
        help="StellaSoraData 根目录路径（必填）",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "data"),
        help="输出目录（dict.json / names.json）",
    )
    parser.add_argument(
        "--languages",
        default="en,cn",
        help="纳入字典的语言列表，逗号分隔。JP/KR/TW 仅写入 _archive/ 不进 dict。",
    )
    parser.add_argument(
        "--archive-dir",
        default="_archive",
        help="非主力语言的归档子目录名（相对 output）",
    )
    args = parser.parse_args()

    data_root = Path(args.source)
    output_dir = Path(args.output)
    languages = [x.strip() for x in args.languages.split(",") if x.strip()]

    if not data_root.is_dir():
        print(f"[error] 数据源不存在: {data_root}", file=sys.stderr)
        return 1

    # 1. 加载 EN 与 CN 全字段文本（必需）
    en_bin, en_lang = collect_language_subdirs(data_root, "en")
    cn_bin, cn_lang = collect_language_subdirs(data_root, "cn")
    print(f"[info] loading EN: {en_lang}")
    en_data = load_all_entries(en_lang)
    print(f"[info]   -> {len(en_data):,} entries (all fields)")
    print(f"[info] loading CN: {cn_lang}")
    cn_data = load_all_entries(cn_lang)
    print(f"[info]   -> {len(cn_data):,} entries (all fields)")

    # 2. 对齐生成主表
    main_dict = build_dict(en_data, cn_data, languages)
    print(f"[info] aligned dict: {len(main_dict):,} entries")

    # 3. 应用人工修正层（overrides.json）后写主表与名字索引
    overrides = load_overrides(output_dir)
    main_dict = apply_entry_overrides(main_dict, overrides["entries"])
    main_dict = apply_replacement_overrides(main_dict, overrides["replacements"])
    name_index = build_name_index(main_dict)
    name_index = apply_alias_overrides(name_index, overrides["aliases"])
    write_json(output_dir / "dict.json", main_dict)
    write_json(output_dir / "names.json", name_index)

    # 4. 归档：JP/KR/TW 的全量词条（含描述）仅写到 _archive/
    archive_dir = output_dir / args.archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    for lang in ("jp", "kr", "tw"):
        _bin, lang_dir = collect_language_subdirs(data_root, lang)
        if not lang_dir.is_dir():
            print(f"[warn] 跳过 {lang}: {lang_dir} 不存在")
            continue
        print(f"[info] loading {lang}: {lang_dir}")
        # 注意：归档文件保留所有 .1+ 字段（含描述），用于开发调试与回溯
        merged_full: Dict[str, str] = {}
        for json_file in sorted(lang_dir.glob("*.json")):
            try:
                with json_file.open("r", encoding="utf-8") as fp:
                    data = json.load(fp)
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[warn] skip {json_file}: {exc}", file=sys.stderr)
                continue
            if isinstance(data, dict):
                merged_full.update(data)
        write_json(archive_dir / f"{lang}_full.json", merged_full)

    # 5. 报告：未对齐的 ID（仅 EN 有或仅 CN 有）
    only_en = sorted(set(en_data.keys()) - set(cn_data.keys()))
    only_cn = sorted(set(cn_data.keys()) - set(en_data.keys()))
    report = {
        "en_total": len(en_data),
        "cn_total": len(cn_data),
        "aligned": len(main_dict),
        "only_en_count": len(only_en),
        "only_cn_count": len(only_cn),
        "only_en_sample": only_en[:50],
        "only_cn_sample": only_cn[:50],
    }
    report_path = output_dir / "_alignment_report.json"
    write_json(report_path, report)

    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())