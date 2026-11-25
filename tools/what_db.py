# -*- coding: utf-8 -*-
"""stellasora_what 离线资料库：SQLite + FTS5 建库与只读访问。

只用宿主 Python 标准库 `sqlite3`（实测宿主 Python 3.14.7 / SQLite 3.50.4，FTS5 可用）。

三条硬约束：

1. **分词对称**：写入 `chunk.seg` 与查询侧 `seg()` 是**同一个函数**（逐字切分 CJK，
   用 U+0001 包夹）。两侧不对称会让 FTS 命中率凭空掉一半。
2. **备库单写**：`PRAGMA journal_mode=DELETE`，避开 SQLite 3.50.4 的 WAL-reset 缺陷。
   建库结束即关闭连接，不长期持有。
3. **展示与复核一律从 `chunk.body` 取**，不从 `seg` 逆变换——`seg` 只服务索引。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
SEP = "\u0001"


def seg(text: str) -> str:
    """写入与查询共用的逐字切分。CJK 逐字包夹 U+0001，非 CJK 原样保留。"""
    return "".join(f"{SEP}{ch}{SEP}" if CJK_RE.match(ch) else ch for ch in text)


def fts_query(text: str) -> str:
    """把用户短语转成 FTS5 短语查询串，保证与 `seg()` 对称。"""
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    if not tokens:
        return ""
    parts = []
    for token in tokens:
        encoded = seg(token).replace('"', '""')
        parts.append(f'"{encoded}"')
    return " AND ".join(parts)


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode = DELETE;
PRAGMA user_version = 1;

CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE article(
  content_id       INTEGER PRIMARY KEY,
  title            TEXT NOT NULL,
  title_alt        TEXT,
  title_alt_source TEXT,
  summary          TEXT,
  list_entry_id    INTEGER,
  type             INTEGER,
  category         TEXT,
  tree_path        TEXT,
  list_updated_at   INTEGER,
  detail_updated_at INTEGER,
  word_count       INTEGER,
  image_num        INTEGER,
  availability     TEXT NOT NULL,
  schema           TEXT,
  source_url       TEXT NOT NULL,
  entity_kind      TEXT NOT NULL,
  entity_name      TEXT NOT NULL,
  entity_name_src  TEXT NOT NULL,
  stat_labels_json TEXT,
  notes_json       TEXT,
  blocks_json      TEXT,
  raw_sha256       TEXT NOT NULL,
  raw_gzip         BLOB
);
CREATE INDEX article_status ON article(availability);
CREATE INDEX article_category ON article(category);

CREATE TABLE entry(
  entry_id   INTEGER PRIMARY KEY,
  parent_id  INTEGER,
  title      TEXT NOT NULL,
  path       TEXT,
  content_id INTEGER REFERENCES article(content_id)
);
CREATE INDEX entry_article ON entry(content_id);

CREATE TABLE entity(
  entity_id      INTEGER PRIMARY KEY,
  source_key     TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  content_id     INTEGER NOT NULL REFERENCES article(content_id),
  source_path    TEXT NOT NULL
);

CREATE TABLE alias(
  alias_norm TEXT NOT NULL,
  entity_id  INTEGER NOT NULL REFERENCES entity(entity_id),
  origin     TEXT NOT NULL,
  PRIMARY KEY(alias_norm, entity_id)
);
CREATE INDEX alias_lookup ON alias(alias_norm);

CREATE TABLE fact(
  entity_id   INTEGER NOT NULL REFERENCES entity(entity_id),
  field_key   TEXT NOT NULL,
  qualifier   TEXT NOT NULL DEFAULT '',
  ordinal     INTEGER NOT NULL,
  content_id  INTEGER NOT NULL REFERENCES article(content_id),
  source_path TEXT NOT NULL,
  value_text  TEXT NOT NULL,
  extra_json  TEXT,
  PRIMARY KEY(entity_id, field_key, qualifier, ordinal)
);
CREATE INDEX fact_lookup ON fact(entity_id, field_key, qualifier);

CREATE TABLE media_ref(
  content_id    INTEGER NOT NULL REFERENCES article(content_id),
  source_path   TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,
  entity_id     INTEGER REFERENCES entity(entity_id),
  kind          TEXT NOT NULL,
  group_label   TEXT,
  locale        TEXT,
  label         TEXT,
  url           TEXT NOT NULL,
  role          TEXT NOT NULL,
  role_evidence TEXT NOT NULL,
  role_status   TEXT NOT NULL,
  node_path     TEXT,
  side          TEXT,
  equivalent_text TEXT,
  PRIMARY KEY(content_id, source_path, ordinal)
);
CREATE INDEX media_ref_role ON media_ref(content_id, role_status);
CREATE INDEX media_ref_group ON media_ref(content_id, group_label);

CREATE TABLE term(
  lookup_norm   TEXT NOT NULL,
  lang          TEXT NOT NULL,
  display_term  TEXT NOT NULL,
  meaning_text  TEXT NOT NULL,
  translation_text TEXT,
  content_id    INTEGER NOT NULL REFERENCES article(content_id),
  source_path   TEXT NOT NULL,
  PRIMARY KEY(lookup_norm, lang, content_id, source_path)
);

CREATE TABLE chunk(
  chunk_id   INTEGER PRIMARY KEY,
  content_id INTEGER NOT NULL REFERENCES article(content_id),
  source_path TEXT NOT NULL,
  scope      TEXT NOT NULL,
  heading    TEXT NOT NULL,
  block_kind TEXT NOT NULL,
  body       TEXT NOT NULL,
  seg        TEXT NOT NULL
);
CREATE INDEX chunk_scope ON chunk(scope, content_id);

CREATE VIRTUAL TABLE chunk_fts USING fts5(
  seg,
  content='chunk',
  content_rowid='chunk_id',
  tokenize='unicode61',
  detail=full
);

CREATE TRIGGER chunk_ai AFTER INSERT ON chunk BEGIN
  INSERT INTO chunk_fts(rowid, seg) VALUES (new.chunk_id, new.seg);
END;
"""


