#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星塔旅人（Stella Sora）离线数据同步工具。

支持在终端独立运行进行全量或单项离线数据抓取与持久化更新，
采用先写临时文件再原子替换模式，单项拉取失败保留本地旧文件回退不破坏完好性。

CLI 用法：
    python tools/sync_data.py --help
    python tools/sync_data.py --element ignis
    python tools/sync_data.py --all
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import argparse
import json
import logging
import sys
import time

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

if __package__:
    from .dict_lookup import DictLookup
    from .fetcher_google_doc import GoogleDocFetcher
    from .fetcher_stelladb import StelladbFetcher, _atomic_write, _read_offline_file
    from .team_table import FIXED_ELEMENTS, build_team_table
else:
    from dict_lookup import DictLookup
    from fetcher_google_doc import GoogleDocFetcher
    from fetcher_stelladb import StelladbFetcher, _atomic_write, _read_offline_file
    from team_table import FIXED_ELEMENTS, build_team_table

logger = logging.getLogger("stellasora.sync_data")

# 六大元素权威集合
ELEMENTS = ["ignis", "aqua", "terra", "lux", "umbra", "ventus"]

_DEFAULT_OFFLINE_DIR = Path(__file__).resolve().parents[1] / "data" / "offline"
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / ".cache"


def sync_offline_data(
    element: Optional[str] = None,
    sync_all: bool = False,
    cache_dir: Optional[Path] = None,
    offline_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """核心同步函数：抓取离线数据并持久化到本地。

    Args:
        element: 单一元素名（ignis/aqua/terra/lux/umbra/ventus，或 index/presets）
        sync_all: 是否执行全量同步（六大元素 + index + presets）
        cache_dir: 网络缓存目录（默认 data/.cache）
        offline_dir: 离线数据存储目录（默认 data/offline）

    Returns:
        包含更新统计与各条目明细的字典：
        {
            "total": int,
            "success": int,
            "failed": int,
            "duration": float,
            "items": {
                "<name>": {
                    "status": "success" | "failed",
                    "duration": float,
                    "char_count": int,
                    "error": Optional[str],
                    "file": Optional[str],
                }
            }
        }
    """
    start_total = time.perf_counter()
    target_offline_dir = Path(offline_dir) if offline_dir is not None else _DEFAULT_OFFLINE_DIR
    target_cache_dir = Path(cache_dir) if cache_dir is not None else _DEFAULT_CACHE_DIR

    # 确定待同步项目列表
    items_to_sync: List[str] = []
    if sync_all:
        items_to_sync = list(ELEMENTS) + ["index", "presets"]
    elif element:
        cleaned = element.strip().lower()
        items_to_sync = [cleaned]
    else:
        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "duration": 0.0,
            "items": {},
        }

    st_fetcher = StelladbFetcher(cache_dir=target_cache_dir, offline_dir=target_offline_dir)
    gd_fetcher = GoogleDocFetcher(cache_dir=target_cache_dir, offline_dir=target_offline_dir)

    results: Dict[str, Dict[str, Any]] = {}
    success_count = 0
    failed_count = 0

    for item in items_to_sync:
        item_start = time.perf_counter()
        target_file: Optional[Path] = None
        error_msg: Optional[str] = None
        char_count = 0
        success = False
        team_table_report: Optional[Dict[str, Any]] = None

        try:
            fetch_func = None
            if item in ELEMENTS:
                target_file = target_offline_dir / "infodocs" / f"{item}.json"
                fetch_func = lambda: st_fetcher.fetch_infodoc(item, force_update=True)
            elif item == "index":
                target_file = target_offline_dir / "infodocs" / "index.json"
                fetch_func = lambda: st_fetcher.fetch_infodoc_index(force_update=True)
            elif item == "presets":
                target_file = target_offline_dir / "presets" / "presets.txt"
                fetch_func = lambda: gd_fetcher.fetch_presets(force_update=True)
            else:
                error_msg = f"未知同步项 '{item}'。支持的元素: {', '.join(ELEMENTS)}，以及 index, presets"

            if fetch_func and target_file:
                mtime_before = target_file.stat().st_mtime_ns if target_file.is_file() else None
                res = fetch_func()
                mtime_after = target_file.stat().st_mtime_ns if target_file.is_file() else None

                if res and not res.startswith("Error fetching") and mtime_after is not None and mtime_after != mtime_before:
                    success = True
                    char_count = len(res)
                else:
                    success = False
                    char_count = len(res) if (res and not res.startswith("Error fetching")) else 0
                    error_msg = "网络拉取失败或更新未生效（已保留本地现有离线数据）"

                if item == "presets":
                    presets_text = res if (res and not res.startswith("Error fetching")) else (target_file.read_text(encoding="utf-8") if target_file.is_file() else "")
                    if presets_text:
                        try:
                            infodocs = {e: _read_offline_file(target_offline_dir / "infodocs" / f"{e}.json") or "" for e in FIXED_ELEMENTS}
                            index_text = _read_offline_file(target_offline_dir / "infodocs" / "index.json") or ""
                            lookup = DictLookup(target_offline_dir.parent)
                            lookup._load()
                            table_dict = build_team_table(presets_text, infodocs, lookup, index_text)
                            _atomic_write(target_offline_dir / "presets" / "team_table.json", json.dumps(table_dict, ensure_ascii=False, indent=2))
                            team_table_report = table_dict.get("report", {})
                        except Exception as e:
                            logger.exception("构建 team_table 失败: %s", e)

        except Exception as exc:
            logger.exception("同步 %s 时捕获异常: %s", item, exc)
            success = False
            error_msg = f"同步异常: {exc}"

        item_duration = round(time.perf_counter() - item_start, 3)
        if success:
            success_count += 1
        else:
            failed_count += 1

        results[item] = {
            "status": "success" if success else "failed",
            "duration": item_duration,
            "char_count": char_count,
            "error": error_msg,
            "file": str(target_file) if target_file is not None else None,
        }
        if item == "presets" and team_table_report is not None:
            results[item]["team_table"] = team_table_report

    total_duration = round(time.perf_counter() - start_total, 3)
    final_report: Dict[str, Any] = {
        "total": len(items_to_sync),
        "success": success_count,
        "failed": failed_count,
        "duration": total_duration,
        "items": results,
    }
    if "presets" in results and "team_table" in results["presets"]:
        final_report["team_table"] = results["presets"]["team_table"]
    return final_report


