# -*- coding: utf-8 -*-
"""stellasora_what 离线检索：意图 → 结构化事实 / 原文片段 / 媒体，全部可回指原文。

纪律（计划 §4.5、§4.6、§5）：

- **结论性字段先于描述性匹配**：生日、所属、武器、等级面板、主位/辅位效果先按
  `(entity, field_key, qualifier, ordinal)` 精确取，不靠词袋命中凑答案。
- **FTS 只是候选池**：`seg()` 保证 token 相邻，但**原字符不一定相邻**（「水、系伤害」会
  命中「水系伤害」的短语）。因此每个 FTS 候选都要用 `instr(body, ?) > 0` 回原文复核，
  复核不过的剔除；**先复核再截断**，否则假命中会挤掉真候选。
- **不替用户选第一条**：同名多候选、多篇命中、多合格片段全部保留，附各自来源。
- **缺证据就说缺**：`age` 无数据源时明确说没有，但**同一次请求里其它可查字段照常给**。
- **支持无名称的描述题**：`mode=article` 允许不带 `subject`，用 `literal` 原文子串 +
  可选 `kind`/`field_key` 直接在全库做同条检索（六属性效果那种题必须能答）。

输出一律是结构化 `QueryResult`，不发送任何 QQ 消息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import sqlite3
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from what_db import fts_query
import what_parse

SENDABLE_KINDS = what_parse.SENDABLE_KINDS

# 字段词表：planner 传入的字段名（含常见同义写法）→ 真实 field_key。
# 面板指标落库时已规范成 `stat.attack` 这类英文键（原文标签存在 extra.label），
# 所以这里两种写法都要能映射到同一 field_key。
FIELD_SYNONYMS: Dict[str, str] = {
    "birthday": "birthday",
    "生日": "birthday",
    "诞生日": "birthday",
    "affiliation": "affiliation",
    "所属": "affiliation",
    "阵营": "affiliation",
    "weapon": "weapon",
    "武器": "weapon",
    "salary": "salary",
    "薪资": "salary",
    "俸禄": "salary",
    "工资": "salary",
    "address": "address",
    "地址": "address",
    "experience": "experience",
    "经验": "experience",
    "age": "age",
    "年龄": "age",
    "几岁": "age",
    "skill.talent": "skill.talent",
    "天赋": "skill.talent",
    "sigil.main_effect": "sigil.main_effect",
    "主位": "sigil.main_effect",
    "主位效果": "sigil.main_effect",
    "sigil.sub_effect": "sigil.sub_effect",
    "辅位": "sigil.sub_effect",
    "辅位效果": "sigil.sub_effect",
    "monster.weakness": "monster.weakness",
    "弱点": "monster.weakness",
    # 面板：规范名与中文标签互为同义
    "stat.attack": "stat.attack",
    "stat.攻击": "stat.attack",
    "攻击": "stat.attack",
    "攻击力": "stat.attack",
    "stat.hp": "stat.hp",
    "stat.生命": "stat.hp",
    "生命": "stat.hp",
    "生命值": "stat.hp",
    "stat.defense": "stat.defense",
    "stat.防御": "stat.defense",
    "防御": "stat.defense",
    "防御力": "stat.defense",
    "stat.crit_rate": "stat.crit_rate",
    "stat.暴击率": "stat.crit_rate",
    "暴击率": "stat.crit_rate",
    "stat.crit_dmg": "stat.crit_dmg",
    "stat.暴击伤害": "stat.crit_dmg",
    "暴击伤害": "stat.crit_dmg",
    "stat.toughness": "stat.toughness",
    "韧性": "stat.toughness",
}

# 元素伤害：`stat.element_damage.<elem>`，水/火/地/风/光/暗 各自一个键，
# 绝不能把某个元素的效果当成另一个元素（跨条词袋的典型错误）。
ELEMENT_NAMES = {"水": "water", "火": "fire", "地": "earth", "风": "wind", "光": "light", "暗": "dark"}
for _zh, _en in ELEMENT_NAMES.items():
    FIELD_SYNONYMS[f"{_zh}系伤害"] = f"stat.element_damage.{_en}"
    FIELD_SYNONYMS[f"stat.{_zh}系伤害"] = f"stat.element_damage.{_en}"
    FIELD_SYNONYMS[f"stat.element_damage.{_en}"] = f"stat.element_damage.{_en}"

# 该板块「识别得出但无数据源」的字段：明确回答没有，绝不改查相邻字段，
# 但也**不影响同一次请求里其它字段的返回**。
NO_SOURCE_FIELDS = {
    "age": "该板块未标注年龄字段；生日是生日，不能当年龄用。",
}

# 只有这些 role 允许进入「可发送图片」建议（role 来自解析期证据，不是推断）
SENDABLE_ROLES = ("body_image", "character_art", "card_banner")

DEFAULT_FTS_LIMIT = 400


@dataclass(slots=True)
class Candidate:
    """一个合格候选：字段值或原文片段，都带可复核来源。"""

    kind: str                    # fact / chunk / media
    content_id: int
    title: str
    entity_id: Optional[int]
    entity_name: Optional[str]
    entity_kind: Optional[str]
    field_key: Optional[str] = None
    qualifier: str = ""
    ordinal: int = 0
    value: str = ""
    source_path: str = ""
    group_label: Optional[str] = None
    role: Optional[str] = None
    role_status: Optional[str] = None
    role_evidence: Optional[str] = None
    media_kind: Optional[str] = None
    url: Optional[str] = None
    side: Optional[str] = None
    equivalent_text: Optional[str] = None
    heading: str = ""

    def citation(self) -> str:
        parts = [f"GameKee · {self.title}"]
        if self.field_key:
            parts.append(self.field_key)
        if self.qualifier:
            parts.append(self.qualifier)
        parts.append(self.source_path)
        return " · ".join(p for p in parts if p)


@dataclass(slots=True)
class QueryResult:
    mode: str
    status: str                 # ok / not_found / clarify / no_source / invalid_intent
    message: str
    subject_name: Optional[str] = None
    candidates: List[Candidate] = field(default_factory=list)
    also_named: List[Dict[str, Any]] = field(default_factory=list)
    available_levels: List[int] = field(default_factory=list)
    available_groups: List[str] = field(default_factory=list)
    available_fields: List[str] = field(default_factory=list)
    field_notes: List[Dict[str, str]] = field(default_factory=list)
    block_evidence: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    media_policy: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "status": self.status,
            "message": self.message,
            "subject_name": self.subject_name,
            "candidates": [
                {
                    "kind": c.kind,
                    "content_id": c.content_id,
                    "title": c.title,
                    "entity_id": c.entity_id,
                    "entity_name": c.entity_name,
                    "entity_kind": c.entity_kind,
                    "field_key": c.field_key,
                    "qualifier": c.qualifier,
                    "ordinal": c.ordinal,
                    "value": c.value,
                    "source_path": c.source_path,
                    "group_label": c.group_label,
                    "role": c.role,
                    "role_status": c.role_status,
                    "role_evidence": c.role_evidence,
                    "media_kind": c.media_kind,
                    "url": c.url,
                    "side": c.side,
                    "equivalent_text": c.equivalent_text,
                    "heading": c.heading,
                    "citation": c.citation(),
                }
                for c in self.candidates
            ],
            "also_named": self.also_named,
            "available_levels": self.available_levels,
            "available_groups": self.available_groups,
            "available_fields": self.available_fields,
            "field_notes": self.field_notes,
            "block_evidence": self.block_evidence,
            "rejected": self.rejected,
            "media_policy": self.media_policy,
            "stats": self.stats,
        }


def norm_key(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


_POINTER_TOKEN_RE = re.compile(r"(\d+)")


def natural_key(pointer: str) -> tuple:
    """指针按段自然排序：`…/content/10` 排在 `…/content/2` 之后。

    纯字符串排序会把 0,1,10…17,2…9 打乱；这里只解析数字段，用于同一篇文章内的
    位置顺序，不改变跨文章的分组。
    """
    parts = []
    for token in _POINTER_TOKEN_RE.split(pointer or ""):
        if not token:
            continue
        parts.append((0, int(token), "") if token.isdigit() else (1, 0, token))
    return tuple(parts)


def resolve_field(raw: str) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    return FIELD_SYNONYMS.get(text) or FIELD_SYNONYMS.get(norm_key(text))


# ---------------------------------------------------------------- 实体识别


def load_alias_index(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    """`alias_norm → [实体信息]`。同名多实体全部保留，不按板块顺序挑第一条。"""
    index: Dict[str, List[Dict[str, Any]]] = {}
    rows = conn.execute(
        """
        SELECT a.alias_norm, a.origin, e.entity_id, e.kind, e.canonical_name, e.content_id
        FROM alias a JOIN entity e ON e.entity_id = a.entity_id
        ORDER BY length(a.alias_norm) DESC, e.entity_id
        """
    )
    for row in rows:
        index.setdefault(row["alias_norm"], []).append(
            {
                "entity_id": row["entity_id"],
                "kind": row["kind"],
                "canonical_name": row["canonical_name"],
                "content_id": row["content_id"],
                "origin": row["origin"],
            }
        )
    return index


def find_entities(
    index: Dict[str, List[Dict[str, Any]]], text: str, *, kind: Optional[str] = None
) -> List[Dict[str, Any]]:
    """最长匹配扫描：先试整串，再逐步截短；命中即停（长名优先）。

    同一名称指向多个实体时**返回全部候选**，`kind` 只作过滤，不作排序偏好。
    整串与所有子串都没命中时，再做一次「别名以查询串开头」的匹配——这样「魔王」
    能落到「魔王(女)」「魔王(男)」两条候选上，而不是报「未收录」再猜一个。
    """
    query = norm_key(text)
    if not query:
        return []
    for length in range(len(query), 0, -1):
        candidates: List[Dict[str, Any]] = []
        for start in range(0, len(query) - length + 1):
            probe = query[start:start + length]
            for hit in index.get(probe, []):
                if kind and hit["kind"] != kind:
                    continue
                if hit not in candidates:
                    candidates.append(hit)
        if candidates:
            return candidates
    if len(query) >= 2:
        candidates = []
        for alias_norm, hits in index.items():
            if not alias_norm.startswith(query) or alias_norm == query:
                continue
            for hit in hits:
                if kind and hit["kind"] != kind:
                    continue
                if hit not in candidates:
                    candidates.append(hit)
        if candidates:
            return candidates
    return []


# ---------------------------------------------------------------- 查询入口


def run_query(conn: sqlite3.Connection, intent: Dict[str, Any]) -> QueryResult:
    if not isinstance(intent, dict):
        return QueryResult("unknown", "invalid_intent", "intent 必须是对象")
    mode = (intent.get("mode") or "").strip()
    if mode not in ("field", "article", "media", "catalog"):
        return QueryResult(
            mode or "unknown",
            "invalid_intent",
            f"mode 必须是 field/article/media/catalog，收到 {mode!r}",
        )

    subject = intent.get("subject") or {}
    name = ""
    kind = None
    if isinstance(subject, dict):
        name = (subject.get("name") or "").strip()
        kind = (subject.get("kind") or "").strip() or None
    if not name:
        kind = (intent.get("kind") or "").strip() or kind

    if kind == "term" and name:
        # 术语表：`popover-comment` 的 children 是原文、data 是注释，方向不能反
        return _query_term(conn, name)

    if not name:
        # 无名称：article 用原文字面条件；field 用同条条件（对象/属性/槽位/条件）检索；
        # 两者都不允许「猜一个主体」再答。
        if mode == "article":
            return _query_article_by_text(conn, intent, kind)
        conditions = intent.get("conditions") or {}
        if mode == "field" and isinstance(conditions, dict) and conditions:
            return _query_field_by_conditions(conn, intent, kind)
        return QueryResult(
            mode,
            "invalid_intent",
            "intent.subject.name 为空；article 需要原文/条件检索，field 需要 conditions",
        )

    index = load_alias_index(conn)
    hits = find_entities(index, name, kind=kind)
    if not hits and kind:
        # 名称本身有命中、只是种类不同：不能静默返回"未收录"，要把真实种类列出来，
        # 让上层决定是否澄清（例如「琥珀」既有旅人图鉴页也有设计稿页）。
        loose = find_entities(index, name)
        if loose:
            result = QueryResult(
                mode,
                "clarify",
                f"「{name}」有 {len(loose)} 个同名条目，但没有 kind={kind} 的；"
                "请按下列真实种类收窄，或直接指定要到哪一条",
                subject_name=name,
            )
            result.also_named = _multi_entity_note(loose, conn)
            return result
    if not hits:
        result = QueryResult(
            mode,
            "not_found",
            f"资料库未收录「{name}」" + (f"（限定 kind={kind}）" if kind else ""),
            subject_name=name,
        )
        titles = _title_candidates(conn, name, kind, 10)
        if titles:
            result.also_named = [
                {
                    "entity_id": None,
                    "kind": t.entity_kind,
                    "canonical_name": t.title,
                    "content_id": t.content_id,
                    "title": t.title,
                    "category": None,
                    "availability": None,
                }
                for t in titles
            ]
            result.message += f"；标题里出现过 {len(titles)} 次，见 also_named"
        return result

    if mode == "field":
        return _query_field(conn, intent, hits, name)
    if mode == "article":
        # 只给了主体、没给正文/标题线索：按「列出该主体的资料条目」处理（多候选全保留）
        if not (intent.get("query") or intent.get("query_text") or intent.get("text")):
            return _query_subject_catalog(conn, intent, hits, name)
        return _query_article(conn, intent, hits, name)
    if mode == "media":
        return _query_media(conn, intent, hits, name)
    return _query_catalog(conn, intent, hits, name)


def _article_row(conn: sqlite3.Connection, content_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT content_id,title,title_alt,availability,entity_kind,entity_name,category"
        " FROM article WHERE content_id=?",
        (content_id,),
    ).fetchone()


def _multi_entity_note(
    hits: Sequence[Dict[str, Any]], conn: sqlite3.Connection
) -> List[Dict[str, Any]]:
    """同名多实体时列出全部候选（含可用性与来源），供上层决定是否澄清。

    只列**不同实体**；同一实体的多个别名命中不算多候选。
    """
    out = []
    seen_entities: set = set()
    for hit in hits:
        if hit["entity_id"] in seen_entities:
            continue
        seen_entities.add(hit["entity_id"])
        row = _article_row(conn, hit["content_id"])
        out.append(
            {
                "entity_id": hit["entity_id"],
                "kind": hit["kind"],
                "canonical_name": hit["canonical_name"],
                "content_id": hit["content_id"],
                "title": (row["title"] or row["title_alt"]) if row else None,
                "title_alt_source": None,
                "availability": row["availability"] if row else None,
                "category": row["category"] if row else None,
            }
        )
    return out


# ---------------------------------------------------------------- mode=field


def _parse_levels(raw_levels: Any) -> Tuple[List[int], Optional[str]]:
    if raw_levels in (None, "", []):
        return [], None
    if isinstance(raw_levels, (int, str)):
        raw_levels = [raw_levels]
    out: List[int] = []
    for raw in raw_levels:
        text = str(raw).strip().lower()
        if text.startswith("lv"):
            text = text[2:]
        try:
            out.append(int(text))
        except ValueError:
            return [], f"levels 含不可解析值 {raw!r}"
    return out, None


def _page_levels(conn: sqlite3.Connection, entity_id: int) -> List[int]:
    rows = conn.execute(
        "SELECT DISTINCT qualifier FROM fact WHERE entity_id=? AND qualifier LIKE 'level:%'",
        (entity_id,),
    )
    out = []
    for row in rows:
        try:
            out.append(int(row["qualifier"].split(":", 1)[1]))
        except (ValueError, IndexError):
            continue
    return sorted(out)


def _query_field(
    conn: sqlite3.Connection,
    intent: Dict[str, Any],
    hits: Sequence[Dict[str, Any]],
    name: str,
) -> QueryResult:
    raw_fields = intent.get("fields") or []
    if isinstance(raw_fields, str):
        raw_fields = [raw_fields]
    fields: List[str] = []
    unknown: List[str] = []
    for raw in raw_fields:
        key = resolve_field(raw)
        if key:
            if key not in fields:
                fields.append(key)
        else:
            unknown.append(str(raw))
    if not fields:
        return QueryResult(
            "field",
            "clarify",
            "没有可识别的字段" + (f"：{unknown}" if unknown else "；intent.fields 为空"),
            subject_name=name,
            available_fields=sorted(set(FIELD_SYNONYMS.values())),
        )

    wanted_levels, level_error = _parse_levels(intent.get("levels"))
    if level_error:
        return QueryResult("field", "invalid_intent", level_error, subject_name=name)

    result = QueryResult("field", "ok", "", subject_name=name)
    result.also_named = _multi_entity_note(hits, conn) if len(hits) > 1 else []

    # 无数据源字段：只标记这一项，其它字段照常返回（不吞掉可查项）
    actionable: List[str] = []
    for f in fields:
        if f in NO_SOURCE_FIELDS:
            result.field_notes.append({"field_key": f, "status": "no_source", "note": NO_SOURCE_FIELDS[f]})
        else:
            actionable.append(f)

    needs_level = any(f.startswith("stat.") for f in actionable)
    if needs_level and not wanted_levels:
        available: List[int] = []
        for hit in hits:
            for level in _page_levels(conn, hit["entity_id"]):
                if level not in available:
                    available.append(level)
        available.sort()
        result.available_levels = available
        if actionable == [f for f in actionable if f.startswith("stat.")]:
            result.status = "clarify"
            result.message = (
                "面板字段必须绑定实际存在的等级；"
                + (f"可用等级：{available}" if available else "该页没有等级面板数据")
            )
            return result
        result.field_notes.append(
            {
                "field_key": ",".join(f for f in actionable if f.startswith("stat.")),
                "status": "clarify",
                "note": f"面板字段缺 levels，已跳过；可用等级：{available}",
            }
        )
        actionable = [f for f in actionable if not f.startswith("stat.")]

    for hit in hits:
        for field_key in actionable:
            if field_key.startswith("stat."):
                _collect_stat_facts(conn, result, hit, field_key, wanted_levels)
            else:
                _collect_plain_facts(conn, result, hit, field_key)

    if not result.candidates:
        absent = [n["note"] for n in result.field_notes if n["status"] == "field_absent_at_level"]
        invalid_levels = [
            r["level"] for r in result.rejected if r.get("reason") == "level_not_in_page"
        ]
        if result.field_notes and all(n["status"] == "no_source" for n in result.field_notes):
            result.status = "no_source"
            result.message = "；".join(n["note"] for n in result.field_notes)
        elif result.status == "ok":
            result.status = "not_found"
            if absent:
                result.message = "；".join(absent)
                if invalid_levels:
                    result.message += (
                        f"；另有等级 {sorted(set(invalid_levels))} 不在本页等级表内"
                        f"（可用 {result.available_levels}）"
                    )
            elif any(f.startswith("stat.") for f in actionable) and result.available_levels:
                result.message = f"该等级不在本页等级表内；可用等级：{result.available_levels}"
            else:
                result.message = f"「{name}」在库中没有这些字段的已核实值：{actionable}"
    else:
        notes = [
            n["note"]
            for n in result.field_notes
            if n["status"] in ("no_source", "field_absent_at_level")
        ]
        if notes:
            result.message = "；".join(notes) + "（其余字段已给出）"
    # 无论查到没查到，都把「这页有哪些结构块、各自指针」带上：缺字段时这是
    # 「原页有什么/没有什么」的证据，而不是一句「没有」。
    _attach_block_evidence(conn, result, hits)
    return result


def _attach_block_evidence(
    conn: sqlite3.Connection, result: QueryResult, hits: Sequence[Dict[str, Any]]
) -> None:
    """把候选文章的块位置写进 `field_notes`，供「原页只有图示/只有所属」这类答复取证。"""
    for hit in hits:
        row = conn.execute(
            "SELECT blocks_json,content_id FROM article WHERE content_id=?",
            (hit["content_id"],),
        ).fetchone()
        if row is None or not row["blocks_json"]:
            continue
        try:
            blocks = json.loads(row["blocks_json"])
        except (json.JSONDecodeError, TypeError):
            # 坏 JSON / 非字符串文本块列：只跳过这条证据；未知编码异常不得被吞成
            # 「没有块证据」（block_evidence 恒空会让「原页只有图示」的答复取证失效）
            continue
        result.field_notes.append(
            {
                "field_key": "blocks",
                "status": "info",
                "note": json.dumps(blocks, ensure_ascii=False),
            }
        )
        result.block_evidence.append({"content_id": row["content_id"], "blocks": blocks})


def _collect_plain_facts(
    conn: sqlite3.Connection,
    result: QueryResult,
    hit: Dict[str, Any],
    field_key: str,
) -> None:
    rows = conn.execute(
        "SELECT fact.field_key,fact.qualifier,fact.ordinal,fact.value_text,fact.source_path,"
        "fact.extra_json,article.title,article.title_alt,article.content_id"
        " FROM fact JOIN article ON article.content_id=fact.content_id"
        " WHERE fact.entity_id=? AND fact.field_key=? ORDER BY fact.ordinal",
        (hit["entity_id"], field_key),
    ).fetchall()
    for row in rows:
        result.candidates.append(
            Candidate(
                kind="fact",
                content_id=row["content_id"],
                title=row["title"] or row["title_alt"] or "",
                entity_id=hit["entity_id"],
                entity_name=hit["canonical_name"],
                entity_kind=hit["kind"],
                field_key=row["field_key"],
                qualifier=row["qualifier"],
                ordinal=row["ordinal"],
                value=row["value_text"],
                source_path=row["source_path"],
            )
        )


def _collect_stat_facts(
    conn: sqlite3.Connection,
    result: QueryResult,
    hit: Dict[str, Any],
    field_key: str,
    wanted_levels: Sequence[int],
) -> None:
    """等级面板：先查该页真实等级集合，非法等级列出可用值，不杜撰、不改查别的指标。"""
    page_levels = _page_levels(conn, hit["entity_id"])
    if not page_levels:
        return
    for level in page_levels:
        if level not in result.available_levels:
            result.available_levels.append(level)
    result.available_levels.sort()

    targets = list(wanted_levels) or [page_levels[-1]]
    for level in targets:
        if level not in page_levels:
            result.rejected.append(
                {
                    "reason": "level_not_in_page",
                    "level": level,
                    "available_levels": page_levels,
                    "entity_id": hit["entity_id"],
                }
            )
            continue
        found = 0
        for row in conn.execute(
            "SELECT fact.field_key,fact.qualifier,fact.ordinal,fact.value_text,fact.source_path,"
            "article.title,article.title_alt,article.content_id"
            " FROM fact JOIN article ON article.content_id=fact.content_id"
            " WHERE fact.entity_id=? AND fact.field_key=? AND fact.qualifier=?"
            " ORDER BY fact.ordinal",
            (hit["entity_id"], field_key, f"level:{level}"),
        ):
            found += 1
            result.candidates.append(
                Candidate(
                    kind="fact",
                    content_id=row["content_id"],
                    title=row["title"] or row["title_alt"] or "",
                    entity_id=hit["entity_id"],
                    entity_name=hit["canonical_name"],
                    entity_kind=hit["kind"],
                    field_key=row["field_key"],
                    qualifier=row["qualifier"],
                    ordinal=row["ordinal"],
                    value=row["value_text"],
                    source_path=row["source_path"],
                )
            )
        if found:
            continue
        # 等级真实存在，只是**这一等级没有这项指标**：和「等级不在表内」分开记，
        # 否则会把有效等级说成无效（708439 level=1 只有攻击/生命，没有水系伤害）。
        note = (
            f"等级 {level} 在等级表中有效，但该等级没有 {field_key} 这一项数据"
            "（不是等级无效）"
        )
        result.rejected.append(
            {
                "reason": "field_absent_at_level",
                "field_key": field_key,
                "level": level,
                "entity_id": hit["entity_id"],
                "available_levels": page_levels,
            }
        )
        if not any(
            n["status"] == "field_absent_at_level" and n.get("field_key") == field_key
            for n in result.field_notes
        ):
            result.field_notes.append(
                {"field_key": field_key, "status": "field_absent_at_level", "note": note}
            )


# ---------------------------------------------------------------- mode=article


def _article_candidate(
    row: sqlite3.Row, entity_name: Optional[str], entity_kind: Optional[str]
) -> Candidate:
    return Candidate(
        kind="chunk",
        content_id=row["content_id"],
        title=row["title"] or row["title_alt"] or "",
        entity_id=row["entity_id"] if "entity_id" in row.keys() else None,
        entity_name=entity_name,
        entity_kind=entity_kind,
        field_key=row["field_key"] if "field_key" in row.keys() else None,
        value=row["body"] if "body" in row.keys() else "",
        source_path=row["source_path"],
        heading=row["heading"] if "heading" in row.keys() else "",
    )


def _query_term(conn: sqlite3.Connection, name: str) -> QueryResult:
    """术语查询：返回原文 + 注释 + 翻译，方向固定（显示=原文，含义=注释）。"""
    key = norm_key(name)
    rows = conn.execute(
        "SELECT t.lookup_norm,t.lang,t.display_term,t.meaning_text,t.translation_text,"
        "t.content_id,t.source_path,article.title,article.title_alt"
        " FROM term t JOIN article ON article.content_id=t.content_id"
        " WHERE t.lookup_norm=? OR instr(t.lookup_norm, ?) > 0"
        " ORDER BY t.content_id LIMIT 50",
        (key, key),
    ).fetchall()
    result = QueryResult("article", "ok" if rows else "not_found", "")
    if not rows:
        result.message = f"术语表未收录「{name}」"
        return result
    for row in rows:
        result.candidates.append(
            Candidate(
                kind="chunk",
                content_id=row["content_id"],
                title=row["title"] or row["title_alt"] or "",
                entity_id=None,
                entity_name=row["display_term"],
                entity_kind="term",
                field_key=f"term.{row['lang']}",
                value=row["display_term"],
                source_path=row["source_path"],
                heading=row["meaning_text"],
            )
        )
    result.stats = {"terms": len(rows)}
    return result


_SLOT_FIELDS = {
    "主位": "sigil.main_effect",
    "主位效果": "sigil.main_effect",
    "主": "sigil.main_effect",
    "辅位": "sigil.sub_effect",
    "辅位效果": "sigil.sub_effect",
    "辅": "sigil.sub_effect",
}
# 槽位是**结构维度**（走 field_key），不是效果正文里的字；当成字去匹配必然全不中。
_SLOT_KEYS = {"slot", "position", "栏位", "槽位", "位"}


def _split_slot_condition(
    conditions: Dict[str, str], fields: List[str]
) -> Tuple[Dict[str, str], Optional[str], Optional[str]]:
    """拆出「槽位」条件：返回（其余文字条件, 槽位原文, 命中不到字段的说明）。"""
    slot_value = None
    textual: Dict[str, str] = {}
    for key, value in conditions.items():
        if norm_key(key) in _SLOT_KEYS:
            slot_value = value
        else:
            textual[key] = value
    if not slot_value:
        return textual, None, None
    mapped = _SLOT_FIELDS.get(slot_value.strip()) or _SLOT_FIELDS.get(norm_key(slot_value).strip())
    if mapped is None:
        # 词表外槽位：退回当普通文字条件，宁可零命中也不猜
        textual["slot"] = slot_value
        return textual, None, None
    if fields and mapped not in fields:
        return textual, mapped, f"槽位「{slot_value}」对应 {mapped}，但请求的字段是 {fields}"
    return textual, mapped, None


def _query_field_by_conditions(
    conn: sqlite3.Connection, intent: Dict[str, Any], kind: Optional[str]
) -> QueryResult:
    """描述题（无主体名）：「那个加水系伤害、获得护盾时触发的主位效果」。

    判定要求**同一条效果内**同时满足所有条件：候选先取该 kind 下该 field_key 的
    `fact`，再逐条检查条件词是否都出现在**这一条** `value_text` 里。不做跨条词袋。
    槽位（主位/辅位）是结构维度，用来收窄 field_key，不当正文字串匹配。
    """
    raw_fields = intent.get("fields") or []
    if isinstance(raw_fields, str):
        raw_fields = [raw_fields]
    fields = [f for f in (resolve_field(x) for x in raw_fields) if f]
    if not fields:
        return QueryResult("field", "clarify", "描述题需要 intent.fields（要查哪类字段）")
    conditions = {k: str(v) for k, v in (intent.get("conditions") or {}).items() if v}
    conditions, slot_field, slot_conflict = _split_slot_condition(conditions, fields)
    if slot_conflict:
        result = QueryResult("field", "not_found", slot_conflict)
        result.stats = {"scanned_facts": 0, "matched": 0, "conditions": conditions}
        return result
    if slot_field:
        fields = [slot_field]

    where = ["fact.field_key IN (%s)" % ",".join("?" for _ in fields)]
    args: List[Any] = list(fields)
    if kind:
        where.append("article.entity_kind = ?")
        args.append(kind)
    rows = conn.execute(
        "SELECT fact.entity_id,fact.field_key,fact.qualifier,fact.ordinal,fact.value_text,"
        "fact.source_path,fact.content_id,article.title,article.title_alt,article.entity_kind,"
        "article.entity_name FROM fact JOIN article ON article.content_id=fact.content_id"
        " WHERE " + " AND ".join(where) + " ORDER BY fact.content_id,fact.field_key,fact.ordinal",
        args,
    ).fetchall()

    result = QueryResult("field", "ok", "")
    matched = 0
    for row in rows:
        text = row["value_text"]
        missing = [k for k, v in conditions.items() if v not in text]
        if missing:
            result.rejected.append(
                {
                    "reason": "condition_not_in_same_entry",
                    "missing": missing,
                    "content_id": row["content_id"],
                    "field_key": row["field_key"],
                    "source_path": row["source_path"],
                    "value": text[:80],
                }
            )
            continue
        matched += 1
        result.candidates.append(
            Candidate(
                kind="fact",
                content_id=row["content_id"],
                title=row["title"] or row["title_alt"] or "",
                entity_id=row["entity_id"],
                entity_name=row["entity_name"],
                entity_kind=row["entity_kind"],
                field_key=row["field_key"],
                qualifier=row["qualifier"],
                ordinal=row["ordinal"],
                value=text,
                source_path=row["source_path"],
            )
        )
    result.stats = {"scanned_facts": len(rows), "matched": matched, "conditions": conditions}
    if not matched:
        result.status = "not_found"
        result.message = (
            f"没有单条效果能同时满足这些条件：{conditions}（初筛 {len(rows)} 条，"
            "均因条件不同条被剔除）"
        )
    return result


def _query_subject_catalog(
    conn: sqlite3.Connection,
    intent: Dict[str, Any],
    hits: Sequence[Dict[str, Any]],
    name: str,
) -> QueryResult:
    """`mode=article` 但只有主体名：列出该名字下的全部条目（每个候选一条，附来源）。

    同名多条目（旅人档案 / 对话小节 / 立绘设计稿）必须**全部保留**，不按板块顺序挑一条。
    """
    result = QueryResult("article", "ok", "", subject_name=name)
    result.also_named = _multi_entity_note(hits, conn) if len(hits) > 1 else []
    seen: set = set()
    for hit in hits:
        if hit["content_id"] in seen:
            continue
        seen.add(hit["content_id"])
        row = conn.execute(
            "SELECT a.content_id,a.title,a.title_alt,a.entity_kind,a.entity_name,a.category,"
            "a.availability,a.word_count,a.schema,"
            "(SELECT COUNT(*) FROM media_ref m WHERE m.content_id=a.content_id) AS media_total,"
            "(SELECT COUNT(*) FROM chunk c WHERE c.content_id=a.content_id) AS chunk_total"
            " FROM article a WHERE a.content_id=?",
            (hit["content_id"],),
        ).fetchone()
        if row is None:
            continue
        result.candidates.append(
            Candidate(
                kind="chunk",
                content_id=row["content_id"],
                title=row["title"] or row["title_alt"] or "",
                entity_id=hit["entity_id"],
                entity_name=row["entity_name"],
                entity_kind=row["entity_kind"],
                field_key="article.catalog",
                value=(
                    f"kind={row['entity_kind']} category={row['category']} "
                    f"schema={row['schema']} word_count={row['word_count']} "
                    f"media={row['media_total']} chunks={row['chunk_total']} "
                    f"availability={row['availability']}"
                ),
                source_path="/",
                heading="(条目概览，不是正文)",
            )
        )
    if not result.candidates:
        result.status = "not_found"
        result.message = f"「{name}」没有可列出的条目"
    return result


def _query_article(
    conn: sqlite3.Connection,
    intent: Dict[str, Any],
    hits: Sequence[Dict[str, Any]],
    name: str,
) -> QueryResult:
    needle = (
        intent.get("query") or intent.get("query_text") or intent.get("text") or ""
    ).strip()
    result = QueryResult("article", "ok", "", subject_name=name)
    result.also_named = _multi_entity_note(hits, conn) if len(hits) > 1 else []
    if not needle:
        result.status = "clarify"
        result.message = "mode=article 需要 intent.query 文本"
        return result

    literal = (intent.get("literal") or "").strip() or None
    tokens = [t for t in needle.split() if t]
    limit = int(intent.get("limit") or DEFAULT_FTS_LIMIT)
    content_ids = [h["content_id"] for h in hits]
    placeholders = ",".join("?" for _ in content_ids)
    sql = (
        "SELECT c.chunk_id,c.content_id,c.source_path,c.heading,c.body,c.block_kind,"
        "article.title,article.title_alt"
        " FROM chunk_fts JOIN chunk c ON c.chunk_id=chunk_fts.rowid"
        " JOIN article ON article.content_id=c.content_id"
        f" WHERE c.content_id IN ({placeholders}) AND chunk_fts MATCH ?"
        " ORDER BY c.chunk_id"
    )
    fts_rows = conn.execute(sql, list(content_ids) + [fts_query(needle)]).fetchall()
    _finalize_article_result(result, fts_rows, literal, limit, tokens=tokens)
    if result.status == "not_found" and not result.rejected:
        # FTS 短语在「同实体、零命中」时补一次原文字面子串兜底：
        # 若库里确实存在该字面串而 FTS 没给出，说明是 token 化差异，不能报成「没有」。
        probe = literal or needle
        rows = conn.execute(
            "SELECT c.chunk_id,c.content_id,c.source_path,c.heading,c.body,c.block_kind,"
            "article.title,article.title_alt"
            " FROM chunk c JOIN article ON article.content_id=c.content_id"
            f" WHERE c.content_id IN ({placeholders}) AND instr(c.body, ?) > 0"
            " ORDER BY c.chunk_id",
            list(content_ids) + [probe],
        ).fetchall()
        if rows:
            _finalize_article_result(result, rows, literal, limit, tokens=tokens)
            result.stats["fts_fallback"] = "instr_substring"
            result.message = (
                "FTS 短语未命中，改用原文子串复核命中"
                f"（{len(rows)} 条）；来源仍是库内原文"
            )
    if result.status == "not_found":
        titles = _title_candidates(conn, needle, None, limit)
        if titles:
            result.candidates = titles
            result.status = "ok"
            result.stats["title_hits"] = len(titles)
            result.message = (
                f"正文没有命中，命中 {len(titles)} 个标题；"
                "标题命中不等于正文回答，需要正文请给更具体的原文片段"
            )
    return result


def _query_article_by_text(
    conn: sqlite3.Connection, intent: Dict[str, Any], kind: Optional[str]
) -> QueryResult:
    """无名称的描述题：用原文子串在全库同条检索。

    `literal` 必填——它是**原文里真实出现的子串**，用于 `instr(body, ?)` 复核；
    `query` 只是 FTS 短语。可选 `kind`（sigil/traveler/monster/...）与
    `field_key` 收窄范围，不收窄就全库找，并把每个候选的来源与归属一并给出。
    """
    literal = (intent.get("literal") or intent.get("query_text") or "").strip() or None
    needle = (intent.get("query") or intent.get("query_text") or literal or "").strip()
    result = QueryResult("article", "ok", "")
    if not needle:
        result.status = "clarify"
        result.message = "无名称的描述检索需要 literal（原文子串）或 query"
        return result

    tokens = [t for t in needle.split() if t]
    limit = int(intent.get("limit") or DEFAULT_FTS_LIMIT)
    where = ["chunk_fts MATCH ?"]
    args: List[Any] = [fts_query(needle)]
    if kind and kind not in ("term",):
        where.append("article.entity_kind = ?")
        args.append(kind)
    if intent.get("field_key"):
        where.append("c.block_kind = ?")
        args.append(str(intent["field_key"]))
    sql = (
        "SELECT c.chunk_id,c.content_id,c.source_path,c.heading,c.body,c.block_kind,"
        "article.title,article.title_alt,article.entity_kind,article.entity_name"
        " FROM chunk_fts JOIN chunk c ON c.chunk_id=chunk_fts.rowid"
        " JOIN article ON article.content_id=c.content_id"
        " WHERE " + " AND ".join(where) +
        " ORDER BY c.content_id,c.chunk_id"
    )
    fts_rows = conn.execute(sql, args).fetchall()
    _finalize_article_result(
        result, fts_rows, literal, limit, keep_entity=True, tokens=tokens,
    )
    if result.status == "not_found":
        titles = _title_candidates(conn, needle, kind, limit)
        if titles:
            result.candidates = titles
            result.status = "ok"
            result.stats["title_hits"] = len(titles)
            result.message = (
                f"正文没有命中，命中 {len(titles)} 个标题；"
                "标题命中不等于正文回答，需要正文请给更具体的原文片段"
            )
    return result


def _verify_body(body: str, literal: Optional[str], tokens: Sequence[str]) -> bool:
    """原文复核。

    - 给了 `literal`：要求它是 `body` 的真实子串（跨标点假命中在这里被剔除）；
    - 没给：要求**每个**查询 token 都出现在 `body` 里（多词查询是"都要有"，不是"连成一串"）。
    """
    if literal:
        return literal in body
    return all(token in body for token in tokens)


def _finalize_article_result(
    result: QueryResult,
    fts_rows: Sequence[sqlite3.Row],
    literal: Optional[str],
    limit: int,
    *,
    keep_entity: bool = False,
    tokens: Sequence[str] = (),
) -> None:
    """**先复核再截断**：假命中先被剔除，截断只作用在已复核通过的候选上。"""
    verified: List[Candidate] = []
    title_hits: List[Candidate] = []
    for row in fts_rows:
        body = row["body"]
        if not _verify_body(body, literal, tokens):
            result.rejected.append(
                {
                    "reason": "body_substring_mismatch",
                    "content_id": row["content_id"],
                    "source_path": row["source_path"],
                    "body": body[:80],
                }
            )
            continue
        entity_name = row["entity_name"] if keep_entity else None
        entity_kind = row["entity_kind"] if keep_entity else None
        verified.append(_article_candidate(row, entity_name, entity_kind))
    result.stats = {
        "fts_hits": len(fts_rows),
        "verified": len(verified),
        "rejected": len(result.rejected),
        "limit": limit,
        "truncated": max(0, len(verified) - limit),
        "distinct_articles": len({c.content_id for c in verified}),
    }
    result.candidates = verified[:limit]
    if not verified:
        result.status = "not_found"
        probe = literal or " ".join(tokens)
        result.message = f"FTS 初筛 {len(fts_rows)} 条，经原文复核（{probe!r}）后无合格候选"
    elif len(verified) > limit:
        result.message = f"合格候选 {len(verified)} 条，按 limit={limit} 截断，剩余见 stats.truncated"


def _title_candidates(
    conn: sqlite3.Connection, needle: str, kind: Optional[str], limit: int
) -> List[Candidate]:
    """标题命中：`mode=article` 要能"找到这篇文章"。

    标题命中**不冒充正文回答**——候选的 `field_key` 写 `article.title`，调用方据此区分
    「这是标题匹配」与「正文里真提到了」。
    """
    if not needle:
        return []
    where = ["(instr(article.title, ?) > 0 OR instr(IFNULL(article.title_alt,''), ?) > 0)"]
    args: List[Any] = [needle, needle]
    if kind and kind != "term":
        where.append("article.entity_kind = ?")
        args.append(kind)
    sql = (
        "SELECT article.content_id,article.title,article.title_alt,article.entity_kind,"
        "article.entity_name,article.availability FROM article WHERE "
        + " AND ".join(where)
        + " ORDER BY article.content_id LIMIT ?"
    )
    args.append(limit)
    out: List[Candidate] = []
    for row in conn.execute(sql, args):
        title = row["title"] or row["title_alt"] or ""
        out.append(
            Candidate(
                kind="chunk",
                content_id=row["content_id"],
                title=title,
                entity_id=None,
                entity_name=row["entity_name"],
                entity_kind=row["entity_kind"],
                field_key="article.title",
                value=title,
                source_path="/",
                heading="(标题命中，不是正文)",
            )
        )
    return out


# ---------------------------------------------------------------- mode=media


def _query_media(
    conn: sqlite3.Connection,
    intent: Dict[str, Any],
    hits: Sequence[Dict[str, Any]],
    name: str,
) -> QueryResult:
    result = QueryResult("media", "ok", "", subject_name=name)
    result.also_named = _multi_entity_note(hits, conn) if len(hits) > 1 else []
    groups = intent.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    wanted_groups = [str(g) for g in groups]

    content_ids = [h["content_id"] for h in hits]
    placeholders = ",".join("?" for _ in content_ids)
    if wanted_groups:
        gph = ",".join("?" for _ in wanted_groups)
        rows = conn.execute(
            "SELECT m.*,article.title,article.title_alt FROM media_ref m"
            " JOIN article ON article.content_id=m.content_id"
            f" WHERE m.content_id IN ({placeholders}) AND m.group_label IN ({gph})",
            list(content_ids) + wanted_groups,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT m.*,article.title,article.title_alt FROM media_ref m"
            " JOIN article ON article.content_id=m.content_id"
            f" WHERE m.content_id IN ({placeholders})",
            list(content_ids),
        ).fetchall()

    # 真实节点顺序：指针里的下标按数字比较（10 排在 2 之后），不能按字符串排。
    rows = sorted(
        rows,
        key=lambda r: (r["content_id"], natural_key(r["source_path"]), r["ordinal"]),
    )

    result.available_groups = sorted(
        {
            r["group_label"]
            for r in conn.execute(
                "SELECT DISTINCT group_label FROM media_ref"
                f" WHERE content_id IN ({placeholders}) AND group_label IS NOT NULL",
                list(content_ids),
            )
        }
    )
    if wanted_groups and not rows:
        result.status = "invalid_intent"
        result.message = f"这些分组在该实体下不存在：{wanted_groups}"
        return result

    for row in rows:
        kind_ok = row["kind"] in SENDABLE_KINDS
        role_ok = row["role"] in SENDABLE_ROLES
        judged = row["role_status"] == "judged"
        if not (kind_ok and role_ok and judged):
            result.rejected.append(
                {
                    "reason": "kind_not_sendable" if not kind_ok else "role_not_sendable",
                    "content_id": row["content_id"],
                    "source_path": row["source_path"],
                    "media_kind": row["kind"],
                    "role": row["role"],
                    "role_status": row["role_status"],
                    "role_evidence": row["role_evidence"],
                }
            )
            continue
        result.candidates.append(
            Candidate(
                kind="media",
                content_id=row["content_id"],
                title=row["title"] or row["title_alt"] or "",
                entity_id=row["entity_id"],
                entity_name=name,
                entity_kind=None,
                source_path=row["source_path"],
                group_label=row["group_label"],
                role=row["role"],
                role_status=row["role_status"],
                role_evidence=row["role_evidence"],
                media_kind=row["kind"],
                url=row["url"],
                side=row["side"],
                equivalent_text=row["equivalent_text"],
            )
        )
    result.media_policy = {
        "sendable_kinds": list(SENDABLE_KINDS),
        "sendable_roles": list(SENDABLE_ROLES),
        "note": "kind 不是图片/图集，或 role 不在 sendable_roles、role_status!=judged 的媒体不进入"
        "发送候选（计划 4.4）；本模块只返回结构化结果，不发送任何消息。",
    }
    if not result.candidates:
        result.status = "not_found"
        result.message = "没有已判定用途的媒体（未判定条目需人工复核，不发送）"
    return result


# ---------------------------------------------------------------- mode=catalog


def _query_catalog(
    conn: sqlite3.Connection,
    intent: Dict[str, Any],
    hits: Sequence[Dict[str, Any]],
    name: str,
) -> QueryResult:
    """只列事实，不下载、不发送：字段清单、分组、等级、各类媒体数量。"""
    result = QueryResult("catalog", "ok", "", subject_name=name)
    result.also_named = _multi_entity_note(hits, conn) if len(hits) > 1 else []
    for hit in hits:
        entity_id = hit["entity_id"]
        for row in conn.execute(
            "SELECT DISTINCT field_key FROM fact WHERE entity_id=? ORDER BY field_key",
            (entity_id,),
        ):
            result.available_fields.append(row["field_key"])
        for row in conn.execute(
            "SELECT DISTINCT group_label FROM media_ref WHERE entity_id=? AND group_label IS NOT NULL",
            (entity_id,),
        ):
            result.available_groups.append(row["group_label"])
        for level in _page_levels(conn, entity_id):
            result.available_levels.append(level)
        for row in conn.execute(
            "SELECT COUNT(*) AS n,kind FROM media_ref WHERE entity_id=? GROUP BY kind", (entity_id,)
        ):
            result.candidates.append(
                Candidate(
                    kind="fact",
                    content_id=hit["content_id"],
                    title=hit["canonical_name"],
                    entity_id=entity_id,
                    entity_name=hit["canonical_name"],
                    entity_kind=hit["kind"],
                    field_key=f"media_count.{row['kind']}",
                    value=str(row["n"]),
                    source_path="/",
                )
            )
    result.available_fields = sorted(set(result.available_fields))
    result.available_groups = sorted(set(result.available_groups))
    result.available_levels = sorted(set(result.available_levels))
    if not result.candidates:
        result.status = "not_found"
        result.message = "该实体没有可列出的目录事实"
    return result