@dataclass(slots=True)
class BuildStats:
    articles: int = 0
    entities: int = 0
    aliases: int = 0
    facts: int = 0
    media: int = 0
    terms: int = 0
    chunks: int = 0
    entries: int = 0
    status_counts: Dict[str, int] = None
    schema_counts: Dict[str, int] = None
    media_kind_counts: Dict[str, int] = None
    media_role_counts: Dict[str, int] = None
    levels_seen: List[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "articles": self.articles,
            "entities": self.entities,
            "aliases": self.aliases,
            "facts": self.facts,
            "media": self.media,
            "terms": self.terms,
            "chunks": self.chunks,
            "entries": self.entries,
            "status_counts": self.status_counts or {},
            "schema_counts": self.schema_counts or {},
            "media_kind_counts": self.media_kind_counts or {},
            "media_role_counts": self.media_role_counts or {},
            "levels_seen": self.levels_seen or [],
        }


def _alias_norm(text: str) -> str:
    import unicodedata

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def open_db(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA trusted_schema = OFF")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


_WIKI_SECTIONS = {"旅人图鉴", "NPC", "秘纹", "怪物", "游戏鉴赏"}
_ARTICLE_SECTIONS = {"攻略区", "剧情区", "二创区"}


def _scope_for_category(category: Optional[str]) -> str:
    """DDL 枚举只有 wiki / announcement / article，**不虚构公告**。

    资料（结构化图鉴 + 媒体图集）→ wiki；正文型栏目（攻略/剧情/二创）→ article；
    目录缺失（category=None）按 wiki 页面处理——不拿标题或提示猜分区。
    """
    if category in _ARTICLE_SECTIONS:
        return "article"
    if category in _WIKI_SECTIONS:
        return "wiki"
    return "wiki"


def insert_articles(
    conn: sqlite3.Connection,
    articles: Sequence[Any],
    *,
    entries: Optional[Sequence[Tuple[int, Optional[int], str, Optional[str], Optional[int]]]] = None,
) -> BuildStats:
    """把 `ParsedArticle` 序列写入库。全部参数化，不拼 SQL 字面量。"""
    stats = BuildStats(
        status_counts={},
        schema_counts={},
        media_kind_counts={},
        media_role_counts={},
        levels_seen=[],
    )
    seen_levels: set = set()
    if entries:
        conn.executemany(
            "INSERT OR REPLACE INTO entry(entry_id,parent_id,title,path,content_id) VALUES(?,?,?,?,?)",
            entries,
        )
        stats.entries = len(entries)

    for art in articles:
        stats.articles += 1
        stats.status_counts[art.status] = stats.status_counts.get(art.status, 0) + 1
        if art.schema:
            stats.schema_counts[art.schema] = stats.schema_counts.get(art.schema, 0) + 1
        conn.execute(
            """
            INSERT INTO article(
              content_id,title,title_alt,title_alt_source,summary,list_entry_id,type,
              category,tree_path,list_updated_at,detail_updated_at,word_count,image_num,
              availability,schema,source_url,entity_kind,entity_name,entity_name_src,
              stat_labels_json,notes_json,blocks_json,raw_sha256,raw_gzip
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                art.content_id,
                art.title,
                art.title_alt,
                art.title_alt_source,
                art.summary,
                art.list_entry_id,
                art.type,
                art.category,
                art.tree_path,
                art.list_updated_at,
                art.detail_updated_at,
                art.word_count,
                art.image_num,
                art.status,
                art.schema,
                art.source_url,
                art.entity_kind,
                art.entity_name,
                art.entity_name_source,
                json.dumps(art.stat_labels, ensure_ascii=False),
                json.dumps(
                    [{"code": n.code, "path": n.source_path, "detail": n.detail} for n in art.notes],
                    ensure_ascii=False,
                ),
                json.dumps(
                    [
                        {
                            "block_type": b.block_type,
                            "title": b.title,
                            "content_id_path": b.content_id_path,
                            "spec_pointer": b.spec_pointer,
                        }
                        for b in art.blocks
                    ],
                    ensure_ascii=False,
                ),
                art.raw_sha256,
                art.raw_gzip,
            ),
        )

        # 实体：一篇文章一个主体实体（图鉴条目/攻略文章/NPC 对话各算一个）
        source_key = f"{art.entity_kind}:{art.content_id}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO entity(source_key,kind,canonical_name,content_id,source_path)"
            " VALUES(?,?,?,?,?)",
            (source_key, art.entity_kind, art.entity_name, art.content_id, "/"),
        )
        if cur.rowcount:
            entity_id = cur.lastrowid
            stats.entities += 1
        else:
            row = conn.execute(
                "SELECT entity_id FROM entity WHERE source_key=?", (source_key,)
            ).fetchone()
            entity_id = row["entity_id"]

        for name, origin in _alias_candidates(art):
            norm = _alias_norm(name)
            if not norm:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO alias(alias_norm,entity_id,origin) VALUES(?,?,?)",
                (norm, entity_id, origin),
            )
            stats.aliases += cur.rowcount or 0

        for fact in art.facts:
            conn.execute(
                "INSERT OR REPLACE INTO fact(entity_id,field_key,qualifier,ordinal,"
                "content_id,source_path,value_text,extra_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    fact.field_key,
                    fact.qualifier,
                    fact.ordinal,
                    art.content_id,
                    fact.source_path,
                    fact.value_text,
                    json.dumps(fact.extra, ensure_ascii=False) if fact.extra else None,
                ),
            )
            stats.facts += 1

        for media in art.media:
            conn.execute(
                "INSERT OR REPLACE INTO media_ref(content_id,source_path,ordinal,entity_id,"
                "kind,group_label,locale,label,url,role,role_evidence,role_status,node_path,"
                "side,equivalent_text)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    art.content_id,
                    media.source_path,
                    media.ordinal,
                    entity_id,
                    media.kind,
                    media.group_label,
                    media.locale,
                    media.label,
                    media.url,
                    media.role,
                    media.role_evidence,
                    media.role_status,
                    media.node_path,
                    media.side,
                    media.equivalent_text,
                ),
            )
            stats.media += 1
            stats.media_kind_counts[media.kind] = stats.media_kind_counts.get(media.kind, 0) + 1
            stats.media_role_counts[media.role] = stats.media_role_counts.get(media.role, 0) + 1

        for term in art.terms:
            conn.execute(
                "INSERT OR REPLACE INTO term(lookup_norm,lang,display_term,meaning_text,"
                "translation_text,content_id,source_path) VALUES(?,?,?,?,?,?,?)",
                (
                    _alias_norm(term.lookup_text),
                    term.lang,
                    term.display_term,
                    term.meaning_text,
                    term.translation_text,
                    art.content_id,
                    term.source_path,
                ),
            )
            stats.terms += 1

        scope = _scope_for_category(art.category)
        for chunk in art.chunks:
            conn.execute(
                "INSERT INTO chunk(content_id,source_path,scope,heading,block_kind,body,seg)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    art.content_id,
                    chunk.source_path,
                    scope,
                    chunk.heading,
                    chunk.block_kind,
                    chunk.body,
                    seg(chunk.body),
                ),
            )
            stats.chunks += 1
        for level in art.levels:
            seen_levels.add(level)

    stats.levels_seen = sorted(seen_levels)
    return stats


def _alias_candidates(art: Any) -> Iterable[Tuple[str, str]]:
    """别名候选：只放有独立证据的名字，不从空标题造名。"""
    if art.entity_name:
        yield art.entity_name, "canonical"
    if art.title and art.title != art.entity_name:
        yield art.title, "title"
    if art.title_alt and art.title_alt not in (art.title, art.entity_name):
        yield art.title_alt, "page_title"


def write_meta(conn: sqlite3.Connection, meta: Dict[str, Any]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
        [(str(k), json.dumps(v, ensure_ascii=False)) for k, v in meta.items()],
    )


def read_meta(conn: sqlite3.Connection) -> Dict[str, Any]:
    return {row["key"]: json.loads(row["value"]) for row in conn.execute("SELECT key,value FROM meta")}


def build_from_articles(
    path: Any,
    articles: Sequence[Any],
    *,
    entries: Optional[Sequence[Tuple[int, Optional[int], str, Optional[str], Optional[int]]]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> BuildStats:
    """建库到 `path`（必须是新文件）：目标已存在就报错，**任何情况下都不删既有的库**。

    没有 `overwrite` 开关：覆盖等于「先毁后建」，一旦中途失败就拿不回已验收的旧库；
    要重建请给新路径，由调用方决定何时切换。异常只清理本次新建的残留文件。
    """
    from pathlib import Path as _Path

    target = _Path(path)
    if target.exists():
        raise FileExistsError(f"目标库已存在，未覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target))
    try:
        create_schema(conn)
        with conn:
            stats = insert_articles(conn, articles, entries=entries)
            write_meta(conn, meta or {})
        conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('optimize')")
        conn.commit()
        return stats
    except Exception:
        conn.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            leftover = _Path(str(target) + suffix)
            if leftover.exists():
                leftover.unlink()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def db_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    def scalar(sql: str, args: Sequence[Any] = ()) -> Any:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None

    return {
        "articles": scalar("SELECT COUNT(*) FROM article"),
        "entities": scalar("SELECT COUNT(*) FROM entity"),
        "aliases": scalar("SELECT COUNT(*) FROM alias"),
        "facts": scalar("SELECT COUNT(*) FROM fact"),
        "media": scalar("SELECT COUNT(*) FROM media_ref"),
        "terms": scalar("SELECT COUNT(*) FROM term"),
        "chunks": scalar("SELECT COUNT(*) FROM chunk"),
        "entries": scalar("SELECT COUNT(*) FROM entry"),
        "by_status": {
            row["availability"]: row["n"]
            for row in conn.execute(
                "SELECT availability, COUNT(*) AS n FROM article GROUP BY availability"
            )
        },
        "by_schema": {
            row["schema"]: row["n"]
            for row in conn.execute(
                "SELECT schema, COUNT(*) AS n FROM article GROUP BY schema"
            )
        },
        "media_by_kind": {
            row["kind"]: row["n"]
            for row in conn.execute("SELECT kind, COUNT(*) AS n FROM media_ref GROUP BY kind")
        },
        "media_by_role": {
            row["role"]: row["n"]
            for row in conn.execute("SELECT role, COUNT(*) AS n FROM media_ref GROUP BY role")
        },
        "user_version": scalar("PRAGMA user_version"),
        "journal_mode": scalar("PRAGMA journal_mode"),
    }
