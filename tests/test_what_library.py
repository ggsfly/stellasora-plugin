#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`tools/what_library.py` 回归：分代读写、增量刷新、失败/取消纪律。

全部测试用**系统临时目录 + 注入的假 HTTP**，零联网、零真实 GameKee、零 QQ 发送。
运行：

    F:/MaiBot/.venv/Scripts/python.exe -X utf8 -B tests/test_what_library.py

覆盖：
  1. 首次建库 / 无库（pending）与损坏（corrupt）两类的分流
  2. 只读代固定 + 查询可用 + 旧代保留
  3. 增量分桶：新增 / 重抓 / 复用 / 删除 / 等戳抽验轮换与等戳异文
  4. 清单异常：重复 id、缺页、分页元信息漂移、total 不符
  5. 上游异常：429+Retry-After、验证页、5xx 重试、非法详情、错 id
  6. 发布闸门：抓取中取消、建库后取消、建库失败、校验失败 → 都不推进指针
  7. 并发：同 root 重入刷新被拒
  8. 出站纪律：白名单主机、串行间隔、响应体积上限
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import email.utils
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import what_db  # noqa: E402
import what_library  # noqa: E402
import what_query  # noqa: E402

HOW_OWNED = 714580  # gamekee.ELEMENT_PAGES['water']，how 正在消费的属性页
BASE_TIME = datetime(2026, 11, 26, 3, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 内容 fixture


def simple_editor(text: str) -> dict:
    return {"type": "simpleEditor", "data": [{"type": "paragraph", "children": [{"text": text}]}]}


def rich_paragraph(*kids) -> dict:
    return {"type": "paragraph", "children": list(kids)}


def image_node(url: str) -> dict:
    return {"type": "image", "src": url, "children": [{"text": ""}]}


def illustrated(block_type: str, data: dict) -> dict:
    return {"type": "illustrated-book", "data": [{"type": block_type, "data": data}]}


def profile_block(attrs) -> dict:
    return illustrated(
        "character-profile",
        {
            "title": "基础信息",
            "name": simple_editor(""),
            "descTitle": simple_editor(""),
            "desc": simple_editor(""),
            "attrList": [
                {"title": simple_editor(label), "content": simple_editor(value)}
                for label, value in attrs
            ],
            "imageList": [],
            "imagesList": [],
        },
    )


def fight_info(levels) -> dict:
    return illustrated(
        "fight-info",
        {
            "title": "属性",
            "label": "Lv",
            "levelList": [
                {
                    "level": level,
                    "levelInfoList": [
                        {"title": simple_editor("攻击"), "content": simple_editor(str(atk))},
                        {"title": simple_editor("生命"), "content": simple_editor(str(hp))},
                    ],
                }
                for level, atk, hp in levels
            ],
        },
    )


def skill_info(title: str, entries) -> dict:
    return illustrated(
        "skill-info",
        {
            "title": title,
            "isShowTabs": False,
            "tabs": [],
            "skillList": [
                {
                    "name": simple_editor(name) if name else {"type": "simpleEditor", "data": []},
                    "desc": simple_editor(desc),
                }
                for name, desc in entries
            ],
        },
    )


TRAVELER_NODES = [
    profile_block([("生日", "3月5日"), ("所属", "地理协会")]),
    fight_info([(1, 100, 700), ("90", 7484, 51179)]),
]
SIGIL_NODES = [
    skill_info("主位效果", [("梦幻泡影", "全队水系伤害提升6%。")]),
    skill_info("辅位效果", [("", "进入星塔时，获得水之音3个。")]),
]
MEDIA_ONLY_NODES = [rich_paragraph(image_node("//cdnimg-v2.gamekee.com/a.png"))]
AMBAR_NODES = [profile_block([("生日", "7月7日")])]


# ---------------------------------------------------------------- 假上游


class Game:
    """一份可控的假 wiki：树 + 清单 + 详情都按真实响应外壳生成。"""

    def __init__(self) -> None:
        self.articles = {}
        self.page_size = 100
        self.duplicate_first_row = False
        self.list_inserted = []

    def add(self, cid, name, *, section="旅人图鉴", type=4, updated_at=1795000000,
            nodes=None, in_tree=None) -> None:
        self.articles[cid] = {
            "cid": cid,
            "name": name,
            "section": section,
            "type": type,
            "updated_at": updated_at,
            "nodes": nodes if nodes is not None else [rich_paragraph({"text": f"{name}正文"})],
            "in_tree": (section is not None) if in_tree is None else in_tree,
        }

    def remove(self, cid) -> None:
        self.articles.pop(cid, None)

    def touch(self, cid, updated_at) -> None:
        self.articles[cid]["updated_at"] = updated_at

    def set_nodes(self, cid, nodes) -> None:
        self.articles[cid]["nodes"] = nodes

    def set_type(self, cid, type_) -> None:
        self.articles[cid]["type"] = type_

    # -- 响应外壳

    def tree_payload(self) -> dict:
        groups = {}
        for cid, article in self.articles.items():
            if not article["in_tree"]:
                continue
            groups.setdefault(article["section"], []).append((cid, article["name"]))
        data = []
        for index, (section, items) in enumerate(groups.items()):
            node_id = 2000 + index
            data.append(
                {
                    "id": node_id,
                    "name": section,
                    "type": 1,
                    "child": [
                        {"id": 3000 + offset, "name": name, "pid": node_id,
                         "content_id": cid, "type": 1, "child": None}
                        for offset, (cid, name) in enumerate(items)
                    ],
                }
            )
        return {"code": 0, "msg": "成功", "data": data}

    def list_rows(self) -> list:
        rows = [
            {
                "id": cid,
                "type": article["type"],
                "title": article["name"],
                "updated_at": article["updated_at"],
                "entry_id": 9000 + cid,
                "summary": "",
                "image_num": 0,
                "word_count": 10,
            }
            for cid, article in self.articles.items()
        ]
        rows.sort(key=lambda row: row["id"])
        if self.duplicate_first_row and rows:
            rows.insert(1, dict(rows[0]))
        return rows

    def page_count(self) -> int:
        return max(1, math.ceil(len(self.list_rows()) / self.page_size))

    def page_payload(self, page_no: int) -> dict:
        rows = self.list_rows()
        start = (page_no - 1) * self.page_size
        return {
            "code": 0,
            "msg": "",
            "data": rows[start:start + self.page_size],
            "meta": {
                "request_id": "request:test",
                "pagination": {
                    "page_no": page_no,
                    "limit": self.page_size,
                    "page_total": self.page_count(),
                    "total": len(rows),
                },
            },
        }

    def detail_payload(self, cid) -> dict | None:
        article = self.articles.get(cid)
        if article is None:
            return None
        return {
            "code": 0,
            "msg": "成功",
            "data": {
                "id": cid,
                "content_id": cid,
                "type": article["type"],
                "title": article["name"],
                "summary": "",
                "entry_id": 9000 + cid,
                "image_num": 0,
                "word_count": 10,
                "updated_at": article["updated_at"],
                "content_json": json.dumps(article["nodes"], ensure_ascii=False),
            },
        }


def http_date(moment: datetime) -> str:
    """把时刻写成上游真实写法（RFC 1123、GMT）。"""
    return email.utils.format_datetime(moment).replace("+0000", "GMT")


def json_response(payload: dict) -> what_library.FetchResponse:
    return what_library.FetchResponse(
        status=200,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8"},
    )


class FakeGameKee:
    """注入式传输层：记录每次请求，可按 key 注入失败/异文/慢动作钩子。"""

    def __init__(self, game: Game) -> None:
        self.game = game
        self.calls = []
        self.after_call = None      # Callable[[str], None]，在返回响应前调用
        self.overrides = {}         # key -> FetchResponse | callable
        self.flaky = {}             # key -> {"response": FetchResponse, "times": n}

    def key_for(self, path: str, query) -> str:
        if path == what_library.TREE_PATH:
            return "tree"
        if path == what_library.LIST_PATH:
            page_no = int((query.get("page_no") or ["1"])[0])
            return f"page:{page_no}"
        if path.startswith("/v1/content/detail/"):
            return f"detail:{path.rsplit('/', 1)[-1]}"
        return f"other:{path}"

    def fetch(self, url, *, max_bytes, timeout):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        key = self.key_for(path, query)
        self.calls.append(
            {"url": url, "path": path, "key": key, "max_bytes": max_bytes, "timeout": timeout}
        )
        if self.after_call is not None:
            self.after_call(key)

        flaky = self.flaky.get(key)
        if flaky and flaky["times"] > 0:
            flaky["times"] -= 1
            return flaky["response"]

        override = self.overrides.get(key)
        if override is not None:
            return override() if callable(override) else override

        if key == "tree":
            return json_response(self.game.tree_payload())
        if key.startswith("page:"):
            return json_response(self.game.page_payload(int(key.split(":", 1)[1])))
        if key.startswith("detail:"):
            payload = self.game.detail_payload(int(key.split(":", 1)[1]))
            if payload is None:
                return what_library.FetchResponse(status=404)
            return json_response(payload)
        return what_library.FetchResponse(status=404)

    def detail_calls(self):
        return [call for call in self.calls if call["key"].startswith("detail:")]

    def detail_ids(self):
        return [int(call["key"].split(":", 1)[1]) for call in self.detail_calls()]


class Clock:
    """每次读表 +1 秒，便于断言时间戳单调前进。"""

    def __init__(self, start=BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class FixedClock:
    """恒定时钟：反复读表都返回同一时刻，冷却秒数可精确断言（不卡壁钟）。"""

    def __init__(self, now=BASE_TIME) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Sleeper:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, seconds) -> None:
        self.calls.append(round(float(seconds), 3))


# ---------------------------------------------------------------- 基础场景


def build_game(**overrides) -> Game:
    game = Game()
    game.add(1001, "优叶", section="旅人图鉴", nodes=TRAVELER_NODES)
    game.add(1002, "泡沫", section="秘纹图鉴", nodes=SIGIL_NODES)
    game.add(1003, "加载漫画", section="攻略区", nodes=MEDIA_ONLY_NODES)
    game.add(HOW_OWNED, "水系阵容推荐", section=None, in_tree=False)
    game.add(1004, "其它类型条目", section=None, in_tree=False, type=1)
    for key, value in overrides.items():
        setattr(game, key, value)
    return game


class LibraryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "what"
        self.game = build_game()
        self.server = FakeGameKee(self.game)
        self.clock = Clock()
        self.sleeper = Sleeper()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, *, verify_ratio=0.0, root=None, sleep=None, clock=None, **kwargs):
        library = what_library.WhatLibrary(
            root or self.root,
            fetch=self.server.fetch,
            clock=clock or self.clock,
            sleep=sleep or self.sleeper,
            verify_ratio=verify_ratio,
            **kwargs,
        )
        return library

    # -- 小工具

    def generations(self):
        return sorted(path.name for path in self.root.glob("kb-*.sqlite3"))

    def pointer(self):
        return json.loads((self.root / what_library.POINTER_NAME).read_text(encoding="utf-8"))

    def article_count(self, db_path) -> int:
        conn = what_db.open_db(str(db_path), readonly=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM article").fetchone()[0])
        finally:
            conn.close()

    def query_attack(self, conn) -> what_query.QueryResult:
        return what_query.run_query(
            conn,
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["stat.attack"],
                "levels": [90],
            },
        )


