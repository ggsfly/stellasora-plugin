#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stellasora_what 离线闭环回归（1.5.3 离线部分）。

只测离线解析/建库/查询，不联网、不导入宿主模块、不发送任何消息。

运行：
    F:/MaiBot/.venv/Scripts/python.exe -B tests/test_what_offline.py

覆盖两组：
  A 合成 fixture —— 双格式分派、显式状态分流、等级类型混杂、主辅位分块、
                    表格纯图、术语方向、媒体三层与角色判定、精确等级、多候选、
                    FTS 跨标点假命中复核、无源字段不吞其它字段。
  B 真实语料抽样 —— 从 722 快照里取 715315 / 708439 / 728502 / 717070 / 705924 /
                    704594 做断言（快照不可用时跳过并打印 SKIP，不伪造通过）。
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import what_db  # noqa: E402
import what_offline  # noqa: E402
import what_parse  # noqa: E402
import what_query  # noqa: E402

SNAPSHOT = ROOT / ".zcode" / "what-research" / "20261125-full"


# ---------------------------------------------------------------- fixture


def simple_editor(text: str) -> dict:
    return {
        "type": "simpleEditor",
        "data": [{"type": "paragraph", "children": [{"text": text}]}],
    }


def rich_paragraph(*kids) -> dict:
    return {"type": "paragraph", "children": list(kids)}


def image_node(url: str, **extra) -> dict:
    node = {"type": "image", "src": url, "children": [{"text": ""}]}
    node.update(extra)
    return node


def illustrated(block_type: str, data: dict) -> dict:
    return {"type": "illustrated-book", "data": [{"type": block_type, "data": data}]}


def fight_info(levels: list) -> dict:
    return illustrated(
        "fight-info",
        {
            "title": "属性",
            "label": "Lv",
            "levelList": [
                {
                    "level": level,  # 故意保留 int / str 混杂
                    "levelInfoList": [
                        {"title": simple_editor("攻击"), "content": simple_editor(str(atk))},
                        {"title": simple_editor("生命"), "content": simple_editor(str(hp))},
                    ],
                }
                for level, atk, hp in levels
            ],
        },
    )


def skill_info(title: str, entries: list) -> dict:
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


def profile_block(attrs: list, *, title="基础信息") -> dict:
    return illustrated(
        "character-profile",
        {
            "title": title,
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


def detail_payload(content_id: int, nodes, *, title="测试角色", image_num=0, word_count=100):
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
            "image_num": image_num,
            "word_count": word_count,
            "updated_at": 1795000000,
            "content_json": json.dumps(nodes, ensure_ascii=False),
        },
    }


def tjmeta_payload(content_id: int, *, title="", page_title="琥珀", pc_src="//cdn/pc.png",
                   wap_src="//cdn/wap.png"):
    doc = {
        "__tjMeta": True,
        "pages": [
            {
                "id": "page-1",
                "title": page_title,
                "icon": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                "data": {
                    "nodeType": "container",
                    "children": [
                        {
                            "nodeType": "element",
                            "elementType": "image",
                            "name": "图片",
                            "props": {"src": pc_src},
                        },
                        {
                            "nodeType": "element",
                            "elementType": "text",
                            "name": "文本",
                            "props": {
                                "content": [
                                    {"type": "paragraph", "children": [{"text": "所属"}]}
                                ]
                            },
                        },
                    ],
                },
                "wapData": {
                    "nodeType": "container",
                    "children": [
                        {
                            "nodeType": "element",
                            "elementType": "image",
                            "name": "图片",
                            "props": {"src": wap_src},
                        }
                    ],
                },
            }
        ],
        "content_live_2d": json.dumps(
            [{"title": "琥珀", "cover": "//cdn/cover.png", "lh": "//cdn/lh.png", "live2d": []}]
        ),
        "content_bg": "//cdn/bg.png",
        "content_wap_bg": "",
    }
    return {
        "code": 0,
        "msg": "成功",
        "data": {
            "id": content_id,
            "content_id": content_id,
            "type": 4,
            "title": title,
            "image_num": 0,
            "word_count": 0,
            "updated_at": 1795000000,
            "content_json": json.dumps(doc, ensure_ascii=False),
        },
    }