def _print_report(report: Dict[str, Any]) -> None:
    """格式化打印离线同步报告。"""
    print("=" * 64)
    print("           星塔旅人 (Stella Sora) 离线数据同步报告")
    print("=" * 64)
    print(
        f"总项数: {report['total']} | "
        f"成功: {report['success']} | "
        f"失败: {report['failed']} | "
        f"总耗时: {report['duration']}s"
    )
    print("-" * 64)

    for name, item in report.get("items", {}).items():
        status_tag = "[SUCCESS]" if item["status"] == "success" else "[FAILED ]"
        file_info = item["file"] or "-"
        if item["status"] == "success":
            print(
                f"{status_tag} {name:<8} | 耗时: {item['duration']:>5.2f}s | "
                f"字符数: {item['char_count']:>6} | 路径: {file_info}"
            )
        else:
            err = item.get("error") or "未知错误"
            print(
                f"{status_tag} {name:<8} | 耗时: {item['duration']:>5.2f}s | "
                f"错误: {err}"
            )

    if "team_table" in report:
        tt = report["team_table"]
        stats = tt.get("stats", {}) if isinstance(tt, dict) else {}
        print("-" * 64)
        print("           team_table 统一队伍-槽位表构建")
        print("-" * 64)
        print(
            f"总行数: {stats.get('total_rows', 0)} | "
            f"预设码行: {stats.get('preset_rows', 0)} | "
            f"无码行: {stats.get('codeless_rows', 0)} | "
            f"关联区块: {stats.get('correlated_blocks', 0)}/{stats.get('total_blocks', 0)} | "
            f"无效行: {stats.get('invalid_rows_count', 0)}"
        )

    print("=" * 64)


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="星塔旅人（Stella Sora）离线数据全量/单项同步工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python tools/sync_data.py --element ignis
    python tools/sync_data.py --all
    python tools/sync_data.py --element presets
        """,
    )
    parser.add_argument(
        "--element",
        type=str,
        default=None,
        help="同步指定元素攻略数据 (ignis/aqua/terra/lux/umbra/ventus) 或 index/presets",
    )
    parser.add_argument(
        "--all",
        dest="sync_all",
        action="store_true",
        help="全量同步六大元素 infodoc、索引页 index 及 Google Docs 预设码",
    )
    parser.add_argument(
        "--offline-dir",
        type=str,
        default=None,
        help="自定义离线数据存储根目录 (默认 data/offline)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="自定义网络缓存目录 (默认 data/.cache)",
    )

    args = parser.parse_args()

    if not args.sync_all and not args.element:
        parser.print_help()
        print("\n提示: 请指定 --element <元素名> 或 --all 进行同步。")
        return 0

    report = sync_offline_data(
        element=args.element,
        sync_all=args.sync_all,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        offline_dir=Path(args.offline_dir) if args.offline_dir else None,
    )
    _print_report(report)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