# ---------------------------------------------------------------- 只读侧


class ReadSideTests(LibraryTestCase):
    def test_no_generation_is_pending(self):
        library = self.make()
        self.assertFalse(self.root.exists())  # 构造不落盘
        with self.assertRaises(what_library.WhatLibraryUnavailable) as raised:
            with library.open_current():
                pass
        error = raised.exception
        self.assertEqual(error.kind, "pending")
        self.assertEqual(error.reason, "no_generation")
        self.assertIn("/st_update", error.user_text)
        self.assertEqual(self.server.calls, [])  # 只读路径零网络

    def test_staging_only_is_pending_not_corrupt(self):
        (self.root / "staging").mkdir(parents=True)
        (self.root / "staging" / "build-dead.sqlite3").write_bytes(b"")
        library = self.make()
        with self.assertRaises(what_library.WhatLibraryUnavailable) as raised:
            with library.open_current():
                pass
        self.assertEqual(raised.exception.kind, "pending")
        self.assertEqual(raised.exception.reason, "staging_only")

    def test_corrupt_pointer_is_corrupt(self):
        library = self.make()
        self.root.mkdir(parents=True)
        (self.root / what_library.POINTER_NAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(what_library.WhatLibraryUnavailable) as raised:
            with library.open_current():
                pass
        self.assertEqual(raised.exception.kind, "corrupt")
        self.assertEqual(raised.exception.user_text, "读取失败")

    def test_pointer_to_missing_db_is_corrupt(self):
        library = self.make()
        self.root.mkdir(parents=True)
        (self.root / what_library.POINTER_NAME).write_text(
            json.dumps(
                {
                    "schema": what_library.POINTER_SCHEMA,
                    "generation": "kb-gone",
                    "db_file": "kb-gone.sqlite3",
                    "built_at": "2026-11-26T03:00:00+00:00",
                    "last_verified": "2026-11-26T03:00:00+00:00",
                    "articles": 3,
                    "listed": 3,
                    "coverage": {},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(what_library.WhatLibraryUnavailable) as raised:
            with library.open_current():
                pass
        self.assertEqual(raised.exception.kind, "corrupt")

    def test_pointer_path_traversal_rejected(self):
        library = self.make()
        self.root.mkdir(parents=True)
        (self.root / what_library.POINTER_NAME).write_text(
            json.dumps({"generation": "kb-x", "db_file": "../kb-x.sqlite3"}),
            encoding="utf-8",
        )
        with self.assertRaises(what_library.WhatLibraryUnavailable) as raised:
            with library.open_current():
                pass
        self.assertEqual(raised.exception.kind, "corrupt")

    def test_generation_count_mismatch_is_corrupt(self):
        library = self.make()
        library.refresh()
        pointer = self.pointer()
        pointer["articles"] = 1  # 指针声明 1 篇，库里实际 3 篇
        (self.root / what_library.POINTER_NAME).write_text(
            json.dumps(pointer, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(what_library.WhatLibraryUnavailable) as raised:
            with library.open_current():
                pass
        self.assertEqual(raised.exception.kind, "corrupt")
        self.assertEqual(raised.exception.reason, "article_count")


# ---------------------------------------------------------------- 首次建库


class FirstBuildTests(LibraryTestCase):
    def test_first_build_publishes_readable_generation(self):
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertTrue(report.first_build)
        self.assertFalse(report.old_generation_ready)
        self.assertEqual(report.listed, 3)
        self.assertEqual(report.articles, 3)
        self.assertEqual(
            (report.added, report.refetched, report.reused, report.removed, report.verified),
            (3, 0, 0, 0, 0),
        )
        self.assertEqual(report.media_only, 1)
        self.assertEqual(report.partial_parse, 0)

        pointer = self.pointer()
        self.assertEqual(pointer["generation"], report.generation)
        self.assertEqual(pointer["articles"], 3)
        self.assertEqual(pointer["coverage"]["list_pages"], 1)
        self.assertEqual(pointer["coverage"]["list_total"], 5)
        self.assertEqual(pointer["coverage"]["excluded_type"], 1)
        self.assertEqual(pointer["coverage"]["target_count"], 3)
        self.assertEqual(pointer["coverage"]["excluded_how_owned"], 1)

        with library.open_current() as (conn, generation):
            self.assertEqual(generation.name, report.generation)
            self.assertEqual(
                generation.as_meta(),
                {
                    "generation": report.generation,
                    "built_at": pointer["built_at"],
                    "last_verified": pointer["last_verified"],
                    "article_count": 3,
                },
            )
            result = self.query_attack(conn)
            self.assertEqual(result.status, "ok")
            self.assertEqual([candidate.value for candidate in result.candidates], ["7484"])
            self.assertEqual(result.candidates[0].qualifier, "level:90")
        self.assertEqual(self.generations(), [f"{report.generation}.sqlite3"])

    def test_excluded_rows_never_fetched(self):
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(sorted(self.server.detail_ids()), [1001, 1002, 1003])
        self.assertNotIn(HOW_OWNED, self.server.detail_ids())

    def test_refresh_state_records_attempt(self):
        library = self.make()
        report = library.refresh()
        state = json.loads((self.root / what_library.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["last_attempt_at"], report.last_attempt_at)
        self.assertEqual(state["last_verified"], report.last_verified)
        self.assertIsNone(state.get("last_error"))


# ---------------------------------------------------------------- 增量


class IncrementalTests(LibraryTestCase):
    def test_incremental_buckets_new_refetch_reuse_remove(self):
        library = self.make()
        first = library.refresh()
        self.assertEqual(first.status, what_library.STATUS_PUBLISHED)

        # 1002 清单时间戳变了 → 重抓；1001 不变 → 复用；1005 新增；1003 从清单消失
        self.game.touch(1002, 1795100000)
        self.game.add(1005, "琥珀", section="旅人图鉴", nodes=AMBAR_NODES)
        self.game.remove(1003)
        self.server.calls.clear()

        second = library.refresh()
        self.assertEqual(second.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(
            (second.added, second.refetched, second.reused, second.removed, second.verified),
            (1, 1, 1, 1, 0),
        )
        self.assertEqual(second.articles, 3)
        self.assertEqual(sorted(self.server.detail_ids()), [1002, 1005])

        # 新代与旧代并存，旧代仍可读
        self.assertEqual(len(self.generations()), 2)
        self.assertEqual(self.article_count(self.root / f"{first.generation}.sqlite3"), 3)

        with library.open_current() as (conn, generation):
            self.assertEqual(generation.name, second.generation)
            self.assertEqual(
                self.query_attack(conn).candidates[0].value,
                "7484",  # 1001 的正文由旧代 raw_gzip 复用，事实没丢
            )

    def test_unchanged_refresh_republishes_and_advances_last_verified(self):
        library = self.make()
        first = library.refresh()
        self.server.calls.clear()
        second = library.refresh()
        self.assertEqual(second.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(
            (second.added, second.refetched, second.reused),
            (0, 0, 3),
        )
        self.assertEqual(second.removed, 0)
        self.assertEqual(second.previous_generation, first.generation)
        self.assertEqual(second.previous_last_verified, first.last_verified)
        self.assertEqual(second.listed, 3)
        self.assertNotEqual(second.last_verified, first.last_verified)
        self.assertEqual(self.server.detail_ids(), [])  # 0 抽验时零详情请求
        self.assertEqual(len(self.generations()), 2)

    def test_full_refresh_refetches_everything(self):
        library = self.make()
        library.refresh()
        self.server.calls.clear()
        report = library.refresh(full=True)
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertEqual((report.added, report.refetched, report.reused), (0, 3, 0))
        self.assertEqual(sorted(self.server.detail_ids()), [1001, 1002, 1003])

    def test_sampling_rotates_across_refreshes(self):
        library = self.make(verify_ratio=0.34)  # 3 篇 → 每轮抽 1 篇
        library.refresh()
        self.server.calls.clear()
        self.assertEqual(self.server.detail_ids(), [])  # 首建全是新增，不抽验

        second = library.refresh()
        self.assertEqual(second.verified, 1)
        self.assertEqual(second.reused, 2)
        self.assertEqual(self.server.detail_ids(), [1001])

        self.server.calls.clear()
        third = library.refresh()
        self.assertEqual(third.verified, 1)
        self.assertEqual(self.server.detail_ids(), [1002])  # 游标轮换到下一篇

        state = json.loads((self.root / what_library.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["rotation_cursor"], 2)

    def test_equal_stamp_hash_mismatch_promotes_whole_batch(self):
        """计划 §2.4：抽验发现等戳异文 → 同批把其余等戳条目抓齐再发布。"""
        library = self.make(verify_ratio=0.34)
        library.refresh()
        # 时间戳不变但正文变了（模拟上游静默改稿）
        self.game.set_nodes(1001, [profile_block([("生日", "3月6日")])])
        self.server.calls.clear()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(report.verified, 0)
        self.assertEqual(report.reused, 0)     # 没有「悄悄复用旧正文」的篇
        self.assertEqual(report.refetched, 3)  # 1 篇异文 + 2 篇被同批抓齐
        self.assertEqual(self.server.detail_ids(), [1001, 1002, 1003])
        reason = next(r for r in report.reasons if r["code"] == "equal_stamp_hash_mismatch")
        self.assertEqual(reason["content_id"], 1001)
        self.assertEqual(reason["promoted"], [1002, 1003])
        with library.open_current() as (conn, _generation):
            birthday = what_query.run_query(
                conn,
                {
                    "mode": "field",
                    "subject": {"name": "优叶", "kind": "traveler"},
                    "fields": ["birthday"],
                },
            )
            self.assertEqual([candidate.value for candidate in birthday.candidates], ["3月6日"])

    def test_equal_stamp_promotion_failure_keeps_published_generation(self):
        """升级抓齐时任一失败 → 整批停：指针不动，已发布代继续可读。"""
        library = self.make(verify_ratio=0.34)
        first = library.refresh()
        self.game.set_nodes(1001, [profile_block([("生日", "3月6日")])])
        self.server.overrides["detail:1003"] = what_library.FetchResponse(status=503, body=b"")
        self.server.calls.clear()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "http_status")
        self.assertEqual(report.failed, 1)
        self.assertEqual(self.pointer()["generation"], first.generation)
        self.assertEqual(self.generations(), [f"{first.generation}.sqlite3"])
        self.assertEqual(report.last_verified, first.last_verified)
        with library.open_current() as (conn, generation):
            self.assertEqual(generation.name, first.generation)
            self.assertEqual(self.query_attack(conn).candidates[0].value, "7484")

    def test_equal_stamp_promotion_does_not_refetch_sampled_twice(self):
        """不重复已重抓项：抽验篇与升级篇各请求一次。"""
        library = self.make(verify_ratio=0.34)
        library.refresh()
        self.game.set_nodes(1001, [profile_block([("生日", "3月6日")])])
        self.server.calls.clear()
        library.refresh()
        ids = self.server.detail_ids()
        self.assertEqual(len(ids), len(set(ids)))

    def test_reuse_failure_falls_back_to_refetch(self):
        library = self.make()
        first = library.refresh()
        # 破坏旧代的 raw_gzip：复用解析会失败，该篇改走重抓
        db_path = self.root / f"{first.generation}.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE article SET raw_gzip = ? WHERE content_id = 1001", (b"not gzip",))
        conn.commit()
        conn.close()

        self.server.calls.clear()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(report.reused, 2)
        self.assertEqual(report.refetched, 1)
        self.assertIn(1001, self.server.detail_ids())
        self.assertIn("reuse_failed", [reason["code"] for reason in report.reasons])


# ---------------------------------------------------------------- 清单异常


class ManifestFailureTests(LibraryTestCase):
    def test_duplicate_list_ids_abort_without_publish(self):
        library = self.make()
        library.refresh()
        before = self.pointer()
        self.game.duplicate_first_row = True
        self.server.calls.clear()

        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "duplicate_list_ids")
        self.assertIsNone(report.generation)
        self.assertTrue(report.old_generation_ready)
        self.assertEqual(self.pointer()["generation"], before["generation"])
        self.assertEqual(self.generations(), [f"{before['generation']}.sqlite3"])

    def test_empty_page_is_page_gap(self):
        self.game.page_size = 2  # 5 行 → 3 页
        library = self.make()
        self.server.overrides["page:2"] = json_response(
            {
                "code": 0,
                "msg": "",
                "data": [],
                "meta": {"pagination": {"page_no": 2, "limit": 2, "page_total": 3, "total": 5}},
            }
        )
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "page_gap")
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())

    def test_pagination_meta_drift_aborts(self):
        self.game.page_size = 2
        library = self.make()
        self.server.overrides["page:2"] = json_response(
            {
                "code": 0,
                "msg": "",
                "data": self.game.page_payload(2)["data"],
                "meta": {"pagination": {"page_no": 2, "limit": 2, "page_total": 4, "total": 5}},
            }
        )
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "contract_drift")

    def test_missing_pagination_aborts(self):
        library = self.make()
        self.server.overrides["page:1"] = json_response(
            {"code": 0, "msg": "", "data": self.game.page_payload(1)["data"]}
        )
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "contract_drift")

    def test_list_total_mismatch_aborts(self):
        self.game.page_size = 2  # 5 行 → 3 页
        library = self.make()

        def page(page_no):
            return json_response(
                {
                    "code": 0,
                    "msg": "",
                    "data": self.game.page_payload(page_no)["data"],
                    "meta": {"pagination": {"page_no": page_no, "limit": 2, "page_total": 3, "total": 6}},
                }
            )

        for number in (1, 2, 3):
            self.server.overrides[f"page:{number}"] = page(number)
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "list_total_mismatch")


# ---------------------------------------------------------------- 上游异常


class UpstreamFailureTests(LibraryTestCase):
    def test_429_stops_batch_and_records_retry_after(self):
        library = self.make()
        self.server.overrides["detail:1001"] = what_library.FetchResponse(
            status=429, body=b"", headers={"Retry-After": "120"}
        )
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "rate_limited")
        self.assertEqual(report.retry_after, 120.0)
        allowance = datetime.fromisoformat(report.next_allowed_at)
        self.assertGreaterEqual((allowance - datetime.fromisoformat(report.last_attempt_at)).total_seconds(), 100)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.done, 0)
        self.assertEqual(report.total, 3)
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())
        # 429 不重试、不加压：sleep 里只有正常间隔
        self.assertEqual(self.sleeper.calls, [0.12, 0.12])

    def test_429_without_retry_after_uses_cooldown_baseline(self):
        library = self.make()
        self.server.overrides["detail:1001"] = what_library.FetchResponse(status=429, body=b"")
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "rate_limited")
        self.assertIsNone(report.retry_after)
        state = json.loads((self.root / what_library.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state["last_error"], "rate_limited")
        self.assertIsNotNone(state.get("next_allowed_at"))

    def test_verification_page_stops_batch(self):
        library = self.make()
        self.server.overrides["tree"] = what_library.FetchResponse(
            status=200, body=b"<html><body>verify</body></html>", headers={"content-type": "text/html"}
        )
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "verification_page")
        self.assertEqual(report.stage, "tree")
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())

    def test_auth_denied_stops_batch(self):
        library = self.make()
        self.server.overrides["detail:1002"] = what_library.FetchResponse(status=403, body=b"")
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "auth_denied")
        self.assertEqual(report.done, 1)  # 第一篇已读，第三篇没发

    def test_5xx_is_retried_with_plan_intervals(self):
        library = self.make()
        self.server.flaky["detail:1001"] = {
            "response": what_library.FetchResponse(status=503, body=b""),
            "times": 2,
        }
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(report.articles, 3)
        # 每次尝试都先走正常间隔，重试再额外 4s、12s（不因为重试跳过节奏）
        self.assertEqual(self.sleeper.calls, [0.12, 0.12, 4.0, 0.12, 12.0, 0.12, 0.12, 0.12])

    def test_detail_id_mismatch_aborts(self):
        library = self.make()
        wrong = self.game.detail_payload(1002)
        wrong["data"]["content_id"] = 9999
        self.server.overrides["detail:1002"] = json_response(wrong)
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "detail_id_mismatch")
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.done, 1)
        reasons = [reason for reason in report.reasons if reason["code"] == "detail_id_mismatch"]
        self.assertEqual(reasons[0]["content_id"], 1002)
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())

    def test_invalid_detail_without_content_json_aborts(self):
        library = self.make()
        broken = self.game.detail_payload(1001)
        broken["data"].pop("content_json")
        self.server.overrides["detail:1001"] = json_response(broken)
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "invalid_detail")

    def test_oversize_body_aborts(self):
        library = self.make()
        self.server.overrides["detail:1001"] = what_library.FetchResponse(
            status=200, body=b"{" + b" " * (what_library.MAX_DETAIL_BYTES + 1) + b"}"
        )
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "oversize")