class ParseArrayTests(unittest.TestCase):
    """数组分支：分派、显式状态、等级、主辅位、表格、术语、媒体角色。"""

    def test_two_schemas_dispatch(self):
        arr = what_parse.parse_raw_dir_entry(detail_payload(1, [rich_paragraph({"text": "正文"})]))
        self.assertEqual(arr.schema, what_parse.SCHEMA_ARRAY)
        self.assertEqual(arr.status, what_parse.STATUS_OK)
        obj = what_parse.parse_raw_dir_entry(tjmeta_payload(2, page_title="琥珀"))
        self.assertEqual(obj.schema, what_parse.SCHEMA_TJMETA)
        self.assertEqual(obj.status, what_parse.STATUS_OK)

    def test_bad_json_and_unknown_schema_are_distinct(self):
        bad = detail_payload(3, [])
        bad["data"]["content_json"] = "{not json"
        art = what_parse.parse_raw_dir_entry(bad)
        self.assertEqual(art.status, what_parse.STATUS_BAD_JSON)

        unknown = detail_payload(4, [])
        unknown["data"]["content_json"] = json.dumps(42)
        art2 = what_parse.parse_raw_dir_entry(unknown)
        self.assertEqual(art2.status, what_parse.STATUS_UNKNOWN_SCHEMA)

        empty = detail_payload(5, [])
        empty["data"]["content_json"] = ""
        art3 = what_parse.parse_raw_dir_entry(empty)
        self.assertEqual(art3.status, what_parse.STATUS_EMPTY)

    def test_media_only_vs_no_text(self):
        """只有媒体 → media_only；既无文字也无媒体 → 不复用 media_only。"""
        only_media = what_parse.parse_raw_dir_entry(
            detail_payload(6, [rich_paragraph(image_node("//cdn/a.png"))])
        )
        self.assertEqual(only_media.status, what_parse.STATUS_MEDIA_ONLY)

        nothing = what_parse.parse_raw_dir_entry(
            detail_payload(7, [{"type": "divider", "children": []}])
        )
        self.assertEqual(nothing.status, what_parse.STATUS_NO_TEXT)

    def test_empty_title_uses_page_title(self):
        """空 detail.title 时用 pages[0].title，且来源可追溯，不从空标题猜主体名。"""
        art = what_parse.parse_raw_dir_entry(tjmeta_payload(8, title="", page_title="缇莉娅"))
        self.assertEqual(art.title, "")
        self.assertEqual(art.title_alt, "缇莉娅")
        self.assertEqual(art.title_alt_source, "pages[0].title")
        self.assertEqual(art.display_title, "缇莉娅")

    def test_level_types_mixed_and_exact_match(self):
        art = what_parse.parse_raw_dir_entry(
            detail_payload(9, [fight_info([(1, 100, 700), ("90", 7484, 51179)])]),
            tree_path="/旅人图鉴/旅人图鉴/优叶",
        )
        self.assertEqual(art.levels, [1, 90])
        level90 = [f for f in art.facts if f.qualifier == "level:90" and f.field_key == "stat.attack"]
        self.assertEqual([f.value_text for f in level90], ["7484"])

    def test_main_and_sub_effect_are_separate_blocks(self):
        art = what_parse.parse_raw_dir_entry(
            detail_payload(
                10,
                [
                    skill_info("主位效果", [("", "全队水系伤害提升6%。")]),
                    skill_info("辅位效果", [("", "进入星塔时，获得水之音3个。")]),
                ],
            )
        )
        main = [f for f in art.facts if f.field_key == "sigil.main_effect"]
        sub = [f for f in art.facts if f.field_key == "sigil.sub_effect"]
        self.assertEqual(len(main), 1)
        self.assertEqual(len(sub), 1)
        self.assertIn("水系伤害提升6%", main[0].value_text)
        self.assertIn("水之音3个", sub[0].value_text)

    def test_skill_entries_name_desc_not_block_title(self):
        art = what_parse.parse_raw_dir_entry(
            detail_payload(11, [skill_info("天赋", [("", "第一条效果"), ("", "第二条效果")])])
        )
        keys = [(f.field_key, f.ordinal, f.value_text) for f in art.facts]
        self.assertEqual(
            keys,
            [("skill.talent", 1, "第一条效果"), ("skill.talent", 2, "第二条效果")],
        )

    def test_profile_salary_and_bonus_merge(self):
        art = what_parse.parse_raw_dir_entry(
            detail_payload(12, [profile_block([("生日", "3月5日"), ("薪资", "10"), ("俸禄", "20")])])
        )
        salary = [f for f in art.facts if f.field_key == "salary"]
        self.assertEqual([f.value_text for f in salary], ["10", "20"])
        birthday = [f for f in art.facts if f.field_key == "birthday"]
        self.assertEqual(birthday[0].value_text, "3月5日")

    def test_table_pure_image_is_needs_review(self):
        table = {
            "type": "table",
            "children": [
                {
                    "type": "table-row",
                    "children": [
                        {"type": "table-cell", "children": [rich_paragraph({"text": "弱点"})]},
                        {"type": "table-cell", "children": [rich_paragraph(image_node("//cdn/w.png"))]},
                    ],
                }
            ],
        }
        art = what_parse.parse_raw_dir_entry(detail_payload(13, [table]))
        cell_imgs = [m for m in art.media if m.role_evidence.startswith("表格单元格内纯图")]
        self.assertEqual(len(cell_imgs), 1)
        self.assertEqual(cell_imgs[0].role, what_parse.ROLE_UNKNOWN)
        self.assertEqual(cell_imgs[0].role_status, what_parse.NEEDS_REVIEW)

    def test_popover_term_direction(self):
        node = {
            "type": "popover-comment",
            "children": [{"text": "所属なし"}],
            "data": [{"type": "paragraph", "children": [{"text": "直接翻译为【无所属】"}]}],
        }
        art = what_parse.parse_raw_dir_entry(detail_payload(14, [rich_paragraph(node)]))
        self.assertEqual(len(art.terms), 1)
        term = art.terms[0]
        self.assertEqual(term.display_term, "所属なし")
        self.assertEqual(term.meaning_text, "直接翻译为【无所属】")
        self.assertEqual(term.lang, "ja")

    def test_nav_image_requires_link_evidence(self):
        """728502 的形状：导航图与 link 分处同一 infoList 项的 content / title 两子树。"""
        block = illustrated(
            "character-info",
            {
                "title": "导航",
                "infoList": [
                    {
                        "title": {
                            "type": "simpleEditor",
                            "data": [
                                {
                                    "type": "paragraph",
                                    "children": [
                                        {"type": "link", "url": "https://x/714580.html",
                                         "children": [{"text": "水"}]}
                                    ],
                                }
                            ],
                        },
                        "content": {
                            "type": "simpleEditor",
                            "data": [
                                {
                                    "type": "paragraph",
                                    "children": [
                                        {"text": ""},
                                        image_node("//cdn/nav.png"),
                                        {"text": ""},
                                    ],
                                }
                            ],
                        },
                    }
                ],
            },
        )
        art = what_parse.parse_raw_dir_entry(detail_payload(15, [block]))
        nav = [m for m in art.media if m.role == what_parse.ROLE_NAV]
        self.assertEqual(len(nav), 1)
        self.assertIn("link", nav[0].role_evidence)

    def test_audio_roles_never_body_image(self):
        block = illustrated(
            "audio-info",
            {
                "title": "语音",
                "tabs": [{"key": "k1", "label": "中配"}],
                "list": [
                    {
                        "title": simple_editor("日常语音"),
                        "filterTabKey": "k1",
                        "content": [
                            {
                                "audio": "//cdn/v.ogg",
                                "name": simple_editor("打招呼"),
                                "desc": simple_editor("你来了。"),
                            }
                        ],
                    }
                ],
            },
        )
        art = what_parse.parse_raw_dir_entry(detail_payload(16, [block]))
        audio = [m for m in art.media if m.kind == what_parse.KIND_AUDIO]
        self.assertTrue(audio)
        for media in audio:
            self.assertNotEqual(media.role, what_parse.ROLE_BODY)
            self.assertEqual(media.role, what_parse.ROLE_AUDIO_ASSET)
        self.assertEqual(audio[0].locale, "中配")

    def test_tjmeta_pc_and_mobile_are_separate_positions(self):
        art = what_parse.parse_raw_dir_entry(
            tjmeta_payload(17, pc_src="//cdn/pc.png", wap_src="//cdn/wap.png")
        )
        design = [m for m in art.media if m.side in ("pc", "mobile")]
        self.assertEqual({m.side for m in design}, {"pc", "mobile"})
        self.assertEqual({m.url for m in design}, {"//cdn/pc.png", "//cdn/wap.png"})
        for media in design:
            self.assertIn("/children", media.source_path)

    def test_live2d_and_background_not_sendable(self):
        art = what_parse.parse_raw_dir_entry(tjmeta_payload(18))
        for media in art.media:
            if media.kind in (what_parse.KIND_LIVE2D, what_parse.KIND_BACKGROUND):
                self.assertNotIn(media.role, (what_parse.ROLE_BODY, what_parse.ROLE_ART))

    def test_all_pointers_resolve(self):
        nodes = [
            profile_block([("生日", "3月5日")]),
            fight_info([(90, 7484, 51179)]),
            skill_info("主位效果", [("梦幻泡影", "全队水系伤害提升6%。")]),
            rich_paragraph({"text": "正文"}, image_node("//cdn/b.png")),
        ]
        payload = detail_payload(19, nodes)
        art = what_parse.parse_raw_dir_entry(payload)
        doc = json.loads(payload["data"]["content_json"])
        missing = []
        for item in art.chunks + art.media:
            ok, _ = what_parse.resolve_pointer(doc, item.source_path)
            if not ok:
                missing.append(item.source_path)
        self.assertEqual(missing, [])


