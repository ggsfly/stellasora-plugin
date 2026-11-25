# -*- coding: utf-8 -*-
"""stellasora_what 交付层测试：全部离线（临时 sqlite + mock 图片字节 + mock forward）。

覆盖：字段题纯文字与千分位、整篇证据（不拿 catalog 冒充正文）、片段证据边界、
每包末行落款与原序、两跳真实字节预算（含多短节点小预算回归）、单图超预算、
五态分流（含 unknown 不重发 / 迟到重打拒绝）、图片用途分流、内部 debug 不外发。
不发起任何真实网络请求，不启动 MaiBot，不读写宿主 config。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import what_db  # noqa: E402
import what_delivery as delivery  # noqa: E402
import what_parse  # noqa: E402
import what_query  # noqa: E402

try:  # 用于「估算 == 真实 msgpack」对拍；缺失时相关断言显式 skip，不静默放过
    import msgpack  # type: ignore
except Exception:  # pragma: no cover - 宿主 venv 有 msgpack
    msgpack = None

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CDN_A = "//cdnimg-v2.gamekee.com/a.png"
CDN_B = "//cdnimg-v2.gamekee.com/b.png"
CDN_C = "//cdnimg-v2.gamekee.com/c.gif"
CDN_D = "//cdnimg-v2.gamekee.com/d.png"
CDN_ART = "//cdnimg-v2.gamekee.com/art.png"
CDN_ICON = "//cdnimg-v2.gamekee.com/icon.png"
CDN_E = "//cdnimg-v2.gamekee.com/e.png"
CDN_F = "//cdnimg-v2.gamekee.com/f.png"

STREAM = "stream-test-1"


def png_bytes(size: int = 80) -> bytes:
    if size < 12:
        raise ValueError("size 太小")
    return PNG_MAGIC + b"\x00" * (size - 8)


# ---------------------------------------------------------------- 解析 fixture


def simple_editor(text: str) -> dict:
    return {"type": "simpleEditor", "data": [{"type": "paragraph", "children": [{"text": text}]}]}


def illustrated(block_type: str, data: dict) -> dict:
    return {"type": "illustrated-book", "data": [{"type": block_type, "data": data}]}


def profile_block(attrs, *, title="基础信息") -> dict:
    return illustrated(
        "character-profile",
        {
            "title": title,
            "name": simple_editor(""),
            "descTitle": simple_editor(""),
            "desc": simple_editor(""),
            "attrList": [
                {"title": simple_editor(label), "content": simple_editor(value)} for label, value in attrs
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


def para(text: str = "", kids=None) -> dict:
    return {"type": "paragraph", "children": ([{"text": text}] if text else []) + list(kids or [])}


def image_node(url: str) -> dict:
    return {"type": "image", "src": url, "children": [{"text": ""}]}


def header(text: str) -> dict:
    return {"type": "header1", "children": [{"text": text}]}


def detail_payload(content_id: int, nodes, *, title="测试角色") -> dict:
    return {
        "code": 0,
        "msg": "成功",
        "data": {
            "id": content_id,
            "content_id": content_id,
            "type": 4,
            "title": title,
            "summary": "",
            "entry_id": 1,
            "image_num": 0,
            "word_count": 100,
            "updated_at": 1795000000,
            "content_json": json.dumps(nodes, ensure_ascii=False),
        },
    }


# ---------------------------------------------------------------- mock 通道


class ForwardRecorder:
    """mock `ctx.send.forward`：记录调用并依次回放 outcome（True/False/异常）。"""

    def __init__(self, outcomes=None):
        self.calls = []
        self._outcomes = list(outcomes or [])

    async def __call__(self, nodes, stream_id):
        self.calls.append((nodes, stream_id))
        outcome = self._outcomes.pop(0) if self._outcomes else True
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def count(self) -> int:
        return len(self.calls)


class ImageLoader:
    """mock 取图：按 url 返回预置字节/异常，记录调用顺序。"""

    def __init__(self, payloads=None, default=PNG_MAGIC + b"\x00" * 64):
        self.calls = []
        self._payloads = dict(payloads or {})
        self._default = default

    async def __call__(self, url):
        self.calls.append(url)
        value = self._payloads.get(url, self._default)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(url)
        return value


def all_segments(nodes) -> list:
    return [seg for node in nodes for seg in node["segments"]]


def text_lines(nodes) -> list:
    return [seg["content"] for seg in all_segments(nodes) if seg.get("type") == "text"]


def image_contents(nodes) -> list:
    return [seg["content"] for seg in all_segments(nodes) if seg.get("type") == "image"]


def assert_packet_contract(test: unittest.TestCase, nodes) -> None:
    """每包结构契约：末节点末段精确落款、全包只出现一次、正文无空行尾巴。"""

    test.assertTrue(nodes, "空包不应发送")
    last = nodes[-1]["segments"][-1]
    test.assertEqual(last, {"type": "text", "content": delivery.SOURCE_LINE})
    lines = text_lines(nodes)
    test.assertEqual(lines.count(delivery.SOURCE_LINE), 1, "每包落款必须恰好一次")
    for seg in all_segments(nodes):
        if seg.get("type") == "text":
            test.assertEqual(seg["content"], seg["content"].strip(), "文字段不得有首尾空白/空行尾巴")
    for node in nodes:
        test.assertEqual(node["nickname"], delivery.FORWARD_NICKNAME)
        test.assertTrue(node["segments"], "节点不能为空")


# ---------------------------------------------------------------- 数据库 fixture


class DataFixture:
    """一次建库，供多个用例只读使用（临时目录，测试结束删除）。"""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._closed = False
        self.path = Path(self.tmp.name) / "delivery.sqlite3"

        # 图文文章：开篇装饰段 → header（关闭 preamble）→ 正文段 → 正文图 A → 图注 →
        # 含内嵌图 B 的段落 → 第二段 → gif C（用途未判）→ 正文图 D
        article = what_parse.parse_raw_dir_entry(
            detail_payload(
                2001,
                [
                    para("开篇装饰文字"),
                    header("正文"),
                    para("第一段正文"),
                    image_node(CDN_A),
                    para("图注文字"),
                    para("嵌套图段落", kids=[image_node(CDN_B)]),
                    para("第二段正文"),
                    image_node(CDN_C),
                    image_node(CDN_D),
                    para("重复正文"),
                    para("重复正文"),
                ],
                title="测试文章",
            ),
            tree_path="/攻略区/攻略区/测试文章",
        )
        # 纯图片页：无可提取文字，只有图片
        media_page = what_parse.parse_raw_dir_entry(
            detail_payload(2002, [image_node(CDN_E), image_node(CDN_F)], title="纯图片页"),
            tree_path="/游戏鉴赏/游戏鉴赏/纯图片页",
        )
        traveler = what_parse.parse_raw_dir_entry(
            detail_payload(
                2004,
                [
                    profile_block([("生日", "3月5日"), ("所属", "地理协会")]),
                    fight_info([(1, 100, 700), ("90", 7484, 51179)]),
                ],
                title="优叶",
            ),
            tree_path="/旅人图鉴/旅人图鉴/优叶",
        )
        what_db.build_from_articles(
            self.path, [article, media_page, traveler], meta={"test": "delivery"}
        )
        self.conn = what_db.open_db(str(self.path))
        entity_id = self.conn.execute(
            "SELECT entity_id FROM entity WHERE content_id=2001"
        ).fetchone()["entity_id"]
        # 注入已判定用途的立绘与图标行：只用于验证「用途分流」，不是真实语料
        injected = [
            ("/9/src", 0, CDN_ART, "character_art", "测试注入：角色立绘位", "image", "立绘"),
            ("/10/src", 0, CDN_ICON, "inline_icon", "测试注入：正文内嵌图标", "image", None),
        ]
        for path, ordinal, url, role, evidence, kind, group_label in injected:
            self.conn.execute(
                "INSERT OR REPLACE INTO media_ref(content_id,source_path,ordinal,entity_id,kind,"
                "group_label,locale,label,url,role,role_evidence,role_status,node_path,side,"
                "equivalent_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (2001, path, ordinal, entity_id, kind, group_label, "zh", role, url, role,
                 evidence, "judged", path, "", ""),
            )
        self.conn.commit()
        # Windows 上必须先 close(sqlite3) 再 cleanup(临时目录)：连接还开着就删库文件会
        # 抛 WinError 32（进程无法访问另一个程序正在使用的文件）。atexit 早于
        # TemporaryDirectory 的 weakref 终扫执行，因此这里注册能保证顺序。
        atexit.register(self.close)

    def close(self) -> None:
        """幂等收尾：先关连接，再删临时目录（Windows 文件占用兜底）。"""

        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        try:
            self.tmp.cleanup()
        except PermissionError:  # 极端情况下仍有句柄未释放，留给解释器收尾，不刷错误
            pass


FIXTURE: DataFixture | None = None


def get_fixture() -> DataFixture:
    global FIXTURE
    if FIXTURE is None:
        FIXTURE = DataFixture()
    return FIXTURE


# ---------------------------------------------------------------- 体积计量


class MetricTests(unittest.TestCase):
    """msgpack 字节：与真实 packb 对拍，两跳取较大者，版本预算白名单。"""

    def test_estimator_matches_real_msgpack(self):
        if msgpack is None:
            self.skipTest("宿主 venv 无 msgpack，无法对拍")
        cases = [
            {"a": 1, "b": [1, 2, 3], "c": "中文/abc", "d": None, "e": True, "f": 2**40, "g": -1000},
            {"messages": [{"nickname": "星塔旅人", "segments": [{"type": "text", "content": "内容"}]}]},
            {"messages": [], "stream_id": STREAM, "return_details": False},
            {"big": "x" * 70000, "cjk": "中" * 40},
            {"nested": {"segments": [{"type": "image", "content": "A" * 900, "filename": "a.png"}]}},
        ]
        for obj in cases:
            self.assertEqual(
                delivery.msgpack_bytes(obj),
                len(msgpack.packb(obj, use_bin_type=True)),
                f"估算与真实 msgpack 不一致：{str(obj)[:40]}",
            )

    def test_packet_bytes_is_two_hop_max(self):
        nodes = [{"nickname": "星塔旅人", "segments": [{"type": "text", "content": "内容"}]} for _ in range(100)]
        hop1 = delivery.frame_bytes(nodes, STREAM)
        hop2 = delivery.gateway_frame_bytes(nodes, STREAM)
        self.assertEqual(delivery.packet_bytes(nodes, STREAM), max(hop1, hop2))
        self.assertGreater(hop2, hop1, "第二跳必须计入节点/段膨胀")

    def test_second_hop_expansion_scales_with_nodes(self):
        def nodes(n):
            return [{"nickname": "星塔旅人", "segments": [{"type": "text", "content": "x"}]} for _ in range(n)]

        small = delivery.gateway_frame_bytes(nodes(100), STREAM) - delivery.frame_bytes(nodes(100), STREAM)
        big = delivery.gateway_frame_bytes(nodes(1000), STREAM) - delivery.frame_bytes(nodes(1000), STREAM)
        per_node_small = small / 100
        per_node_big = big / 1000
        # 实测国内部模型约 +54 B/节点（Leader 独立测 100 节点 +5,390 B）；这里要求
        # 至少 40 B/节点，且不随节点数塌掉（恒定余量那个错口径会在这里失败）
        self.assertGreater(per_node_small, 40)
        self.assertGreater(per_node_big, 40)
        self.assertGreater(big, 40_000)

    def test_image_segment_counts_b64_in_both_hops(self):
        b64 = "A" * 4000
        nodes = [{"nickname": "星塔旅人", "segments": [{"type": "image", "content": b64, "filename": "a.png"}]}]
        hop1 = delivery.frame_bytes(nodes, STREAM)
        hop2 = delivery.gateway_frame_bytes(nodes, STREAM)
        self.assertGreater(hop1, len(b64))
        self.assertGreater(hop2, len(b64))
        self.assertGreater(hop2 - hop1, 60, "图片第二跳要计入 64 字符 hash")

    def test_frame_limit_by_verified_host_versions(self):
        limit, conservative, note = delivery.resolve_frame_limit("1.4.8")
        self.assertEqual(limit, 32 * 1024 * 1024)
        self.assertFalse(conservative)
        self.assertIn("1.4.8", note)
        for version in ("1.3.15",):
            self.assertEqual(delivery.resolve_frame_limit(version)[0], 16 * 1024 * 1024)
        for version in ("1.4.6", "1.4.7"):
            self.assertEqual(delivery.resolve_frame_limit(version)[0], 32 * 1024 * 1024)
        limit, conservative, note = delivery.resolve_frame_limit("9.9.9")
        self.assertEqual(limit, 16 * 1024 * 1024)
        self.assertTrue(conservative)
        self.assertIn("未核对", note)


# ---------------------------------------------------------------- 字段题


class FieldDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """字段题：纯文字、千分位、无图片、来源落款。"""

    @classmethod
    def setUpClass(cls):
        cls.fx = get_fixture()

    async def test_field_answer_is_text_only(self):
        result = what_query.run_query(
            self.fx.conn,
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["stat.attack"],
                "levels": [90],
            },
        )
        self.assertEqual(result.status, "ok")
        plan = delivery.prepare_delivery(self.fx.conn, result, {"mode": "field", "shape": "single"})
        self.assertEqual(plan.kind, "ok")
        self.assertEqual(plan.groups[0].label, "【优叶】")
        self.assertEqual(plan.groups[0].items[0].text, "攻击（90级）：7,484")
        self.assertEqual(plan.image_count(), 0)

        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=delivery.FRAME_LIMIT_DEFAULT
        )
        self.assertEqual(report.status, "sent")
        self.assertTrue(report.success)
        self.assertTrue(report.stop_after_execution)
        self.assertEqual(report.packs_sent, 1)
        self.assertEqual(report.images_sent, 0)
        self.assertEqual(forward.count, 1)
        lines = text_lines(forward.calls[0][0])
        self.assertEqual(lines, ["【优叶】", "攻击（90级）：7,484", delivery.SOURCE_LINE])
        self.assertNotIn("7484", "".join(lines), "必须补千分位，不能露原样 7484")
        self.assertNotIn("stat.attack", "".join(lines), "内部字段键不进正文")

    async def test_thousands_formatting_rules(self):
        self.assertEqual(delivery.format_thousands("7484"), "7,484")
        self.assertEqual(delivery.format_thousands("51179"), "51,179")
        self.assertEqual(delivery.format_thousands("-1234"), "-1,234")
        self.assertEqual(delivery.format_thousands("100"), "100")
        self.assertEqual(delivery.format_thousands("3月5日"), "3月5日")
        self.assertEqual(delivery.format_thousands("6%"), "6%")
        self.assertEqual(delivery.format_thousands("1.234567"), "1.234567")
        self.assertEqual(delivery.format_thousands("12345.67"), "12,345.67")

    async def test_field_notes_debug_never_leaks(self):
        """field_notes 里的 blocks JSON（status=info）是内部取证，绝不能进 QQ 正文。"""

        result = SimpleNamespace(
            status="ok",
            mode="field",
            message="；该板块未标注年龄字段（其余字段已给出）",
            candidates=[
                what_query.Candidate(
                    kind="fact",
                    content_id=2004,
                    title="优叶",
                    entity_id=1,
                    entity_name="优叶",
                    entity_kind="traveler",
                    field_key="salary",
                    qualifier="",
                    ordinal=1,
                    value="12000",
                    source_path="/2/children/3/props",
                )
            ],
            field_notes=[
                {"field_key": "blocks", "status": "info", "note": json.dumps([{"block_type": "header1"}])},
                {"field_key": "age", "status": "no_source", "note": "该板块未标注年龄字段；生日不能当年龄用。"},
            ],
            rejected=[{"reason": "body_substring_mismatch", "source_path": "/3/children/0"}],
            stats={"rejected": 1},
            also_named=[],
            available_levels=[],
            available_fields=[],
            available_groups=[],
            media_policy={},
            block_evidence=[],
        )
        plan = delivery.prepare_delivery(self.fx.conn, result, {"mode": "field", "shape": "single"})
        texts = "".join(
            item.text for group in plan.groups for item in group.items if isinstance(item, delivery.TextItem)
        )
        self.assertIn("薪资：12,000", texts)
        self.assertIn("该板块未标注年龄字段", texts)
        self.assertNotIn("block_type", texts)
        self.assertNotIn("header1", texts)
        self.assertNotIn("/3/children/0", texts)


# ---------------------------------------------------------------- 整篇


class WholeArticleTests(unittest.IsolatedAsyncioTestCase):
    """整篇：按核实 content_id 读正文与原序配图；目录/标题伪候选不得冒充。"""

    @classmethod
    def setUpClass(cls):
        cls.fx = get_fixture()

    def _catalog_result(self, content_id=2001, title="测试文章") -> SimpleNamespace:
        return SimpleNamespace(
            status="ok",
            mode="article",
            message="",
            subject_name=title,
            candidates=[
                what_query.Candidate(
                    kind="chunk",
                    content_id=content_id,
                    title=title,
                    entity_id=None,
                    entity_name="测试文章",
                    entity_kind="article",
                    field_key="article.catalog",
                    value=(
                        "kind=article category=攻略区 schema=prosemirror word_count=100 "
                        "media=5 chunks=8 availability=ok"
                    ),
                    source_path="/",
                    heading="(条目概览，不是正文)",
                )
            ],
            also_named=[],
            available_levels=[],
            available_fields=[],
            available_groups=[],
            field_notes=[],
            block_evidence=[],
            rejected=[],
            media_policy={},
            stats={},
        )

    def test_whole_article_reads_body_in_pointer_order(self):
        result = self._catalog_result()
        plan = delivery.prepare_delivery(
            self.fx.conn, result, {"mode": "article", "shape": "whole_article"}
        )
        self.assertEqual(plan.kind, "ok")
        items = plan.groups[0].items
        kinds = ["text" if isinstance(i, delivery.TextItem) else "img" for i in items]
        texts = [i.text for i in items if isinstance(i, delivery.TextItem)]
        urls = [i.url for i in items if isinstance(i, delivery.ImageItem)]
        # 双版面/重复正文只留一次
        self.assertEqual(texts.count("重复正文"), 1)
        self.assertIn("第一段正文", texts)
        # 目录概览串绝不出现
        self.assertNotIn("kind=article", "".join(texts))
        self.assertNotIn("chunks=8", "".join(texts))
        # 原序：正文段 A → 图 A → 图注 → 嵌套段 → 图 B → 第二段 → 图 D
        self.assertIn(CDN_A, urls)
        self.assertIn(CDN_B, urls)
        self.assertIn(CDN_D, urls)
        self.assertNotIn(CDN_C, urls, "用途未判（gif/unknown）不得进入整篇")
        self.assertNotIn(CDN_ART, urls, "立绘不属于整篇必要图")
        self.assertNotIn(CDN_ICON, urls, "内嵌图标不得当正文图")
        self.assertEqual(plan.groups[0].label, "【测试文章】")
        # 指针原序：A 在图注之前、B 在第二段之前
        idx_first = kinds.index("text")
        self.assertEqual(texts[1], "正文")  # /1 header 紧跟 /0
        order = [
            ("text", "第一段正文"),
            ("img", CDN_A),
            ("text", "图注文字"),
            ("text", "嵌套图段落"),
            ("img", CDN_B),
            ("text", "第二段正文"),
            ("img", CDN_D),
        ]
        position = 0
        for kind_wanted, value in order:
            while position < len(items):
                item = items[position]
                got_kind = "text" if isinstance(item, delivery.TextItem) else "img"
                got_value = item.text if isinstance(item, delivery.TextItem) else item.url
                position += 1
                if got_kind == kind_wanted and got_value == value:
                    break
            else:
                self.fail(f"整篇顺序缺少 {kind_wanted}:{value}")
        self.assertGreater(idx_first, -1)

    async def test_whole_article_delivery_sends_all_images(self):
        result = self._catalog_result()
        plan = delivery.prepare_delivery(
            self.fx.conn, result, {"mode": "article", "shape": "whole_article"}
        )
        loader = ImageLoader()
        forward = ForwardRecorder()
        report = await deliver_plan(plan, forward, loader)
        self.assertEqual(report.status, "sent")
        self.assertEqual(report.images_planned, 3)
        self.assertEqual(report.images_sent, 3)
        self.assertEqual(loader.calls, [CDN_A, CDN_B, CDN_D], "取图必须按原序逐张")
        self.assertEqual(report.packs_sent, 1)
        nodes = forward.calls[0][0]
        assert_packet_contract(self, nodes)
        self.assertEqual(len(image_contents(nodes)), 3)
        bodies = text_lines(nodes)
        self.assertIn("第一段正文", bodies)
        self.assertEqual(bodies[0], "【测试文章】")
        self.assertEqual(bodies[-1], delivery.SOURCE_LINE)

    def test_media_only_page_is_not_pretended_as_text(self):
        result = self._catalog_result(content_id=2002, title="纯图片页")
        plan = delivery.prepare_delivery(
            self.fx.conn, result, {"mode": "article", "shape": "whole_article"}
        )
        self.assertEqual(plan.kind, "ok")
        group = plan.groups[0]
        self.assertIn("原页无可提取文字", group.note)
        self.assertEqual([i.url for i in group.items if isinstance(i, delivery.ImageItem)], [CDN_E, CDN_F])
        self.assertFalse([i for i in group.items if isinstance(i, delivery.TextItem)])


async def deliver_plan(plan, forward, loader=None, *, budget=None, host_version=None):
    """测试用一行包装：显式预算优先，否则按宿主版本/默认值解析。"""

    return await delivery.deliver_what(
        stream_id=STREAM,
        send_forward=forward,
        plan=plan,
        load_image=loader,
        budget_bytes=(
            budget
            if budget is not None
            else (None if host_version is not None else delivery.FRAME_LIMIT_DEFAULT)
        ),
        host_version=host_version,
    )


# ---------------------------------------------------------------- 片段


class SnippetDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """片段：只发原文；配图仅在与片段指针同子树的证据图。"""

    @classmethod
    def setUpClass(cls):
        cls.fx = get_fixture()

    def _result(self, source_path: str, body: str) -> SimpleNamespace:
        return SimpleNamespace(
            status="ok",
            mode="article",
            message="",
            subject_name="测试文章",
            candidates=[
                what_query.Candidate(
                    kind="chunk",
                    content_id=2001,
                    title="测试文章",
                    entity_id=None,
                    entity_name="测试文章",
                    entity_kind="article",
                    value=body,
                    source_path=source_path,
                    heading="正文",
                )
            ],
            also_named=[],
            available_levels=[],
            available_fields=[],
            available_groups=[],
            field_notes=[],
            block_evidence=[],
            rejected=[],
            media_policy={},
            stats={},
        )

    def test_snippet_with_nested_image_carries_that_image_only(self):
        plan = delivery.prepare_delivery(
            self.fx.conn,
            self._result("/5", "嵌套图段落"),
            {"mode": "article", "shape": "single"},
        )
        urls = [i.url for i in plan.groups[0].items if isinstance(i, delivery.ImageItem)]
        self.assertEqual(urls, [CDN_B], "只带同子树证据图")

    def test_sibling_image_is_not_claimed_as_body(self):
        plan = delivery.prepare_delivery(
            self.fx.conn,
            self._result("/2", "第一段正文"),
            {"mode": "article", "shape": "single"},
        )
        urls = [i.url for i in plan.groups[0].items if isinstance(i, delivery.ImageItem)]
        self.assertEqual(urls, [], "兄弟节点的图不是该片段证据，不猜")

    async def test_title_candidate_never_pretends_to_be_body(self):
        result = SimpleNamespace(
            status="ok",
            mode="article",
            message="正文没有命中，命中 1 个标题；标题命中不等于正文回答",
            subject_name="测试文章",
            candidates=[
                what_query.Candidate(
                    kind="chunk",
                    content_id=2001,
                    title="测试文章",
                    entity_id=None,
                    entity_name="测试文章",
                    entity_kind="article",
                    field_key="article.title",
                    value="测试文章",
                    source_path="/",
                    heading="(标题命中，不是正文)",
                )
            ],
            also_named=[],
            available_levels=[],
            available_fields=[],
            available_groups=[],
            field_notes=[],
            block_evidence=[],
            rejected=[],
            media_policy={},
            stats={},
        )
        plan = delivery.prepare_delivery(
            self.fx.conn, result, {"mode": "article", "shape": "single"}
        )
        self.assertEqual(plan.kind, "nothing")
        self.assertEqual(plan.groups, [])
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan
        )
        self.assertEqual(report.status, "nothing")
        self.assertTrue(report.success)


# ---------------------------------------------------------------- 媒体用途


class MediaPolicyTests(unittest.IsolatedAsyncioTestCase):
    """图片用途分流：media 意图发已判定立绘；整篇/片段只发正文图。"""

    @classmethod
    def setUpClass(cls):
        cls.fx = get_fixture()

    def test_query_media_keeps_judged_roles_only(self):
        result = what_query.run_query(
            self.fx.conn, {"mode": "media", "subject": {"name": "测试文章"}}
        )
        self.assertEqual(result.status, "ok")
        roles = sorted({c.role for c in result.candidates})
        self.assertEqual(roles, ["body_image", "character_art"])
        rejected_roles = sorted({r["role"] for r in result.rejected})
        self.assertIn("inline_icon", rejected_roles)
        self.assertIn("unknown", rejected_roles)

    async def test_media_intent_sends_judged_art_and_body(self):
        result = what_query.run_query(
            self.fx.conn, {"mode": "media", "subject": {"name": "测试文章"}}
        )
        plan = delivery.prepare_delivery(
            self.fx.conn, result, {"mode": "media", "shape": "single"}
        )
        self.assertEqual(plan.kind, "ok")
        self.assertEqual(plan.image_count(), 4)
        labels = {group.label for group in plan.groups}
        self.assertIn("【测试文章】正文图", labels)
        self.assertIn("【测试文章】立绘", labels)
        loader = ImageLoader()
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=loader
        )
        self.assertEqual(report.status, "sent")
        self.assertEqual(report.images_sent, 4)
        self.assertIn(CDN_ART, loader.calls)
        self.assertNotIn(CDN_ICON, loader.calls)
        self.assertNotIn(CDN_C, loader.calls)

    async def test_media_invalid_intent_when_group_missing(self):
        result = what_query.run_query(
            self.fx.conn, {"mode": "media", "subject": {"name": "测试文章"}, "groups": ["不存在的组"]}
        )
        self.assertEqual(result.status, "invalid_intent")
        plan = delivery.prepare_delivery(
            self.fx.conn, result, {"mode": "media", "shape": "single", "groups": ["不存在的组"]}
        )
        self.assertNotEqual(plan.kind, "ok")
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan
        )
        self.assertEqual(report.status, "failed")
        self.assertFalse(report.success)


# ---------------------------------------------------------------- 分包与预算


def make_plan(group_count: int, text: str = "一句话") -> delivery.DeliveryPlan:
    groups = [
        delivery.DeliveryGroup(label=f"【组{i}】", items=[delivery.TextItem(text=text)])
        for i in range(group_count)
    ]
    return delivery.DeliveryPlan(kind="ok", message="", groups=groups)


def expected_nodes(group_count: int, text: str = "一句话") -> list:
    nodes = []
    for i in range(group_count):
        nodes.append(
            {
                "nickname": delivery.FORWARD_NICKNAME,
                "segments": [
                    {"type": "text", "content": f"【组{i}】"},
                    {"type": "text", "content": text},
                ],
            }
        )
    nodes[-1]["segments"].append({"type": "text", "content": delivery.SOURCE_LINE})
    return nodes


class PackingTests(unittest.IsolatedAsyncioTestCase):
    """分包：真实字节、原序、每包落款、第二跳膨胀驱动的小预算回归。"""

    async def test_small_budget_splits_but_never_truncates(self):
        budget = 8_000
        plan = make_plan(60)
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=budget
        )
        self.assertEqual(report.status, "sent")
        self.assertGreaterEqual(report.packs_sent, 2, "60 个短节点在 8k 预算下必须拆包")
        self.assertEqual(report.skipped_reasons, [])
        self.assertEqual(report.packs_sent, forward.count)
        sent_texts = []
        for nodes, _stream in forward.calls:
            assert_packet_contract(self, nodes)
            self.assertLessEqual(delivery.packet_bytes(nodes, STREAM), budget)
            sent_texts.extend(t for t in text_lines(nodes) if t != delivery.SOURCE_LINE)
        self.assertEqual(len(sent_texts), 120, "拆包不能丢内容")
        for index in range(60):
            self.assertIn(f"【组{index}】", sent_texts)
        # 原序：组号必须递增出现
        seen = [int(t[2:-1]) for t in sent_texts if t.startswith("【组")]
        self.assertEqual(seen, list(range(60)))
        self.assertEqual(report.frames, [delivery.packet_bytes(n, STREAM) for n, _ in forward.calls])

    async def test_second_hop_expansion_drives_the_split(self):
        """若只按第一跳计量，这一批本可整包发；第二跳膨胀后才必须拆。"""

        group_count = 120
        one_packet = expected_nodes(group_count)
        hop1 = delivery.frame_bytes(one_packet, STREAM)
        hop2 = delivery.gateway_frame_bytes(one_packet, STREAM)
        self.assertGreater(hop2, hop1)
        budget = (hop1 + hop2) // 2
        self.assertLessEqual(hop1, budget, "构造前提：第一跳本来放得下")
        self.assertGreater(hop2, budget, "构造前提：第二跳放不下")

        plan = make_plan(group_count)
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=budget
        )
        self.assertEqual(report.status, "sent")
        self.assertGreaterEqual(report.packs_sent, 2)
        self.assertTrue(all(len(nodes) < group_count for nodes, _ in forward.calls))
        for nodes, _stream in forward.calls:
            self.assertLessEqual(delivery.gateway_frame_bytes(nodes, STREAM), budget)
            assert_packet_contract(self, nodes)

    async def test_long_text_is_split_into_segments_without_loss(self):
        long_line = "长" * 3000
        text = f"{long_line}\n第二行\n第三行"
        pieces = delivery._text_pieces(text)
        self.assertGreater(len(pieces), 2)
        self.assertTrue(all(len(piece) <= delivery.MAX_TEXT_SEGMENT_CHARS for piece in pieces))
        self.assertEqual("".join(pieces), text, "切段不得丢字改字、不得在切口加换行")
        self.assertTrue(all(piece for piece in pieces), "不允许出现空片段")
        self.assertIn("\n第二行\n第三行", "".join(pieces), "换行必须逐字保留")

    async def test_text_pieces_keep_inner_whitespace_verbatim(self):
        """首尾清理只对原整段生效：段内空格/空行/行尾空白一律原样带走。"""

        text = "第一行  \n\n  缩进行\n结尾  "
        pieces = delivery._text_pieces(text)
        self.assertEqual(pieces, [text.strip()])
        self.assertEqual(delivery._text_segment("行尾  \n")["content"], "行尾  \n",
                         "文字段不得二次 strip，否则行尾换行会被吃掉")

    async def test_node_segment_cap_keeps_order(self):
        group = delivery.DeliveryGroup(
            label="【组0】",
            items=[delivery.TextItem(text=f"第{i}行") for i in range(80)],
        )
        plan = delivery.DeliveryPlan(kind="ok", message="", groups=[group])
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=20_000
        )
        self.assertEqual(report.status, "sent")
        for nodes, _ in forward.calls:
            for node in nodes:
                self.assertLessEqual(len(node["segments"]), delivery.MAX_SEGMENTS_PER_NODE)
            assert_packet_contract(self, nodes)
        lines = [t for nodes, _ in forward.calls for t in text_lines(nodes) if t != delivery.SOURCE_LINE]
        self.assertEqual(lines[0], "【组0】")
        self.assertEqual(lines[1:], [f"第{i}行" for i in range(80)])


# ---------------------------------------------------------------- 图片异常


class ImageFailureTests(unittest.IsolatedAsyncioTestCase):
    """单图失败/超预算：明确未发、状态如实、绝不发不合规包。"""

    def _media_plan(self, urls) -> delivery.DeliveryPlan:
        group = delivery.DeliveryGroup(
            label="【测试文章】正文图",
            content_id=2001,
            note="已判定用途的媒体",
            items=[
                delivery.ImageItem(
                    content_id=2001,
                    source_path=f"/{i}/src",
                    ordinal=0,
                    url=url,
                    media_kind="image",
                    role="body_image",
                    filename=f"p{i}.png",
                )
                for i, url in enumerate(urls)
            ],
        )
        return delivery.DeliveryPlan(kind="ok", message="", groups=[group])

    async def test_single_oversize_image_is_reported_not_sent(self):
        huge = png_bytes(20_000)
        plan = self._media_plan([CDN_A, CDN_B])
        loader = ImageLoader({CDN_A: huge})
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=loader, budget_bytes=20_000
        )
        self.assertEqual(report.status, "partial")
        self.assertTrue(report.success)
        self.assertFalse(report.stop_after_execution)
        self.assertEqual(report.images_failed, 1)
        self.assertEqual(report.images_sent, 1)
        self.assertEqual(report.packs_sent, 1, "超预算那张放不进任何包；能发的那张与标题合成一包")
        self.assertEqual(forward.count, 1)
        self.assertEqual(len(image_contents(forward.calls[0][0])), 1, "必须是真图，不是空包")
        lines = text_lines(forward.calls[0][0])
        self.assertEqual(lines, ["【测试文章】正文图：已判定用途的媒体", delivery.SOURCE_LINE])
        for nodes, _ in forward.calls:
            self.assertLessEqual(delivery.packet_bytes(nodes, STREAM), 20_000)
            assert_packet_contract(self, nodes)

    async def test_html_intercept_page_is_rejected_by_magic(self):
        plan = self._media_plan([CDN_A])
        loader = ImageLoader({CDN_A: b"<html>403 forbidden</html>"})
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=loader
        )
        # 整组唯一那张被拦下：没有任何真实内容，连标题都不发，也不报 partial 成功
        self.assertEqual(report.status, "nothing")
        self.assertFalse(report.success)
        self.assertEqual(report.packs_sent, 0)
        self.assertEqual(forward.count, 0)
        self.assertEqual(report.images_failed, 1)
        self.assertEqual(report.images_sent, 0)
        self.assertIn("响应非图片", " ".join(report.skipped_reasons))

    async def test_image_fetch_error_is_partial_not_fake_success(self):
        plan = self._media_plan([CDN_A, CDN_B])
        loader = ImageLoader({CDN_A: RuntimeError("网络超时（模拟）")})
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=loader
        )
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.images_sent, 1)
        self.assertEqual(report.images_failed, 1)
        self.assertIn("取图失败", report.skipped_reasons[0])
        # 证据：真正发出去的是可数的一包，里面有标题、那张真图和固定落款
        self.assertEqual(report.packs_sent, 1)
        self.assertEqual(forward.count, 1)
        self.assertEqual(len(image_contents(forward.calls[0][0])), 1)
        self.assertEqual(
            text_lines(forward.calls[0][0]),
            ["【测试文章】正文图：已判定用途的媒体", delivery.SOURCE_LINE],
        )

    async def test_all_images_fail_reports_nothing_sent(self):
        plan = self._media_plan([CDN_A, CDN_B])
        loader = ImageLoader({
            CDN_A: RuntimeError("全挂（模拟）"),
            CDN_B: RuntimeError("全挂（模拟）"),
        })
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=loader
        )
        # 只剩标题和落款的一包不算把资料送达：全部媒体失败时必须明确未发
        self.assertEqual(report.status, "nothing")
        self.assertFalse(report.success)
        self.assertFalse(report.partial_delivery)
        self.assertEqual(report.packs_sent, 0)
        self.assertEqual(forward.count, 0, "不允许发只有标题+落款的空壳包")
        self.assertEqual(report.images_planned, 2)
        self.assertEqual(report.images_sent, 0)
        self.assertEqual(report.images_failed, 2)
        self.assertEqual(report.unsent_groups, 1)
        self.assertEqual(report.skipped_reasons[0].count("取图失败"), 1)
        self.assertIn("取图失败", report.skipped_reasons[0])
        self.assertIn("整组没有任何真实内容", report.skipped_reasons[-1])
        self.assertIn("未发", report.user_text)
        self.assertIn("没有任何一组真实内容成功发出", report.error)

    async def test_mixed_groups_still_send_real_ones(self):
        """相邻真实组照发：有真实文字的组不受其它组全挂影响。"""

        ok_group = delivery.DeliveryGroup(
            label="【测试文章】正文", items=[delivery.TextItem(text="第一段正文")]
        )
        bad_group = self._media_plan([CDN_A]).groups[0]
        plan = delivery.DeliveryPlan(kind="ok", message="", groups=[bad_group, ok_group])
        loader = ImageLoader({CDN_A: RuntimeError("全挂（模拟）")})
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=loader
        )
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.packs_sent, 1)
        self.assertEqual(forward.count, 1)
        self.assertEqual(
            text_lines(forward.calls[0][0]),
            ["【测试文章】正文", "第一段正文", delivery.SOURCE_LINE],
        )
        self.assertEqual(len(image_contents(forward.calls[0][0])), 0)

    async def test_missing_loader_is_explicit_failure(self):
        plan = self._media_plan([CDN_A])
        forward = ForwardRecorder()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=None
        )
        # 没有取图函数 = 这张图明确未发；该组没有别的真实内容，所以整组不发
        self.assertEqual(report.status, "nothing")
        self.assertFalse(report.success)
        self.assertEqual(report.packs_sent, 0)
        self.assertEqual(forward.count, 0)
        self.assertEqual(report.images_failed, 1)
        self.assertIn("未注入取图函数", report.skipped_reasons[0])


# ---------------------------------------------------------------- 五态


class StateMatrixTests(unittest.IsolatedAsyncioTestCase):
    """sent / nothing / partial / unknown / failed 五态分流与不重发纪律。"""

    async def test_forward_false_is_unknown_and_not_resent(self):
        plan = make_plan(2)
        forward = ForwardRecorder([False])
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=20_000
        )
        self.assertEqual(report.status, "unknown")
        self.assertFalse(report.success)
        self.assertFalse(report.stop_after_execution)
        self.assertEqual(report.packs_sent, 0)
        self.assertEqual(forward.count, 1, "回执未知后绝不再发")
        self.assertEqual(report.user_text, "送达不确定")
        self.assertGreaterEqual(report.unsent_groups, 1)

    async def test_forward_exception_is_unknown_and_stops(self):
        plan = make_plan(3)
        forward = ForwardRecorder([RuntimeError("连接断开（模拟）")])
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=20_000
        )
        self.assertEqual(report.status, "unknown")
        self.assertEqual(forward.count, 1)
        self.assertEqual(report.packs_sent, 0)
        self.assertIn("不重发", report.error)

    async def test_forward_timeout_is_unknown(self):
        plan = make_plan(2)
        forward = ForwardRecorder([asyncio.TimeoutError("超时（模拟）")])
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=20_000
        )
        self.assertEqual(report.status, "unknown")
        self.assertEqual(forward.count, 1)
        self.assertEqual(report.user_text, "送达不确定")

    async def test_second_packet_false_reports_partial_delivery_unknown(self):
        plan = make_plan(60)
        forward = ForwardRecorder([True, False])
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=8_000
        )
        self.assertEqual(report.status, "unknown")
        self.assertEqual(report.packs_sent, 1)
        self.assertTrue(report.partial_delivery)
        self.assertEqual(forward.count, 2, "第一包成功、第二包未知后停止，不再补发")

    async def test_same_plan_is_delivered_once(self):
        plan = make_plan(1)
        forward = ForwardRecorder()
        first = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=20_000
        )
        self.assertEqual(first.status, "sent")
        second = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, budget_bytes=20_000
        )
        self.assertEqual(second.status, "unknown")
        self.assertFalse(second.success)
        self.assertIn("未重复发送", second.error)
        self.assertEqual(forward.count, 1, "迟到重打必须被拒绝")

    async def test_not_found_is_nothing_with_phrase(self):
        result = SimpleNamespace(
            status="not_found",
            mode="field",
            message="资料库未收录「不存在的人」",
            also_named=[
                {"canonical_name": "魔王(女)", "kind": "monster", "content_id": 1},
                {"canonical_name": "魔王(男)", "kind": "npc", "content_id": 2},
            ],
            candidates=[],
        )
        plan = delivery.prepare_delivery(None, result, {"mode": "field", "shape": "single"})
        self.assertEqual(plan.kind, "not_found")
        forward = ForwardRecorder()
        report = await delivery.deliver_what(stream_id=STREAM, send_forward=forward, plan=plan)
        self.assertEqual(report.status, "nothing")
        self.assertTrue(report.success)
        self.assertTrue(report.stop_after_execution)
        self.assertEqual(forward.count, 0, "没查到不发任何包")
        self.assertTrue(report.user_text.startswith("没查到"))
        self.assertIn("魔王(女)", report.user_text)
        self.assertNotIn("content_id", report.user_text)
        self.assertNotIn("/", report.user_text)

    async def test_clarify_message_is_sanitized(self):
        result = what_query.run_query(
            get_fixture().conn,
            {"mode": "field", "subject": {"name": "优叶", "kind": "traveler"}, "fields": ["stat.attack"]},
        )
        self.assertEqual(result.status, "clarify")
        plan = delivery.prepare_delivery(
            get_fixture().conn, result, {"mode": "field", "shape": "single"}
        )
        report = await delivery.deliver_what(stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan)
        self.assertEqual(report.status, "nothing")
        self.assertTrue(report.success)
        self.assertIn("等级", report.user_text)
        self.assertIn("1, 90", report.user_text, "可用等级是给用户看的信息，不能被当 debug 剔掉")
        self.assertNotIn("stat.", report.user_text)

    async def test_invalid_intent_is_failed(self):
        result = SimpleNamespace(status="ok", mode="bogus", message="", candidates=[])
        plan = delivery.prepare_delivery(get_fixture().conn, result, {"mode": "bogus"})
        self.assertEqual(plan.kind, "invalid_intent")
        report = await delivery.deliver_what(stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan)
        self.assertEqual(report.status, "failed")
        self.assertFalse(report.success)
        self.assertFalse(report.stop_after_execution)
        self.assertEqual(report.user_text, "读取失败")

    async def test_empty_candidates_with_ok_status_is_failed(self):
        result = SimpleNamespace(status="ok", mode="field", message="", candidates=[])
        plan = delivery.prepare_delivery(get_fixture().conn, result, {"mode": "field"})
        self.assertEqual(plan.kind, "invalid_intent")
        report = await delivery.deliver_what(stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan)
        self.assertEqual(report.status, "failed")

    async def test_budget_smaller_than_allowance_is_failed(self):
        plan = make_plan(1)
        report = await delivery.deliver_what(
            stream_id=STREAM,
            send_forward=ForwardRecorder(),
            plan=plan,
            budget_bytes=delivery.SECOND_HOP_FIXED_ALLOWANCE,
        )
        self.assertEqual(report.status, "failed")
        self.assertIn("预算", report.error)

    async def test_budget_from_host_version_and_conservative_flag(self):
        plan = make_plan(1)
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan, host_version="1.4.8"
        )
        self.assertEqual(report.status, "sent")
        self.assertEqual(report.frame_limit, 32 * 1024 * 1024)
        self.assertFalse(report.conservative)
        plan2 = make_plan(1)
        report2 = await delivery.deliver_what(
            stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan2, host_version="0.0.1"
        )
        self.assertTrue(report2.conservative)
        self.assertEqual(report2.frame_limit, 16 * 1024 * 1024)

    async def test_whole_article_uses_prepared_plan(self):
        intent = {
            "mode": "article",
            "subject": {"name": "测试文章"},
            "shape": "whole_article",
        }

        def prepare():
            conn = what_db.open_db(str(get_fixture().path), readonly=True)
            try:
                result = what_query.run_query(conn, intent)
                return delivery.prepare_delivery(conn, result, intent)
            finally:
                conn.close()

        get_fixture()  # 初始化共享 fixture；工作线程只打开自己的只读连接。
        plan = await asyncio.to_thread(prepare)
        forward = ForwardRecorder()
        loader = ImageLoader()
        report = await delivery.deliver_what(
            stream_id=STREAM, send_forward=forward, plan=plan, load_image=loader
        )
        self.assertEqual(report.status, "sent")
        self.assertEqual(forward.count, 1)
        body = text_lines(forward.calls[0][0])
        self.assertIn("第一段正文", body)
        self.assertEqual(body[-1], delivery.SOURCE_LINE)
        self.assertEqual(loader.calls, [CDN_A, CDN_B, CDN_D])
        self.assertEqual(report.images_sent, 3)


class IntentGuardTests(unittest.IsolatedAsyncioTestCase):
    """planner 不允许带 URL/路径：带就明确失败，不采用其值。"""

    def test_url_in_intent_is_rejected(self):
        result = SimpleNamespace(
            status="ok",
            mode="article",
            message="",
            subject_name="x",
            candidates=[
                what_query.Candidate(
                    kind="chunk",
                    content_id=2001,
                    title="测试文章",
                    entity_id=None,
                    entity_name=None,
                    entity_kind="article",
                    value="正文",
                    source_path="/2",
                )
            ],
        )
        plan = delivery.prepare_delivery(
            get_fixture().conn,
            result,
            {"mode": "article", "shape": "whole_article", "url": "https://cdnimg-v2.gamekee.com/a.png"},
        )
        self.assertEqual(plan.kind, "invalid_intent")
        self.assertIn("URL/路径", " ".join(plan.notes))

    def test_path_in_intent_is_rejected(self):
        result = SimpleNamespace(status="not_found", mode="field", message="没查到", also_named=[])
        plan = delivery.prepare_delivery(
            get_fixture().conn,
            result,
            {"mode": "field", "shape": "single", "subject": {"name": "/旅人图鉴/优叶"}},
        )
        self.assertEqual(plan.kind, "invalid_intent")
        report = asyncio.run(
            delivery.deliver_what(stream_id=STREAM, send_forward=ForwardRecorder(), plan=plan)
        )
        self.assertEqual(report.status, "failed")


class CatalogTests(unittest.TestCase):
    """目录只列事实，不下载、不编造。"""

    def test_catalog_lists_counts_without_images(self):
        result = SimpleNamespace(
            status="ok",
            mode="catalog",
            message="",
            subject_name="魔王的立绘",
            candidates=[
                what_query.Candidate(
                    kind="fact",
                    content_id=2001,
                    title="魔王",
                    entity_id=1,
                    entity_name="魔王",
                    entity_kind="npc",
                    field_key="media_count.image",
                    value="12",
                    source_path="/",
                ),
                what_query.Candidate(
                    kind="fact",
                    content_id=2001,
                    title="魔王",
                    entity_id=1,
                    entity_name="魔王",
                    entity_kind="npc",
                    field_key="media_count.video",
                    value="1",
                    source_path="/",
                ),
            ],
            available_fields=["affiliation", "media_ref"],
            available_levels=[],
            available_groups=["立绘"],
            also_named=[],
            field_notes=[],
            rejected=[],
            stats={},
        )
        plan = delivery.prepare_delivery(get_fixture().conn, result, {"mode": "catalog"})
        self.assertEqual(plan.kind, "ok")
        self.assertEqual(plan.image_count(), 0)
        text = plan.groups[0].items[0].text
        self.assertIn("图片 12 项", text)
        self.assertIn("视频 1 项", text)
        self.assertIn("可用分组：立绘", text)
        self.assertNotIn("media_count", text)


if __name__ == "__main__":
    unittest.main()