# ---------------------------------------------------------------- 解析闸门


class ParseGateTests(LibraryTestCase):
    """坏正文 / 未知结构 / 关键缺失一律不推进代与指针；`media_only` 照常保留。"""

    def _break(self, cid: int, content_json: str) -> None:
        payload = self.game.detail_payload(cid)
        payload["data"]["content_json"] = content_json
        self.server.overrides[f"detail:{cid}"] = json_response(payload)

    def test_first_build_bad_json_is_refused(self):
        self._break(1001, "not json")           # 坏正文：json.loads 直接失败
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "bad_article")
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.stage, "detail")
        self.assertIsNone(report.generation)
        self.assertIsNone(report.last_verified)
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())
        self.assertEqual(self.generations(), [])

    def test_unknown_schema_is_refused(self):
        self._break(1001, json.dumps({"foo": 1}, ensure_ascii=False))
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "bad_article")
        self.assertIn("unknown_schema", report.error)
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())

    def test_empty_content_json_is_refused(self):
        self._break(1001, "")
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "bad_article")
        self.assertIn("empty_content", report.error)
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())

    def test_bad_article_patch_keeps_old_generation_and_pointer(self):
        library = self.make()
        first = library.refresh()
        self.assertEqual(first.status, what_library.STATUS_PUBLISHED)
        self._break(1002, "not json")
        self.server.calls.clear()
        report = library.refresh(full=True)
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "bad_article")
        # 旧库/指针原样：代名、last_verified、库文件都没被动过
        self.assertEqual(self.pointer()["generation"], first.generation)
        self.assertEqual(self.pointer()["last_verified"], first.last_verified)
        self.assertEqual(report.last_verified, first.last_verified)
        self.assertTrue(report.old_generation_ready)
        self.assertEqual(self.generations(), [f"{first.generation}.sqlite3"])
        with library.open_current() as (conn, generation):
            self.assertEqual(generation.name, first.generation)
            self.assertEqual(self.query_attack(conn).candidates[0].value, "7484")

    def test_media_only_article_is_still_published(self):
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(report.media_only, 1)     # 纯图页合法，不因闸门被拒
        self.assertEqual(report.articles, 3)
        with library.open_current() as (conn, _generation):
            rows = conn.execute(
                "SELECT availability FROM article ORDER BY content_id"
            ).fetchall()
        self.assertEqual(
            [row[0] for row in rows], ["ok", "ok", "media_only"]
        )


