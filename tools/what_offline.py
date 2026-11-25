# -*- coding: utf-8 -*-
"""stellasora_what 离线可复跑闭环：原文 → 解析 → SQLite+FTS5 → 本地查询结果。

零网络、只读原始快照、只写独立构建目录。不导入宿主模块，不触发任何发送。

用法（插件根目录，用宿主 venv 的 python）：

    python -B tools/what_offline.py build
    python -B tools/what_offline.py stats
    python -B tools/what_offline.py query --intent '{"mode":"field","subject":{"name":"优叶","kind":"traveler"},"fields":["stat.attack"],"levels":[90]}'
    python -B tools/what_offline.py demo

`build` 先写本次独有的 `staging-<run>/build.sqlite3`，校验通过才发布为 `what.sqlite3`。
已有的正式库与旧的 staging **一律不动**：目标已存在就直接失败（换 `--build-dir` 重建），
不做「先删后写」；发布用 `os.link` 原子 no-clobber（并发/晚到的同名库也不会被覆盖），
失败保留 staging 供复查。

退出码：0 全部通过；2 前置条件缺失（含正式库已存在）；3 清单校验失败（缺页/重复目标/
ID 不符/多余详情），未建库未发布；4 发布切换失败，原文件与 staging 都保留。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import what_db  # noqa: E402
import what_parse  # noqa: E402
import what_query  # noqa: E402

DEFAULT_SNAPSHOT = _HERE.parent / ".zcode" / "what-research" / "20261125-full"
DEFAULT_BUILD_DIR = _HERE.parent / ".zcode" / "what-research" / "20261125-offline-build"

EXIT_OK = 0
EXIT_PRECONDITION = 2
EXIT_MANIFEST = 3
EXIT_PUBLISH = 4


# ---------------------------------------------------------------- 快照读取


def _walk_tree(payload: Any) -> List[Dict[str, Any]]:
    """一次遍历目录树，返回每条的 `content_id / name / path / parent_path`。

    只遍历一次：早期版本在每个条目里重读 tree.json 做反查，722 条目 × 627 节点会退化成
    O(n²) 的文件解析。
    """
    out: List[Dict[str, Any]] = []

    def walk(nodes: Any, path: str) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            name = node.get("name") or ""
            here = f"{path}/{name}"
            cid = node.get("content_id")
            out.append(
                {
                    "node_id": node.get("id"),
                    "name": name,
                    "path": here,
                    "parent_path": path,
                    "content_id": int(cid) if cid else None,
                }
            )
            walk(node.get("child") or [], here)

    if isinstance(payload, dict):
        walk(payload.get("data") or [], "")
    return out


def load_snapshot_meta(snapshot: Path) -> Dict[str, Any]:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    tree_file = snapshot / "raw" / "tree.json"
    tree_rows = _walk_tree(json.loads(tree_file.read_text(encoding="utf-8"))) if tree_file.exists() else []
    tree_by_cid: Dict[int, Dict[str, Any]] = {}
    for row in tree_rows:
        if row["content_id"] is not None:
            tree_by_cid.setdefault(row["content_id"], row)
    list_items: Dict[int, Dict[str, Any]] = {}
    for page_file in sorted((snapshot / "raw" / "list").glob("page_*.json")):
        payload = json.loads(page_file.read_text(encoding="utf-8"))
        for item in payload.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                list_items[int(item["id"])] = item
    return {
        "manifest": manifest,
        "tree_rows": tree_rows,
        "tree_by_cid": tree_by_cid,
        "list": list_items,
    }


def parse_snapshot(
    snapshot: Path,
) -> Tuple[List[Any], List[Tuple[Any, ...]], Dict[str, Any]]:
    """按清单校验 ID，逐条解析。清单异常与解析状态分开记录。"""
    meta = load_snapshot_meta(snapshot)
    targets: List[int] = [int(x) for x in meta["manifest"]["targets"]]
    detail_dir = snapshot / "raw" / "detail"

    manifest_errors: List[Dict[str, Any]] = []
    seen: set = set()
    duplicates: List[int] = []
    for cid in targets:
        if cid in seen:
            duplicates.append(cid)
        seen.add(cid)
    if duplicates:
        manifest_errors.append({"code": "duplicate_target_ids", "ids": sorted(set(duplicates))})

    articles: List[Any] = []
    missing: List[Dict[str, Any]] = []
    id_mismatch: List[Dict[str, Any]] = []
    for cid in dict.fromkeys(targets):
        path = detail_dir / f"{cid}.json"
        if not path.exists():
            missing.append({"content_id": cid, "reason": "detail_file_missing"})
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        tree = meta["tree_by_cid"].get(cid)
        list_item = meta["list"].get(cid)
        art = what_parse.parse_raw_dir_entry(
            payload,
            tree_path=tree["path"] if tree else None,
            list_item=list_item,
            list_updated_at=(list_item or {}).get("updated_at"),
        )
        if art.content_id != cid:
            id_mismatch.append(
                {"expected": cid, "actual": art.content_id, "file": str(path.name)}
            )
            continue
        articles.append(art)

    if missing:
        manifest_errors.append({"code": "detail_files_missing", "items": missing})
    if id_mismatch:
        manifest_errors.append({"code": "content_id_mismatch", "items": id_mismatch})

    entries: List[Tuple[Any, ...]] = []
    for row in meta["tree_rows"]:
        if not row["node_id"]:
            continue
        entries.append(
            (
                int(row["node_id"]),
                None,
                row["name"],
                row["path"],
                row["content_id"],
            )
        )

    extras = _extra_detail_files(detail_dir, set(targets))
    if extras:
        manifest_errors.append({"code": "extra_detail_files", "ids": extras})

    report = {
        "targets": len(targets),
        "unique_targets": len(dict.fromkeys(targets)),
        "parsed": len(articles),
        "missing_detail_files": missing,
        "duplicate_ids": sorted(set(duplicates)),
        "id_mismatch": id_mismatch,
        "extra_detail_files": extras,
        "manifest_errors": manifest_errors,
        "blocking": bool(missing or id_mismatch or duplicates),
    }
    return articles, entries, report


def _extra_detail_files(detail_dir: Path, targets: set) -> List[int]:
    if not detail_dir.exists():
        return []
    out = []
    for path in detail_dir.glob("*.json"):
        try:
            cid = int(path.stem)
        except ValueError:
            continue
        if cid not in targets:
            out.append(cid)
    return sorted(out)


# ---------------------------------------------------------------- 命令


def cmd_build(args: argparse.Namespace) -> int:
    snapshot = Path(args.snapshot)
    build_dir = Path(args.build_dir)
    if not (snapshot / "manifest.json").exists():
        print(f"[FAIL] 快照清单不存在：{snapshot / 'manifest.json'}", file=sys.stderr)
        return EXIT_PRECONDITION

    final_path = build_dir / "what.sqlite3"
    if final_path.exists():
        # 不删、不覆盖：先毁后建一旦失败就拿不回已验收的库。
        print(
            f"[FAIL] 正式库已存在，未覆盖也未删除：{final_path}\n"
            f"       要重建请换一个 --build-dir（旧库保持可读）。",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    articles, entries, report = parse_snapshot(snapshot)
    if report["blocking"]:
        # 清单校验失败：不入库、不发布、非零退出。缺页/重复目标/ID 不符不能用「部分成功」冒充完整。
        print("[FAIL] 清单校验未通过，未建库、未发布：", file=sys.stderr)
        for err in report["manifest_errors"]:
            print(f"       {err['code']}: {json.dumps(err, ensure_ascii=False)[:400]}", file=sys.stderr)
        (build_dir).mkdir(parents=True, exist_ok=True)
        (build_dir / "build-report.json").write_text(
            json.dumps({"published": False, "snapshot_report": report}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return EXIT_MANIFEST

    build_dir.mkdir(parents=True, exist_ok=True)
    # 每次构建独占一个 staging 目录：不删旧 staging，也不与别的运行共用同一文件名。
    staging_dir = tempfile.mkdtemp(prefix="staging-", dir=str(build_dir))
    staging_path = Path(staging_dir) / "build.sqlite3"

    meta = {
        "built_at": _now_iso(),
        "snapshot": str(snapshot),
        "targets": report["targets"],
        "parsed": report["parsed"],
        "schema": "what-1",
        "seg_separator": "U+0001",
    }
    # 先把所有解析/写库异常挡在发布之前
    stats = what_db.build_from_articles(staging_path, articles, entries=entries, meta=meta)
    conn = what_db.open_db(str(staging_path), readonly=True)
    try:
        summary = what_db.db_summary(conn)
    finally:
        conn.close()

    if summary["articles"] != report["unique_targets"]:
        print(
            f"[FAIL] 入库篇数 {summary['articles']} 与清单 {report['unique_targets']} 不一致，未发布",
            file=sys.stderr,
        )
        return EXIT_MANIFEST

    # 原子 no-clobber：`os.link` 目标已存在就失败。不能先 exists() 再 replace——
    # 中间别的人（或并发 build）创建了正式库时，replace 会把对方的文件覆盖掉。
    try:
        os.link(staging_path, final_path)
    except FileExistsError:
        print(
            f"[FAIL] 发布前目标正式库被创建（并发或晚到），未覆盖：{final_path}\n"
            f"       对方文件保持原样；本次结果保留在 {staging_path}",
            file=sys.stderr,
        )
        return EXIT_PUBLISH
    except OSError as exc:
        print(
            f"[FAIL] 发布切换失败：{exc}\n"
            f"       未留下半截正式库；本次结果保留在 {staging_path}",
            file=sys.stderr,
        )
        return EXIT_PUBLISH
    staging_path.unlink()
    published = True
    shutil.rmtree(staging_dir, ignore_errors=True)

    payload = {
        "published": published,
        "snapshot_report": report,
        "build_stats": stats.as_dict(),
        "db_summary": summary,
        "db_path": str(final_path),
        "db_bytes": final_path.stat().st_size if final_path.exists() else 0,
    }
    (build_dir / "build-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"build: {report['parsed']}/{report['unique_targets']} 篇解析入库；"
        f"schema {summary['by_schema']}；状态 {summary['by_status']}"
    )
    print(
        f"  facts {summary['facts']}　media {summary['media']}　chunks {summary['chunks']}"
        f"　terms {summary['terms']}　entities {summary['entities']}　aliases {summary['aliases']}"
    )
    print(f"  媒体 kind {summary['media_by_kind']}")
    print(f"  媒体 role {summary['media_by_role']}")
    print(
        f"  db {final_path} （{payload['db_bytes']} 字节，"
        f"user_version={summary['user_version']}，journal={summary['journal_mode']}）"
    )
    if report["duplicate_ids"]:
        print(f"  [WARN] 清单重复 ID {report['duplicate_ids']}", file=sys.stderr)
    if report["extra_detail_files"]:
        print(
            f"  [WARN] 快照里有不在清单的详情 {report['extra_detail_files'][:5]}",
            file=sys.stderr,
        )
    return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    db_path = Path(args.build_dir) / "what.sqlite3"
    if not db_path.exists():
        print(f"[FAIL] 库不存在：{db_path}（先跑 build）", file=sys.stderr)
        return EXIT_PRECONDITION
    conn = what_db.open_db(str(db_path), readonly=True)
    try:
        summary = what_db.db_summary(conn)
        levels = [
            int(row[0].split(":", 1)[1])
            for row in conn.execute(
                "SELECT DISTINCT qualifier FROM fact WHERE qualifier LIKE 'level:%'"
            )
        ]
        summary["levels_seen"] = sorted(levels)
        summary["field_keys"] = [
            row[0] for row in conn.execute("SELECT DISTINCT field_key FROM fact ORDER BY field_key")
        ]
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return EXIT_OK


def _read_intent(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    if args.intent:
        return json.loads(args.intent)
    if args.intent_file:
        return json.loads(Path(args.intent_file).read_text(encoding="utf-8"))
    print("[FAIL] 需要 --intent JSON 或 --intent-file 路径", file=sys.stderr)
    return None


def cmd_query(args: argparse.Namespace) -> int:
    db_path = Path(args.build_dir) / "what.sqlite3"
    if not db_path.exists():
        print(f"[FAIL] 库不存在：{db_path}（先跑 build）", file=sys.stderr)
        return EXIT_PRECONDITION
    intent = _read_intent(args)
    if intent is None:
        return EXIT_PRECONDITION
    conn = what_db.open_db(str(db_path), readonly=True)
    try:
        result = what_query.run_query(conn, intent)
    finally:
        conn.close()
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=1))
    return EXIT_OK


DEMO_INTENTS: List[Dict[str, Any]] = [
    {
        "label": "G01 优叶 90 级面板攻击（规范字段名）",
        "intent": {
            "mode": "field",
            "subject": {"name": "优叶", "kind": "traveler"},
            "fields": ["stat.attack"],
            "levels": [90],
        },
    },
    {
        "label": "G02 优叶生日 + 所属（多字段一次取齐）",
        "intent": {
            "mode": "field",
            "subject": {"name": "优叶", "kind": "traveler"},
            "fields": ["birthday", "affiliation"],
        },
    },
    {
        "label": "G03 泡沫主位/辅位效果（同页两块，不合并）",
        "intent": {
            "mode": "field",
            "subject": {"name": "泡沫", "kind": "sigil"},
            "fields": ["sigil.main_effect", "sigil.sub_effect"],
        },
    },
    {
        "label": "G04 优叶几岁（无数据源，不改查生日）",
        "intent": {
            "mode": "field",
            "subject": {"name": "优叶", "kind": "traveler"},
            "fields": ["age"],
        },
    },
    {
        "label": "G05 优叶几岁 + 生日混问（无源字段不吞掉可查字段）",
        "intent": {
            "mode": "field",
            "subject": {"name": "优叶", "kind": "traveler"},
            "fields": ["age", "birthday"],
        },
    },
    {
        "label": "G06 非法等级 95（列出可用等级，不杜撰）",
        "intent": {
            "mode": "field",
            "subject": {"name": "优叶", "kind": "traveler"},
            "fields": ["stat.attack"],
            "levels": [95],
        },
    },
    {
        "label": "G07 描述题同条约束：水系＋获得护盾时 的主位效果（709299 唯一）",
        "intent": {
            "mode": "field",
            "subject": {"name": None, "kind": "sigil"},
            "fields": ["sigil.main_effect"],
            "conditions": {"attribute": "水系", "trigger": "获得护盾时", "slot": "主位"},
        },
    },
    {
        "label": "G08 同一辅位效果 24 篇（体力之音 3 个 / 生命上限，全部候选保留）",
        "intent": {
            "mode": "article",
            "query": "进入星塔时，获得体力之音3个，每个提升0.3%生命上限。",
            "limit": 200,
        },
    },
    {
        "label": "G09 跨标点假命中（水、系伤害 必须被原文复核剔除）",
        "intent": {
            "mode": "article",
            "subject": {"name": "优叶", "kind": "traveler"},
            "query": "水系伤害",
        },
    },
    {
        "label": "G10 未收录角色",
        "intent": {
            "mode": "field",
            "subject": {"name": "不存在的旅人", "kind": "traveler"},
            "fields": ["birthday"],
        },
    },
    {
        "label": "G11 媒体题：优叶立绘分组（只出已判定 role）",
        "intent": {"mode": "media", "subject": {"name": "优叶", "kind": "traveler"}},
    },
    {
        "label": "G12 目录题：琥珀可用字段/分组/等级",
        "intent": {"mode": "catalog", "subject": {"name": "琥珀", "kind": "traveler"}},
    },
]


def cmd_demo(args: argparse.Namespace) -> int:
    db_path = Path(args.build_dir) / "what.sqlite3"
    if not db_path.exists():
        print(f"[FAIL] 库不存在：{db_path}（先跑 build）", file=sys.stderr)
        return EXIT_PRECONDITION
    conn = what_db.open_db(str(db_path), readonly=True)
    payload = []
    try:
        for case in DEMO_INTENTS:
            result = what_query.run_query(conn, case["intent"])
            payload.append(
                {"label": case["label"], "intent": case["intent"], "result": result.as_dict()}
            )
            print("=" * 78)
            print(f"[{case['label']}] status={result.status} message={result.message or '-'}")
            for cand in result.candidates[:4]:
                print(f"  - {cand.citation()} => {cand.value[:60]!r}")
            if len(result.candidates) > 4:
                print(f"  … 共 {len(result.candidates)} 条合格候选")
            if result.available_levels:
                print(f"  可用等级：{result.available_levels}")
            if result.rejected:
                print(f"  已剔除：{len(result.rejected)} 条（{result.rejected[0].get('reason')}）")
            if result.also_named:
                print(f"  同名候选：{[x['canonical_name'] for x in result.also_named]}")
    finally:
        conn.close()
    out = Path(args.build_dir) / "demo-results.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果已写入 {out}")
    return EXIT_OK


def _now_iso() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="stellasora_what 离线解析/建库/查询")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--build-dir", default=str(DEFAULT_BUILD_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="解析快照并建库（staging → 发布）").set_defaults(func=cmd_build)
    sub.add_parser("stats", help="打印库统计").set_defaults(func=cmd_stats)

    p_query = sub.add_parser("query", help="按结构化意图查询")
    p_query.add_argument("--intent", help="JSON 意图")
    p_query.add_argument("--intent-file", help="意图 JSON 文件路径")
    p_query.set_defaults(func=cmd_query)

    sub.add_parser("demo", help="跑一组真实语料演示查询").set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
