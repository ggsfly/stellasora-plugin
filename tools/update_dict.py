#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全字段字典更新脚本

两种模式：
  --mode local   从本地 StellaSoraData 仓库读取（需 --source 参数；建议先对本地仓库 git pull）
  --mode remote  直接从 GitHub 拉取最新语言文件（git sparse clone，只下载 EN/language 与
                 CN/language 两个目录，需要本机安装 Git 并加入 PATH）

合并策略（保守）：
  - 新数据覆盖旧条目（全字段：.1 名字 + .2/.3 描述/效果/剧情文本）
  - 旧条目独有 ID 保留（历史角色/物品不下线）
  - names.json 全量重建（仅索引 .1 名字字段）
  - 应用 overrides.json 人工修正层
  - 更新报告写入 data/_update_report.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_dict import (  # noqa: E402
    _normalize_game_text,
    apply_alias_overrides,
    apply_entry_overrides,
    apply_replacement_overrides,
    build_name_index,
    collect_language_subdirs,
    load_all_entries,
    load_overrides,
    write_json,
)

REPO_URL = "https://github.com/AutumnVN/StellaSoraData.git"


def load_current(dict_path: Path) -> Dict[str, Dict[str, str]]:
    """读取现有字典；不存在则报错退出（不静默重建）。"""
    if not dict_path.is_file():
        print(f"[error] 找不到现有 {dict_path}；请先运行 build_dict.py", file=sys.stderr)
        sys.exit(1)
    with dict_path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def fetch_from_root(data_root: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """从 StellaSoraData 根目录加载 EN/CN 全字段文本。"""
    _en_bin, en_lang = collect_language_subdirs(data_root, "en")
    _cn_bin, cn_lang = collect_language_subdirs(data_root, "cn")
    if not en_lang.is_dir() or not cn_lang.is_dir():
        print(f"[error] 数据源不完整（缺少 language 目录）: {data_root}", file=sys.stderr)
        sys.exit(1)
    print(f"[info] loading EN: {en_lang}")
    en_data = load_all_entries(en_lang)
    print(f"[info] loading CN: {cn_lang}")
    cn_data = load_all_entries(cn_lang)
    return en_data, cn_data


def fetch_remote(tmp_root: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    """git sparse clone 仓库的语言目录到临时目录，再按本地源加载。"""
    git = shutil.which("git")
    if not git:
        print("[error] remote 模式需要安装 Git 并加入 PATH；"
              "或改用 local 模式配合本地仓库", file=sys.stderr)
        sys.exit(1)
    repo_dir = tmp_root / "StellaSoraData"
    print(f"[info] sparse clone {REPO_URL}")
    subprocess.run(
        [git, "clone", "--depth", "1", "--filter=blob:none", "--sparse",
         REPO_URL, str(repo_dir)],
        check=True,
    )
    subprocess.run(
        [git, "sparse-checkout", "set", "EN/language", "CN/language"],
        cwd=repo_dir,
        check=True,
    )
    return fetch_from_root(repo_dir)


def build_new_dict(en_data: Dict[str, str], cn_data: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """EN/CN 同 ID 对齐生成新主表（仅保留两侧都存在的条目）。"""
    common = sorted(set(en_data) & set(cn_data))
    out: Dict[str, Dict[str, str]] = {}
    for key in common:
        en_v = _normalize_game_text(en_data[key].strip())
        cn_v = _normalize_game_text(cn_data[key].strip())
        if not en_v or not cn_v:
            continue
        if en_v.lower() == "null" or cn_v == "Null":
            continue
        out[key] = {"en": en_v, "cn": cn_v, "cat": key.split(".", 1)[0]}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Update StellaSora CN-EN dictionary.")
    parser.add_argument("--mode", choices=["local", "remote"], default="local",
                        help="local=本地仓库（需 --source）；remote=GitHub 直拉（需 Git）")
    parser.add_argument("--source", default=None,
                        help="StellaSoraData 根目录（仅 local 模式需要）")
    parser.add_argument("--output",
                        default=str(Path(__file__).resolve().parents[1] / "data"),
                        help="字典输出目录（含 dict.json / names.json）")
    args = parser.parse_args()

    output_dir = Path(args.output)
    dict_path = output_dir / "dict.json"
    old_dict = load_current(dict_path)
    print(f"[info] 现有字典: {len(old_dict):,} 条")

    # 1. 按模式获取最新语言数据
    if args.mode == "local":
        if not args.source:
            print("[error] --mode local 需要 --source <StellaSoraData 根目录>", file=sys.stderr)
            return 1
        data_root = Path(args.source)
        if not data_root.is_dir():
            print(f"[error] 数据源不存在: {data_root}", file=sys.stderr)
            return 1
        en_data, cn_data = fetch_from_root(data_root)
    else:
        tmp_root = Path(tempfile.mkdtemp(prefix="stellasora_dict_"))
        try:
            en_data, cn_data = fetch_remote(tmp_root)
        finally:
            # 数据已载入内存，临时目录可立即清理
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"[info] EN 词条: {len(en_data):,} | CN 词条: {len(cn_data):,}")

    # 2. 生成新主表并与旧字典合并
    new_dict = build_new_dict(en_data, cn_data)
    merged: Dict[str, Dict[str, str]] = dict(new_dict)
    for key, entry in old_dict.items():
        if key not in merged:
            merged[key] = entry

    added = sorted(set(new_dict) - set(old_dict))
    updated = sorted(
        k for k in set(new_dict) & set(old_dict) if new_dict[k] != old_dict[k]
    )
    stale = sorted(set(old_dict) - set(new_dict))

    # 3. 应用人工修正层（overrides.json）后写主表 + 重建名字索引 + 写报告
    overrides = load_overrides(output_dir)
    merged = apply_entry_overrides(merged, overrides["entries"])
    merged = apply_replacement_overrides(merged, overrides["replacements"])
    name_index = build_name_index(merged)
    name_index = apply_alias_overrides(name_index, overrides["aliases"])
    write_json(dict_path, merged)
    write_json(output_dir / "names.json", name_index)
    report = {
        "mode": args.mode,
        "old_count": len(old_dict),
        "merged_count": len(merged),
        "added": len(added),
        "added_sample": added[:50],
        "updated": len(updated),
        "updated_sample": updated[:50],
        "stale_kept": len(stale),
        "stale_sample": stale[:50],
    }
    write_json(output_dir / "_update_report.json", report)

    print(
        f"[done] 新增 {len(added)} | 更新 {len(updated)} | "
        f"数据源已消失但保留 {len(stale)} | 合计 {len(merged):,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