# ---------------------------------------------------------------- 冷却闸门


class CooldownTests(LibraryTestCase):
    """429 落的 `next_allowed_at` 必须挡住下一次 `/st_update` 的每一个请求。

    全部用 `FixedClock`（恒定时钟）：冷却秒数、`next_allowed_at` 与「到点」都按
    绝对 HTTP-date 精确断言，不靠秒差容差去卡壁钟。
    """

    def _state(self) -> dict:
        return json.loads((self.root / what_library.STATE_NAME).read_text(encoding="utf-8"))

    def frozen(self, **kwargs):
        """恒定时钟 + 独立库：返回 (library, fixed_clock)。"""
        fixed = FixedClock(BASE_TIME)
        return self.make(clock=fixed, **kwargs), fixed

    def _throttle(self, library, header: str):
        self.server.overrides["detail:1001"] = what_library.FetchResponse(
            status=429, body=b"", headers={"Retry-After": header}
        )
        return library.refresh()

    def test_second_refresh_within_cooldown_sends_nothing(self):
        library, _fixed = self.frozen()
        first = self._throttle(library, "120")
        self.assertEqual(first.error_code, "rate_limited")
        self.assertEqual(first.retry_after, 120.0)
        # 绝对时刻精确断言（恒定时钟：T0 + 120s）
        self.assertEqual(first.last_attempt_at, BASE_TIME.isoformat())
        self.assertEqual(
            first.next_allowed_at, (BASE_TIME + timedelta(seconds=120)).isoformat()
        )
        self.server.overrides.pop("detail:1001")
        self.server.calls.clear()

        second = library.refresh()   # 冷却期内再点一次 /st_update
        self.assertEqual(second.status, what_library.STATUS_FAILED)
        self.assertEqual(second.error_code, "cooldown")
        self.assertEqual(second.stage, "cooldown")
        self.assertEqual(self.server.calls, [])       # 连树 GET 都没发
        self.assertEqual(second.done, 0)
        self.assertIsNone(second.generation)
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())
        # 冷却时刻不清空、不缩短
        self.assertEqual(
            self._state()["next_allowed_at"],
            (BASE_TIME + timedelta(seconds=120)).isoformat(),
        )

    def test_cooldown_blocks_after_published_generation_too(self):
        library, _fixed = self.frozen()
        first = library.refresh()
        self.game.touch(1001, 1795100000)
        throttled = self._throttle(library, "600")
        self.assertEqual(throttled.error_code, "rate_limited")
        self.server.overrides.pop("detail:1001")
        self.server.calls.clear()
        blocked = library.refresh()
        self.assertEqual(blocked.error_code, "cooldown")
        self.assertEqual(blocked.previous_generation, first.generation)
        self.assertEqual(self.pointer()["generation"], first.generation)
        self.assertEqual(self.server.calls, [])

    def test_cooldown_lifts_exactly_at_deadline(self):
        library, fixed = self.frozen()
        first = self._throttle(library, "120")
        self.server.overrides.pop("detail:1001")
        self.server.calls.clear()
        fixed.now = datetime.fromisoformat(first.next_allowed_at)   # 正到点
        again = library.refresh()
        self.assertEqual(again.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(sorted(self.server.detail_ids()), [1001, 1002, 1003])
        self.assertIsNone(self._state().get("next_allowed_at"))   # 发布成功后清冷却

    def test_illegal_retry_after_falls_back_to_default_cooldown(self):
        for header in (
            "NaN",                                      # 数字但非有限
            "Infinity",
            "-5",                                       # 负数
            "not-a-date",                               # 乱串
            "Wed, 99 Zzz 2015 25:99:99 GMT",            # 真正无效的日期
            "1e15",                                     # 超出 datetime 可表示范围
        ):
            with self.subTest(header=header):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "what"
                    server = FakeGameKee(self.game)
                    server.overrides["detail:1001"] = what_library.FetchResponse(
                        status=429, body=b"", headers={"Retry-After": header}
                    )
                    library = what_library.WhatLibrary(
                        root,
                        fetch=server.fetch,
                        clock=FixedClock(BASE_TIME),
                        sleep=Sleeper(),
                        verify_ratio=0.0,
                    )
                    report = library.refresh()
                    self.assertEqual(report.error_code, "rate_limited")
                    self.assertIsNone(report.retry_after)
                    self.assertEqual(report.last_attempt_at, BASE_TIME.isoformat())
                    self.assertEqual(
                        report.next_allowed_at,
                        (BASE_TIME + timedelta(seconds=what_library.NO_RETRY_AFTER_COOLDOWN))
                        .isoformat(),
                    )

    def test_retry_after_parser_rejects_non_finite(self):
        for header in ("NaN", "Infinity", "-Infinity", "-5", "", "soon", "not-a-date",
                       "Wed, 99 Zzz 2015 25:99:99 GMT", "1e15"):
            with self.subTest(header=header):
                self.assertIsNone(what_library._retry_after_seconds({"retry-after": header}))
        # 数字秒照旧：合法值直接采用、0 合法
        self.assertEqual(what_library._retry_after_seconds({"Retry-After": "120"}), 120.0)
        self.assertEqual(what_library._retry_after_seconds({"retry-after": "0"}), 0.0)

    def test_retry_after_parser_honours_http_date(self):
        # 合法 HTTP-date 与数字秒同等尊重：按注入时钟算剩余秒（纯函数，可精确断言）
        two_hours = http_date(BASE_TIME + timedelta(hours=2))
        self.assertEqual(
            what_library._retry_after_seconds({"retry-after": two_hours}, now=BASE_TIME),
            7200.0,
        )
        # 过去的日期 → 0（立即可再试），不是无穷冷却
        past = http_date(BASE_TIME - timedelta(hours=1))
        self.assertEqual(
            what_library._retry_after_seconds({"retry-after": past}, now=BASE_TIME), 0.0
        )
        # 合法长期限不封顶：48h 数字秒与 48h date 都原样保留（代码不替上游缩短）
        self.assertEqual(
            what_library._retry_after_seconds({"retry-after": "172800"}, now=BASE_TIME),
            172800.0,
        )
        two_days = http_date(BASE_TIME + timedelta(hours=48))
        self.assertEqual(
            what_library._retry_after_seconds({"retry-after": two_days}, now=BASE_TIME),
            172800.0,
        )
        # 十年后的 date 也保留（只要 datetime 表示得出来）
        decade = http_date(BASE_TIME + timedelta(days=3650))
        self.assertEqual(
            what_library._retry_after_seconds({"retry-after": decade}, now=BASE_TIME),
            3650 * 86400.0,
        )
        # 只有非有限/超出表示范围才无效
        self.assertIsNone(what_library._retry_after_valid(float("inf"), BASE_TIME))
        self.assertIsNone(what_library._retry_after_valid(1e15, BASE_TIME))
        self.assertEqual(what_library._retry_after_valid(172800.0, BASE_TIME), 172800.0)

    def test_forty_eight_hour_retry_after_is_not_capped(self):
        """上游要求 48h：就等 48h，不得折回 30 分钟（数字秒写法）。"""
        library, _fixed = self.frozen()
        first = self._throttle(library, "172800")
        self.assertEqual(first.error_code, "rate_limited")
        self.assertEqual(first.retry_after, 172800.0)
        self.assertEqual(
            first.next_allowed_at, (BASE_TIME + timedelta(hours=48)).isoformat()
        )
        self.server.overrides.pop("detail:1001")
        self.server.calls.clear()
        blocked = library.refresh()
        self.assertEqual(blocked.error_code, "cooldown")
        self.assertEqual(self.server.calls, [])
        self.assertEqual(
            self._state()["next_allowed_at"], (BASE_TIME + timedelta(hours=48)).isoformat()
        )

    def test_forty_eight_hour_http_date_is_not_capped(self):
        """48h 以 HTTP-date 给出时同样保留（不被任何策略上限折回）。"""
        library, _fixed = self.frozen()
        header = http_date(BASE_TIME + timedelta(hours=48))
        first = self._throttle(library, header)
        self.assertEqual(first.error_code, "rate_limited")
        self.assertEqual(first.retry_after, 172800.0)
        self.assertEqual(
            first.next_allowed_at, (BASE_TIME + timedelta(hours=48)).isoformat()
        )

    def test_http_date_two_hours_blocks_second_refresh_until_deadline(self):
        """合法 date 按服务要求的 2 小时尊重：期间第二次零请求，到点再抓。"""
        library, fixed = self.frozen()
        deadline = BASE_TIME + timedelta(hours=2)
        first = self._throttle(library, http_date(deadline))
        self.assertEqual(first.error_code, "rate_limited")
        self.assertEqual(first.retry_after, 7200.0)
        self.assertEqual(first.last_attempt_at, BASE_TIME.isoformat())
        self.assertEqual(first.next_allowed_at, deadline.isoformat())
        self.assertEqual(
            (datetime.fromisoformat(first.next_allowed_at)
             - datetime.fromisoformat(first.last_attempt_at)).total_seconds(),
            7200.0,
        )

        self.server.overrides.pop("detail:1001")
        self.server.calls.clear()
        blocked = library.refresh()
        self.assertEqual(blocked.error_code, "cooldown")
        self.assertEqual(self.server.calls, [])      # 冷却期内一个请求都不发
        self.assertFalse(self.root.joinpath(what_library.POINTER_NAME).exists())

        # 冷却到点：放行并正常发布
        fixed.now = deadline
        again = library.refresh()
        self.assertEqual(again.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(sorted(self.server.detail_ids()), [1001, 1002, 1003])
        self.assertIsNone(self._state().get("next_allowed_at"))

    def test_past_http_date_allows_immediate_retry(self):
        library, _fixed = self.frozen()
        first = self._throttle(library, http_date(BASE_TIME - timedelta(hours=1)))
        self.assertEqual(first.error_code, "rate_limited")
        self.assertEqual(first.retry_after, 0.0)
        self.assertEqual(first.next_allowed_at, BASE_TIME.isoformat())
        self.server.overrides.pop("detail:1001")
        self.server.calls.clear()
        again = library.refresh()      # 过去的日期不构成冷却
        self.assertEqual(again.status, what_library.STATUS_PUBLISHED)
        self.assertIsNone(again.error_code)
        self.assertNotEqual(self.server.calls, [])


# ---------------------------------------------------------------- 发布与取消


class PublishGateTests(LibraryTestCase):
    def test_reader_keeps_old_generation_during_and_after_refresh(self):
        library = self.make()
        first = library.refresh()
        with library.open_current() as (old_conn, old_generation):
            self.assertEqual(old_generation.name, first.generation)

            seen = {}

            def during(key):
                if key == "detail:1005" and not seen:
                    with library.open_current() as (conn, generation):
                        seen["name"] = generation.name
                        seen["rows"] = int(conn.execute("SELECT COUNT(*) FROM article").fetchone()[0])
                    self.assertEqual(self.pointer()["generation"], first.generation)

            self.server.after_call = during
            self.game.add(1005, "琥珀", section="旅人图鉴", nodes=AMBAR_NODES)
            second = library.refresh()
            self.server.after_call = None

            self.assertEqual(seen["name"], first.generation)
            self.assertEqual(seen["rows"], 3)
            self.assertEqual(second.status, what_library.STATUS_PUBLISHED)
            self.assertEqual(second.previous_generation, first.generation)

            # 刷新后：旧连接仍在读旧代
            self.assertEqual(self.query_attack(old_conn).candidates[0].value, "7484")
        self.assertEqual(self.generations(), sorted([f"{first.generation}.sqlite3", f"{second.generation}.sqlite3"]))

    def test_cancel_during_fetch_does_not_advance_pointer(self):
        library = self.make()
        first = library.refresh()
        token = what_library.RefreshCancel()

        def cancel_on_first_detail(key):
            if key == "detail:1002":
                token.cancel()

        self.server.after_call = cancel_on_first_detail
        self.game.touch(1002, 1795100000)
        self.game.add(1005, "琥珀", section="旅人图鉴", nodes=AMBAR_NODES)
        report = library.refresh(cancel=token)
        self.server.after_call = None
        self.assertEqual(report.status, what_library.STATUS_CANCELLED)
        self.assertEqual(report.error_code, "cancelled")
        self.assertIsNone(report.generation)
        self.assertTrue(report.old_generation_ready)
        self.assertEqual(report.done, 1)      # 1002 已读完，1005 没再发请求
        self.assertEqual(report.total, 2)    # 1002 时间戳变了 → 它 + 1005
        self.assertEqual(self.pointer()["generation"], first.generation)
        self.assertEqual(self.generations(), [f"{first.generation}.sqlite3"])

    def test_cancel_after_build_does_not_publish_pointer(self):
        library = self.make()
        first = library.refresh()
        token = what_library.RefreshCancel()
        real_build = what_db.build_from_articles

        def build_then_cancel(*args, **kwargs):
            result = real_build(*args, **kwargs)
            token.cancel()
            return result

        with mock.patch.object(what_library.what_db, "build_from_articles", side_effect=build_then_cancel):
            self.game.add(1005, "琥珀", section="旅人图鉴", nodes=AMBAR_NODES)
            report = library.refresh(cancel=token)

        self.assertEqual(report.status, what_library.STATUS_CANCELLED)
        self.assertEqual(self.pointer()["generation"], first.generation)
        self.assertEqual(self.generations(), [f"{first.generation}.sqlite3"])
        staging = sorted((self.root / "staging").glob("*.sqlite3"))
        self.assertTrue(staging)  # 未发布的 staging 保留供复查

    def test_build_failure_keeps_old_generation(self):
        library = self.make()
        first = library.refresh()
        with mock.patch.object(
            what_library.what_db,
            "build_from_articles",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            report = library.refresh(full=True)
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "build_failed")
        self.assertEqual(self.pointer()["generation"], first.generation)
        self.assertEqual(self.generations(), [f"{first.generation}.sqlite3"])

    def test_verify_failure_keeps_old_generation(self):
        library = self.make()
        first = library.refresh()
        with mock.patch.object(
            what_library.WhatLibrary,
            "verify_staging",
            side_effect=what_library._VerifyFailed("模拟 FTS 回读失败"),
        ):
            report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "verify_failed")
        self.assertIn("FTS", report.error)
        self.assertEqual(self.pointer()["generation"], first.generation)

    def test_in_flight_refresh_is_refused(self):
        library = self.make()
        library.refresh()
        nested = {}

        def reenter(key):
            if key == "detail:1005" and not nested:
                nested["report"] = library.refresh()

        self.server.after_call = reenter
        self.game.add(1005, "琥珀", section="旅人图鉴", nodes=AMBAR_NODES)
        report = library.refresh()
        self.server.after_call = None
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        self.assertEqual(nested["report"].status, what_library.STATUS_FAILED)
        self.assertEqual(nested["report"].error_code, "refresh_in_progress")


# ---------------------------------------------------------------- 出站纪律


class OutboundTests(LibraryTestCase):
    def test_request_limits_and_serial_pacing(self):
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        tree = [call for call in self.server.calls if call["key"] == "tree"]
        pages = [call for call in self.server.calls if call["key"].startswith("page:")]
        details = self.server.detail_calls()
        self.assertEqual(tree[0]["max_bytes"], what_library.MAX_MANIFEST_BYTES)
        self.assertEqual(pages[0]["max_bytes"], what_library.MAX_MANIFEST_BYTES)
        self.assertTrue(all(call["max_bytes"] == what_library.MAX_DETAIL_BYTES for call in details))
        self.assertTrue(all(call["timeout"] == 20.0 for call in self.server.calls))
        self.assertTrue(all(call["url"].startswith("https://www.gamekee.com/") for call in self.server.calls))
        # 串行：每次请求之间一次固定间隔，且没有并发（调用记录按顺序单调）
        self.assertEqual(len(self.sleeper.calls), len(self.server.calls) - 1)
        self.assertTrue(all(sleep == 0.12 for sleep in self.sleeper.calls))
        order = [call["key"] for call in self.server.calls]
        self.assertEqual(order[0], "tree")
        self.assertEqual(order[1], "page:1")
        self.assertTrue(all(key.startswith("detail:") for key in order[2:]))

    def test_dynamic_page_count_not_hardcoded(self):
        self.game.page_size = 2  # 5 行 → 3 页（不硬编码 8 页）
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        pages = [call["key"] for call in self.server.calls if call["key"].startswith("page:")]
        self.assertEqual(pages, ["page:1", "page:2", "page:3"])

    def test_unreachable_detail_404_counts_as_failure(self):
        library = self.make()
        self.server.overrides["detail:1003"] = what_library.FetchResponse(status=404)
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_FAILED)
        self.assertEqual(report.error_code, "http_status")
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.done, 2)

    def test_only_three_documented_endpoints_used(self):
        library = self.make()
        report = library.refresh()
        self.assertEqual(report.status, what_library.STATUS_PUBLISHED)
        paths = {call["path"] for call in self.server.calls}
        self.assertEqual(
            paths,
            {
                what_library.TREE_PATH,
                what_library.LIST_PATH,
                "/v1/content/detail/1001",
                "/v1/content/detail/1002",
                "/v1/content/detail/1003",
            },
        )
        page_call = next(call for call in self.server.calls if call["key"] == "page:1")
        self.assertEqual(page_call["url"], "https://www.gamekee.com/v1/content/pageList?page_no=1&limit=100")
        self.assertEqual(what_library.API_HEADERS["game-alias"], "stellasora")

    def test_url_whitelist_rules(self):
        self.assertTrue(what_library._url_allowed("https://www.gamekee.com/v1/entry/treesByPidV1"))
        self.assertFalse(what_library._url_allowed("http://www.gamekee.com/v1/entry/treesByPidV1"))
        self.assertFalse(what_library._url_allowed("https://evil.example/v1/content/pageList"))
        self.assertFalse(what_library._url_allowed("https://www.gamekee.com:8443/v1/content/pageList"))
        self.assertFalse(what_library._url_allowed("https://cdnimg-v2.gamekee.com/a.png"))


# ---------------------------------------------------------------- 时间信息


class GenerationInfoTests(LibraryTestCase):
    def test_age_and_stale(self):
        library = self.make()
        library.refresh()
        with library.open_current() as (_conn, generation):
            self.assertAlmostEqual(generation.age_hours(now=BASE_TIME), 0, delta=0.02)
            self.assertFalse(generation.is_stale(6, now=BASE_TIME))
            self.assertTrue(generation.is_stale(0, now=BASE_TIME + timedelta(hours=7)))

    def test_cancelled_refresh_leaves_last_verified_untouched(self):
        library = self.make()
        first = library.refresh()
        self.server.calls.clear()
        token = what_library.RefreshCancel()
        token.cancel()
        report = library.refresh(cancel=token)
        self.assertEqual(report.status, what_library.STATUS_CANCELLED)
        self.assertEqual(report.last_verified, first.last_verified)
        self.assertEqual(self.pointer()["last_verified"], first.last_verified)
        self.assertEqual(self.server.detail_ids(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