class QueryTests(unittest.TestCase):
    """查询：规范字段、精确等级、无源字段不吞项、多候选、跨标点复核、无名称检索。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmpdir.name) / "t.sqlite3"

        traveler = what_parse.parse_raw_dir_entry(
            detail_payload(
                1001,
                [
                    profile_block([("生日", "3月5日"), ("所属", "地理协会"), ("薪资", "10")]),
                    fight_info([(1, 100, 700), ("90", 7484, 51179)]),
                ],
                title="优叶",
            ),
            tree_path="/旅人图鉴/旅人图鉴/优叶",
        )
        sigil_a = what_parse.parse_raw_dir_entry(
            detail_payload(
                1002,
                [
                    illustrated(
                        "audio-info",
                        {"title": "基本信息", "tabs": [], "list": []},
                    ),
                    skill_info("主位效果", [("梦幻泡影", "全队水系伤害提升6%。")]),
                    skill_info("辅位效果", [("", "进入星塔时，获得水之音3个，每个提升0.3%水系伤害。")]),
                ],
                title="泡沫",
            ),
            tree_path="/秘纹图鉴/秘纹图鉴/泡沫",
        )
        sigil_b = what_parse.parse_raw_dir_entry(
            detail_payload(
                1003,
                [skill_info("辅位效果", [("", "进入星塔时，获得水之音3个，每个提升0.3%水系伤害。")])],
                title="浪花",
            ),
            tree_path="/秘纹图鉴/秘纹图鉴/浪花",
        )
        fake_hit = what_parse.parse_raw_dir_entry(
            detail_payload(
                1004,
                [rich_paragraph({"text": "水、系伤害测试用段落，用来验证跨标点假命中被剔除。"})],
                title="测试文章",
            )
        )
        dup_name = what_parse.parse_raw_dir_entry(
            detail_payload(1005, [rich_paragraph({"text": "同名实体之一"})], title="琥珀"),
            tree_path="/旅人图鉴/旅人图鉴/琥珀",
        )
        # 同名但不同种类的第二个实体（真实语料里「琥珀」既是旅人也是 NPC 对话）
        dup_name_2 = what_parse.parse_raw_dir_entry(
            detail_payload(1006, [rich_paragraph({"text": "同名实体之二"})], title="琥珀"),
            tree_path="/NPC/卫星图鉴/琥珀",
        )
        cls.stats = what_db.build_from_articles(
            cls.db_path,
            [traveler, sigil_a, sigil_b, fake_hit, dup_name, dup_name_2],
            meta={"test": True},
        )
        cls.conn = what_db.open_db(str(cls.db_path))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmpdir.cleanup()

    def q(self, intent):
        return what_query.run_query(self.conn, intent)

    def test_field_canonical_name_mapping(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["stat.attack"],
                "levels": [90],
            }
        )
        self.assertEqual(r.status, "ok")
        self.assertEqual([c.value for c in r.candidates], ["7484"])
        self.assertEqual(r.candidates[0].qualifier, "level:90")

    def test_field_chinese_alias_same_result(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["攻击"],
                "levels": [90],
            }
        )
        self.assertEqual([c.value for c in r.candidates], ["7484"])

    def test_exact_level_does_not_leak_other_level(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["stat.attack"],
                "levels": [1],
            }
        )
        self.assertEqual([(c.value, c.qualifier) for c in r.candidates], [("100", "level:1")])

    def test_illegal_level_lists_available(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["stat.attack"],
                "levels": [95],
            }
        )
        self.assertEqual(r.status, "not_found")
        self.assertIn(90, r.available_levels)
        self.assertEqual(r.candidates, [])

    def test_slot_condition_narrows_field_not_text(self):
        """槽位（主位/辅位）是结构维度：正文里没有「主位」二字也要命中。"""
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": None},
                "fields": ["sigil.main_effect", "sigil.sub_effect"],
                "conditions": {"slot": "主位", "attribute": "水系"},
            }
        )
        self.assertEqual(len(r.candidates), 1)
        self.assertEqual(r.candidates[0].field_key, "sigil.main_effect")
        self.assertIn("全队水系伤害提升6%", r.candidates[0].value)

        conflict = self.q(
            {
                "mode": "field",
                "subject": {"name": None},
                "fields": ["sigil.sub_effect"],
                "conditions": {"slot": "主位"},
            }
        )
        self.assertEqual(conflict.status, "not_found")
        self.assertEqual(conflict.candidates, [])

    def test_age_no_source_but_other_fields_still_returned(self):
        only_age = self.q(
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["age"],
            }
        )
        self.assertEqual(only_age.status, "no_source")
        self.assertIn("未标注年龄", only_age.message)

        mixed = self.q(
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["age", "birthday"],
            }
        )
        self.assertEqual(mixed.status, "ok")
        self.assertEqual([c.value for c in mixed.candidates], ["3月5日"])
        self.assertIn("未标注年龄", mixed.message)

    def test_multi_candidates_are_all_kept(self):
        r = self.q(
            {
                "mode": "article",
                "literal": "进入星塔时",
                "query": "进入星塔时",
            }
        )
        self.assertEqual(r.status, "ok")
        titles = {c.title for c in r.candidates}
        self.assertIn("泡沫", titles)
        self.assertIn("浪花", titles)
        self.assertEqual(r.stats["distinct_articles"], 2)

    def test_article_kind_filter_narrows_scope(self):
        inside = self.q(
            {"mode": "article", "literal": "进入星塔时", "query": "进入星塔时", "kind": "sigil"}
        )
        outside = self.q(
            {"mode": "article", "literal": "进入星塔时", "query": "进入星塔时", "kind": "traveler"}
        )
        self.assertTrue(inside.candidates)
        self.assertEqual(outside.candidates, [])

    def test_cross_punctuation_false_hit_is_rejected(self):
        r = self.q(
            {
                "mode": "article",
                "literal": "水系伤害",
                "query": "水系伤害",
            }
        )
        for cand in r.candidates:
            self.assertIn("水系伤害", cand.value)
        rejected_bodies = [x["body"] for x in r.rejected]
        self.assertTrue(any("水、系伤害" in b for b in rejected_bodies))

    def test_verify_before_truncate(self):
        r = self.q(
            {"mode": "article", "literal": "水系伤害", "query": "水系伤害", "limit": 1}
        )
        self.assertEqual(len(r.candidates), 1)
        self.assertIn("水系伤害", r.candidates[0].value)
        self.assertEqual(r.stats["fts_hits"], r.stats["verified"] + r.stats["rejected"])

    def test_same_name_keeps_all_candidates(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "琥珀"},
                "fields": ["birthday"],
            }
        )
        # 同名多实体：列出全部候选，不静默挑第一条
        self.assertEqual(len(r.also_named), 2)
        kinds = {x["kind"] for x in r.also_named}
        self.assertEqual(kinds, {"traveler", "npc"})

    def test_media_unknown_not_sendable(self):
        r = self.q({"mode": "media", "subject": {"name": "优叶", "kind": "traveler"}})
        for cand in r.candidates:
            self.assertIn(cand.role, what_query.SENDABLE_ROLES)
            self.assertEqual(cand.role_status, "judged")

    def test_missing_subject_name_rejected(self):
        r = self.q({"mode": "field", "fields": ["birthday"]})
        self.assertEqual(r.status, "invalid_intent")

    def test_unknown_field_reports_clarify(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["不存在的字段"],
            }
        )
        self.assertEqual(r.status, "clarify")


class SegmentationTests(unittest.TestCase):
    """分词契约：写入与查询共用同一函数，CJK 逐字。"""

    def test_seg_is_symmetric_and_char_wise(self):
        text = "优叶 3月5日"
        encoded = what_db.seg(text)
        self.assertIn("\u0001优\u0001", encoded)
        self.assertIn("\u0001叶\u0001", encoded)
        # 非 CJK 原样保留
        self.assertIn("3月", encoded.replace("\u0001", ""))

    def test_fts_query_matches_write_side(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "s.sqlite3"
            art = what_parse.parse_raw_dir_entry(
                detail_payload(2001, [rich_paragraph({"text": "全队水系伤害提升6%"})])
            )
            what_db.build_from_articles(path, [art])
            conn = what_db.open_db(str(path))
            try:
                q = what_db.fts_query("水系伤害")
                rows = conn.execute(
                    "SELECT c.body FROM chunk_fts JOIN chunk c ON c.chunk_id=chunk_fts.rowid"
                    " WHERE chunk_fts MATCH ?",
                    (q,),
                ).fetchall()
                self.assertTrue(rows)
                self.assertIn("水系伤害", rows[0]["body"])
            finally:
                conn.close()
        finally:
            tmp.cleanup()


class TabMediaRoleTests(unittest.TestCase):
    """① 页签媒体按真实文件类型定角色：mp4 不是 character_art，封面仍是图片用途。"""

    def test_tab_video_src_is_video_asset_not_art(self):
        block = illustrated(
            "tab-info",
            {
                "title": "视频",
                "tabList": [
                    {
                        "title": "第一话",
                        "type": "video",
                        "content": [{"src": "//cdn/ep1.mp4", "cover": "//cdn/ep1.png"}],
                    }
                ],
            },
        )
        art = what_parse.parse_raw_dir_entry(detail_payload(21, [block]))
        src = [m for m in art.media if m.url.endswith(".mp4")]
        cover = [m for m in art.media if m.url.endswith(".png")]
        self.assertEqual(src[0].kind, what_parse.KIND_VIDEO)
        self.assertEqual(src[0].role, what_parse.ROLE_VIDEO_ASSET)
        self.assertIn("不作图片候选", src[0].role_evidence)
        self.assertEqual(cover[0].kind, what_parse.KIND_IMAGE)
        self.assertEqual(cover[0].role, what_parse.ROLE_UNKNOWN)
        self.assertEqual(cover[0].role_status, what_parse.NEEDS_REVIEW)
        self.assertIn("视频封面", cover[0].role_evidence)

    def test_tab_string_video_not_art(self):
        block = illustrated(
            "tab-info",
            {"title": "视频", "tabList": [{"title": "第一话", "content": ["//cdn/ep1.mp4"]}]},
        )
        art = what_parse.parse_raw_dir_entry(detail_payload(22, [block]))
        item = [m for m in art.media if m.url.endswith(".mp4")][0]
        self.assertEqual(item.kind, what_parse.KIND_VIDEO)
        self.assertEqual(item.role, what_parse.ROLE_VIDEO_ASSET)


class MediaOrderTests(unittest.TestCase):
    """G13：媒体顺序按真实节点下标（数字比较），不是字符串比较。"""

    def test_natural_key_compares_indexes_numerically(self):
        paths = ["/a/content/0", "/a/content/10", "/a/content/2", "/a/content/17", "/a/content/9"]
        self.assertEqual(
            sorted(paths, key=what_query.natural_key),
            ["/a/content/0", "/a/content/2", "/a/content/9", "/a/content/10", "/a/content/17"],
        )

    def test_real_swimsuit_group_keeps_position_order(self):
        """715315「泳装」18 条必须按 0..17 的顺序出现，不能 0,1,10…17,2…9。"""
        path = SNAPSHOT / "raw" / "detail" / "715315.json"
        if not path.exists():
            self.skipTest("715315 不在快照内")
        art = what_parse.parse_raw_dir_entry(json.loads(path.read_text(encoding="utf-8")))
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "order.sqlite3"
            what_db.build_from_articles(db_path, [art])
            conn = what_db.open_db(str(db_path), readonly=True)
            try:
                result = what_query.run_query(
                    conn,
                    {
                        "mode": "media",
                        "subject": {"name": art.entity_name},
                        "groups": ["泳装"],
                    },
                )
            finally:
                conn.close()
        finally:
            tmp.cleanup()
        self.assertEqual(len(result.candidates), 18)
        indexes = [int(c.source_path.rsplit("/", 1)[1]) for c in result.candidates]
        self.assertEqual(indexes, list(range(18)))
        # 候选带 media_kind，聚合/发送层能判“是不是图片”
        self.assertEqual({c.media_kind for c in result.candidates}, {"image"})


@unittest.skipUnless(SNAPSHOT.exists(), "研究快照不存在，跳过真实语料抽样")
class RealCorpusTests(unittest.TestCase):
    """真实语料的抽样断言（不改库、不联网）。"""

    @classmethod
    def setUpClass(cls):
        cls.articles = {}
        for cid in (715315, 708439, 728502, 717070, 705924, 704594):
            path = SNAPSHOT / "raw" / "detail" / f"{cid}.json"
            if not path.exists():
                continue
            cls.articles[cid] = what_parse.parse_raw_dir_entry(
                json.loads(path.read_text(encoding="utf-8"))
            )

    def test_youye_level90_attack(self):
        art = self.articles.get(715315)
        if art is None:
            self.skipTest("715315 不在快照内")
        facts = [f for f in art.facts if f.field_key == "stat.attack" and f.qualifier == "level:90"]
        self.assertEqual([f.value_text for f in facts], ["7484"])
        self.assertIn(90, art.levels)

    def test_paomo_main_and_sub(self):
        art = self.articles.get(708439)
        if art is None:
            self.skipTest("708439 不在快照内")
        main = [f for f in art.facts if f.field_key == "sigil.main_effect"][0]
        sub = [f for f in art.facts if f.field_key == "sigil.sub_effect"][0]
        self.assertIn("水系伤害提升6%", main.value_text)
        self.assertIn("水之音3个", sub.value_text)
        self.assertNotEqual(main.source_path, sub.source_path)

    def test_728502_nav_images(self):
        art = self.articles.get(728502)
        if art is None:
            self.skipTest("728502 不在快照内")
        nav = [m for m in art.media if m.role == what_parse.ROLE_NAV]
        self.assertEqual(len(nav), 6)
        for media in nav:
            self.assertIn("link", media.role_evidence)
        # 页首署名横幅不当正文图
        opening = [m for m in art.media if "文章开头" in m.role_evidence]
        self.assertTrue(opening)
        for media in opening:
            self.assertEqual(media.role, what_parse.ROLE_UNKNOWN)
            self.assertEqual(media.role_status, what_parse.NEEDS_REVIEW)

    def test_717070_dual_side_and_empty_title(self):
        art = self.articles.get(717070)
        if art is None:
            self.skipTest("717070 不在快照内")
        design = [m for m in art.media if m.side in ("pc", "mobile")]
        self.assertEqual({m.side for m in design}, {"pc", "mobile"})
        self.assertEqual(len({m.url for m in design}), 2, "PC 与移动是两张不同文件")
        for media in design:
            self.assertIn("/children/", media.source_path, "指针必须含 children 层才能回原文")

    def test_705924_weakness_cell_is_image_only(self):
        art = self.articles.get(705924)
        if art is None:
            self.skipTest("705924 不在快照内")
        weak = [m for m in art.media if m.role_evidence.startswith("表格单元格内纯图")]
        self.assertTrue(weak)
        for media in weak:
            self.assertEqual(media.role_status, what_parse.NEEDS_REVIEW)
        # 不能从图里造出弱点文字
        self.assertFalse([f for f in art.facts if f.field_key == "monster.weakness"])

    def test_704594_term_direction(self):
        art = self.articles.get(704594)
        if art is None:
            self.skipTest("704594 不在快照内")
        terms = [t for t in art.terms if t.display_term == "所属なし"]
        self.assertTrue(terms)
        self.assertEqual(terms[0].meaning_text, "直接翻译为【无所属】")

    def test_715315_voice_not_double_counted(self):
        """语音条目 desc 里嵌了整块 audio-info：媒体位置要收，事实不能翻倍。"""
        art = self.articles.get(715315)
        if art is None:
            self.skipTest("715315 不在快照内")
        voice = [f for f in art.facts if f.field_key == "voice.line"]
        self.assertEqual(len(voice), 188, "188 条语音 = 中配/日配 × 59/30/5")
        self.assertEqual(len({f.source_path for f in voice}), 188)
        audio_media = [m for m in art.media if m.kind == what_parse.KIND_AUDIO]
        self.assertEqual(len(audio_media), 188)
        tab_refs = [m for m in art.media if "tabList" in m.source_path]
        self.assertEqual(len(tab_refs), 67, "页签立绘引用 67 条")


class GoldCorpusTests(unittest.TestCase):
    """真实 722 建库后的黄金集 / 等级 / 分区 / scope 回归（临时目录，只读快照）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        articles, entries, report = what_offline.parse_snapshot(SNAPSHOT)
        assert report["parsed"] == 722 and not report["blocking"], report
        cls.db = Path(cls.tmp.name) / "corpus.sqlite3"
        cls.stats = what_db.build_from_articles(cls.db, articles, entries=entries)
        cls.conn = what_db.open_db(str(cls.db), readonly=True)
        gold_path = ROOT / ".zcode" / "what-research" / "20261125-offline-eval" / "gold.json"
        cases = json.loads(gold_path.read_text(encoding="utf-8"))["cases"]
        cls.gold = {c["id"]: c for c in cases}

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def q(self, intent):
        return what_query.run_query(self.conn, intent)

    # ---- G07：同条条件（不跨条词袋）

    def test_g07_conditions_single_candidate(self):
        case = self.gold["G07-shield-water-same-entry"]
        r = self.q(case["intent"])
        self.assertEqual([c.content_id for c in r.candidates], [709299])
        self.assertEqual([c.source_path for c in r.candidates], ["/3/data/0/data/skillList/1/desc"])
        self.assertIn(case["expected"]["text"], r.candidates[0].value)
        same_entry = [x for x in r.rejected if x.get("reason") == "condition_not_in_same_entry"]
        self.assertGreaterEqual(len(same_entry), 5, "另 5 条异构属性必须因“不同条”被剔除")
        for x in r.rejected:
            self.assertEqual(x["reason"], "condition_not_in_same_entry")

    # ---- G08：24 篇同文本辅位，逐条核对内容，不只核对数量

    def test_g08_each_article_has_same_sub_effect(self):
        case = self.gold["G08-duplicate-effect-all-candidates"]
        text = case["expected"]["text"]
        r = self.q({"mode": "article", "query": text, "limit": 400})
        got = sorted({c.content_id for c in r.candidates})
        self.assertEqual(got, sorted(case["expected_ids"]))
        self.assertEqual(r.stats["truncated"], 0)
        for cid in case["expected_ids"]:
            rows = self.conn.execute(
                "SELECT value_text FROM fact WHERE content_id=? AND field_key='sigil.sub_effect'",
                (cid,),
            ).fetchall()
            self.assertTrue(
                any(text in row["value_text"] for row in rows),
                f"{cid} 的辅位效果里没有目标文本",
            )

    # ---- 等级有效但该等级没有这项指标（708439 level:1 只有攻击/生命）

    def test_level_valid_but_field_absent_at_level(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "泡沫", "kind": "sigil"},
                "fields": ["stat.element_damage.water"],
                "levels": [1],
            }
        )
        self.assertEqual(r.status, "not_found")
        self.assertEqual(r.candidates, [])
        self.assertIn("有效", r.message)
        self.assertIn("没有", r.message)
        self.assertNotIn("该等级不在本页等级表内", r.message)
        reasons = {x["reason"] for x in r.rejected}
        self.assertEqual(reasons, {"field_absent_at_level"})

    def test_invalid_level_still_reports_level_not_in_page(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "泡沫", "kind": "sigil"},
                "fields": ["stat.attack"],
                "levels": [95],
            }
        )
        self.assertEqual(r.status, "not_found")
        self.assertEqual({x["reason"] for x in r.rejected}, {"level_not_in_page"})
        self.assertIn("该等级不在本页等级表内", r.message)

    def test_mixed_fields_keep_found_values(self):
        r = self.q(
            {
                "mode": "field",
                "subject": {"name": "泡沫", "kind": "sigil"},
                "fields": ["stat.element_damage.water", "sigil.main_effect"],
                "levels": [1],
            }
        )
        self.assertEqual(r.status, "ok")
        self.assertEqual([c.field_key for c in r.candidates], ["sigil.main_effect"])
        self.assertIn("没有", r.message)
        self.assertIn("其余字段已给出", r.message)

    # ---- 分区：真实顶层，不拿提示/标题当分类

    def test_categories_and_tree_evidence(self):
        expected = {
            705924: "怪物",
            699467: "NPC",
            715315: "旅人图鉴",
            708439: "秘纹",
            712509: "攻略区",
            704456: "剧情区",
            700979: "二创区",
            698918: "游戏鉴赏",
            701507: None,
        }
        for cid, want in expected.items():
            row = self.conn.execute(
                "SELECT category,tree_path FROM article WHERE content_id=?", (cid,)
            ).fetchone()
            self.assertEqual(row["category"], want, f"{cid} category")
        # 提示文字只出现在 tree_path 证据里，不能当 category
        row = self.conn.execute(
            "SELECT category,tree_path FROM article WHERE content_id=705924"
        ).fetchone()
        self.assertIn("建议使用PC端或手机横屏查看", row["tree_path"])
        self.assertEqual(row["category"], "怪物")

    def test_category_distribution_matches_verified_tree(self):
        got = {
            row["category"]: row["n"]
            for row in self.conn.execute(
                "SELECT category, COUNT(*) AS n FROM article GROUP BY category"
            )
        }
        expected = {
            None: 95,
            "游戏鉴赏": 86,
            "旅人图鉴": 42,
            "NPC": 126,
            "二创区": 2,
            "剧情区": 8,
            "怪物": 250,
            "秘纹": 106,
            "攻略区": 7,
        }
        self.assertEqual(got, expected)

    # ---- scope：wiki / article 分开，不虚构 announcement，索引覆盖不缩

    def test_scope_enum_and_chunk_coverage(self):
        scopes = {
            row["scope"]: row["n"]
            for row in self.conn.execute(
                "SELECT scope, COUNT(DISTINCT content_id) AS n FROM chunk GROUP BY scope"
            )
        }
        self.assertEqual(set(scopes), {"wiki", "article"}, "DDL 只有 wiki/article，不虚构公告")
        # 645 篇 ok 有正文；77 篇 media_only 本来就没有文字块，不算“索引缩水”
        self.assertEqual(sum(scopes.values()), 645)
        for cid, want in ((705924, "wiki"), (712509, "article"), (704648, "wiki"), (701507, "wiki")):
            row = self.conn.execute(
                "SELECT scope FROM chunk WHERE content_id=? LIMIT 1", (cid,)
            ).fetchone()
            self.assertIsNotNone(row, f"{cid} 应有 chunk")
            self.assertEqual(row["scope"], want, f"{cid} scope")
        chunks = self.conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
        self.assertEqual(chunks, self.stats.chunks, "建库统计与实际 chunk 数必须一致（覆盖不缩）")
        self.assertGreater(chunks, 0)


class BuildCliTests(unittest.TestCase):
    """CLI：staging → 发布、拒绝覆盖、缺清单非零退出。"""

    def test_build_publishes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap"
            (snapshot / "raw" / "detail").mkdir(parents=True)
            (snapshot / "raw" / "list").mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"targets": [3001]}), encoding="utf-8"
            )
            payload = detail_payload(3001, [rich_paragraph({"text": "正文内容"})])
            (snapshot / "raw" / "detail" / "3001.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            build_dir = Path(tmp) / "out"
            rc = what_offline.main(
                ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
            )
            self.assertEqual(rc, 0)
            self.assertTrue((build_dir / "what.sqlite3").exists())

            before = (build_dir / "what.sqlite3").read_bytes()
            rc2 = what_offline.main(
                ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
            )
            self.assertEqual(rc2, 2, "已存在正式库时必须拒绝，且不给覆盖开关")
            # 拒绝时旧库必须原样保留，不能被「先删后建」抹掉
            self.assertEqual((build_dir / "what.sqlite3").read_bytes(), before)

    def test_duplicate_targets_block_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap"
            (snapshot / "raw" / "detail").mkdir(parents=True)
            (snapshot / "raw" / "list").mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"targets": [5001, 5001]}), encoding="utf-8"
            )
            payload = detail_payload(5001, [rich_paragraph({"text": "内容"})])
            (snapshot / "raw" / "detail" / "5001.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            build_dir = Path(tmp) / "out"
            rc = what_offline.main(
                ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
            )
            self.assertEqual(rc, 3, "重复目标必须显式失败，不能用去重后的部分结果冒充完整")
            self.assertFalse((build_dir / "what.sqlite3").exists())
            report = json.loads((build_dir / "build-report.json").read_text(encoding="utf-8"))
            codes = [e["code"] for e in report["snapshot_report"]["manifest_errors"]]
            self.assertIn("duplicate_target_ids", codes)

    def test_previous_staging_is_not_deleted(self):
        """旧 staging（别的运行留下的）不能被本次构建删掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap"
            (snapshot / "raw" / "detail").mkdir(parents=True)
            (snapshot / "raw" / "list").mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"targets": [6001]}), encoding="utf-8"
            )
            (snapshot / "raw" / "detail" / "6001.json").write_text(
                json.dumps(detail_payload(6001, [rich_paragraph({"text": "内容"})]), ensure_ascii=False),
                encoding="utf-8",
            )
            build_dir = Path(tmp) / "out"
            old_staging = build_dir / "staging"
            old_staging.mkdir(parents=True)
            sentinel = old_staging / "build.sqlite3"
            sentinel.write_bytes(b"old staging")
            rc = what_offline.main(
                ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(sentinel.exists(), "旧 staging 应原样保留")
            self.assertEqual(sentinel.read_bytes(), b"old staging")
            self.assertTrue((build_dir / "what.sqlite3").exists())

    def test_publish_failure_keeps_staging(self):
        """切换正式库失败时，不能留下半截正式库，staging 也不能被清掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap"
            (snapshot / "raw" / "detail").mkdir(parents=True)
            (snapshot / "raw" / "list").mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"targets": [7001]}), encoding="utf-8"
            )
            (snapshot / "raw" / "detail" / "7001.json").write_text(
                json.dumps(detail_payload(7001, [rich_paragraph({"text": "内容"})]), ensure_ascii=False),
                encoding="utf-8",
            )
            build_dir = Path(tmp) / "out"
            with mock.patch.object(what_offline.os, "link", side_effect=OSError("disk full")):
                rc = what_offline.main(
                    ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
                )
            self.assertEqual(rc, 4)
            self.assertFalse((build_dir / "what.sqlite3").exists())
            kept = list(build_dir.glob("staging-*/build.sqlite3"))
            self.assertEqual(len(kept), 1, "staging 结果应保留供复查")

    def test_late_library_is_not_overwritten(self):
        """TOCTOU：staging 建好后、发布前别的人创建了正式库 → 不覆盖，非零退出，staging 保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap"
            (snapshot / "raw" / "detail").mkdir(parents=True)
            (snapshot / "raw" / "list").mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"targets": [8001]}), encoding="utf-8"
            )
            (snapshot / "raw" / "detail" / "8001.json").write_text(
                json.dumps(detail_payload(8001, [rich_paragraph({"text": "内容"})]), ensure_ascii=False),
                encoding="utf-8",
            )
            build_dir = Path(tmp) / "out"
            build_dir.mkdir(parents=True)
            final_path = build_dir / "what.sqlite3"
            sentinel = b"late library bytes"
            real_build = what_offline.what_db.build_from_articles

            def late_hook(*args, **kwargs):
                stats = real_build(*args, **kwargs)
                final_path.write_bytes(sentinel)  # 晚到的同名库，落在发布之前
                return stats

            with mock.patch.object(what_offline.what_db, "build_from_articles", side_effect=late_hook):
                rc = what_offline.main(
                    ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
                )
            self.assertNotEqual(rc, 0, "晚到库存在时发布必须失败，不能 rc=0")
            self.assertEqual(final_path.read_bytes(), sentinel, "晚到库字节必须原样保留")
            self.assertEqual(
                len(list(build_dir.glob("staging-*/build.sqlite3"))), 1, "本次结果保留在独立 staging"
            )

    def test_missing_detail_file_blocks_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap"
            (snapshot / "raw" / "detail").mkdir(parents=True)
            (snapshot / "raw" / "list").mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"targets": [4001]}), encoding="utf-8"
            )
            build_dir = Path(tmp) / "out"
            rc = what_offline.main(
                ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
            )
            self.assertEqual(rc, 3)
            self.assertFalse((build_dir / "what.sqlite3").exists())

    def test_id_mismatch_blocks_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap"
            (snapshot / "raw" / "detail").mkdir(parents=True)
            (snapshot / "raw" / "list").mkdir(parents=True)
            (snapshot / "manifest.json").write_text(
                json.dumps({"targets": [4002]}), encoding="utf-8"
            )
            payload = detail_payload(9999, [rich_paragraph({"text": "内容"})])
            (snapshot / "raw" / "detail" / "4002.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            build_dir = Path(tmp) / "out"
            rc = what_offline.main(
                ["--snapshot", str(snapshot), "--build-dir", str(build_dir), "build"]
            )
            self.assertEqual(rc, 3)
            self.assertFalse((build_dir / "what.sqlite3").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
