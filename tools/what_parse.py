# -*- coding: utf-8 -*-
"""stellasora_what 离线解析：详情原文 → 可入库的结构化事实。

两种顶层形状分派（实测 2026-11-25 快照，722 篇）：

- 节点数组（680 篇）：ProseMirror 风格富文本 + `illustrated-book` 结构化块。
- `__tjMeta` / `pages` 对象（42 篇）：Live2D 立绘设计稿。`pages[i]` 有 `data`（PC）与
  `wapData`（移动）两份**不同**的设计稿（实测 42/42 两份 JSON 长度都不同），必须
  分别保留来源；`content_live_2d` 是 JSON 字符串（立绘资源），`content_bg` /
  `content_wap_bg` 是页面背景。

解析纪律（与计划 §3、§4 对应）：

- `source_path` 一律 JSON Pointer，可回原文复核；不做「见标题就归事实」的猜测。
- 媒体每一处真实出现都保留：同一 URL 出现在正文与立绘分组是两条记录。遍历本身
  只走一次，不用 id() 去重（那会连带删掉真实重复位置）。
- 媒体角色（`role`）只由路径/实际结构/标注证据判定。结构不足以判定时写
  `unknown` + `needs_review`，不按 `image_num`、像素尺寸或「唯一 image 节点」推断。
- 可提取文字为空但结构可枚举 → `media_only`（合法状态，不是失败）；结构缺失或
  解析中断 → `partial_parse`；未知顶层/坏 JSON 各自独立状态，不与「未收录」混用。
- 不从图片内容推数值：图只登记位置与用途，数值一律来自文字叶。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------- 状态与角色枚举

STATUS_OK = "ok"
STATUS_MEDIA_ONLY = "media_only"
STATUS_PARTIAL = "partial_parse"
STATUS_EMPTY = "empty_content"
STATUS_NO_TEXT = "no_extractable_text"   # 结构可枚举，但既无文字也无媒体
STATUS_BAD_JSON = "bad_json"
STATUS_UNKNOWN_SCHEMA = "unknown_schema"

SCHEMA_ARRAY = "prosemirror"
SCHEMA_TJMETA = "tjmeta"

ROLE_BODY = "body_image"
ROLE_ICON = "inline_icon"
ROLE_ART = "character_art"
ROLE_BANNER = "card_banner"
ROLE_NAV = "nav_decoration"
ROLE_UNKNOWN = "unknown"

# 非图片媒体的角色：**不进入 body_image 统计**。`role` 的语义是「这张图用来干什么」，
# 音频/视频/模型/页面背景/外站嵌入不是这张图的用途，混进 body_image 会让
# 「正文图有多少」这个数字失真。
ROLE_AUDIO_ASSET = "audio_asset"
ROLE_VIDEO_ASSET = "video_asset"
ROLE_LIVE2D_ASSET = "live2d_asset"
ROLE_PAGE_BACKGROUND = "page_background"
ROLE_EMBED = "external_embed"

JUDGED = "judged"
UNJUDGED = "unjudged"
NEEDS_REVIEW = "needs_review"

# 媒体 kind：只有 image/album 可能是攻略正文图；audio/video 是同位置媒体；
# live2d/background/icon_svg 是资源或装饰，永不当作攻略正文图。
KIND_IMAGE = "image"
KIND_ALBUM = "album"
KIND_AUDIO = "audio"
KIND_VIDEO = "video"
KIND_LIVE2D = "live2d"
KIND_BACKGROUND = "background"
KIND_EMBED = "embed"

# 只有这些 kind 进入「可发送图片」候选池
SENDABLE_KINDS = (KIND_IMAGE, KIND_ALBUM)

# 页签/立绘位的“资产角色”按**真实文件类型**定：mp4 不是图片，不能标成 character_art
_KIND_ASSET_ROLE = {
    KIND_IMAGE: ROLE_ART,
    KIND_ALBUM: ROLE_ART,
    KIND_AUDIO: ROLE_AUDIO_ASSET,
    KIND_VIDEO: ROLE_VIDEO_ASSET,
    KIND_LIVE2D: ROLE_LIVE2D_ASSET,
    KIND_BACKGROUND: ROLE_PAGE_BACKGROUND,
    KIND_EMBED: ROLE_EMBED,
}


def _asset_role_for(kind: str) -> str:
    return _KIND_ASSET_ROLE.get(kind, ROLE_UNKNOWN)

# ---------------------------------------------------------------- illustrated-book

# skill-info 块标题 → field_key。命名不一致的归一在此集中，避免散落在遍历里。
_SKILL_FIELD = {
    "主位效果": "sigil.main_effect",
    "辅位效果": "sigil.sub_effect",
    "天赋": "skill.talent",
    "档案": "skill.file",
    "收录语": "skill.quote",
    "秘纹故事": "sigil.story",
    "逸闻笔记": "skill.anecdote",
    "员工手册": "skill.manual",
}

# character-profile 的 attrList 标题 → field_key（薪资/俸禄 归一到 salary）
_PROFILE_FIELD = {
    "生日": "birthday",
    "所属": "affiliation",
    "武器": "weapon",
    "地址": "address",
    "经验": "experience",
    "薪资": "salary",
    "俸禄": "salary",
    "技能": "profile.skill_note",
}

# audio-info 标题为「基本信息」的是秘纹元信息容器，不是语音（实测 106 篇）
_AUDIO_META_TITLE = "基本信息"
# 上游未填分组名时的占位串，不能当真实分组标签
_AUDIO_PLACEHOLDER_GROUPS = ("分组标题1",)

_HEADER_TYPES = ("header1", "header2", "header3")

# 面板指标的中文标签 → 规范 field_key 后缀。落库用规范名，原文标签另存 extra.label，
# 这样 planner 传 `stat.attack` 与用户看到的「攻击」指向同一条事实。
_STAT_KEYS = {
    "生命": "hp",
    "生命值": "hp",
    "攻击": "attack",
    "攻击力": "attack",
    "防御": "defense",
    "防御力": "defense",
    "暴击率": "crit_rate",
    "暴击伤害": "crit_dmg",
    "韧性": "toughness",
    "绝招伤害": "ult_damage",
    "技能伤害": "skill_damage",
    "普攻伤害": "normal_damage",
}
_ELEMENT_STAT = {"水": "water", "火": "fire", "地": "earth", "风": "wind",
                 "光": "light", "暗": "dark"}


def stat_field_key(label: str) -> str:
    """面板指标 → `stat.<规范名>`；未知标签退化成原文标签，不虚构英文名。"""
    key = _STAT_KEYS.get(label)
    if key:
        return f"stat.{key}"
    m = re.fullmatch(r"([水火地风光暗])系伤害", label)
    if m:
        return f"stat.element_damage.{_ELEMENT_STAT[m.group(1)]}"
    return f"stat.{label}"


_SCOPE_BY_CATEGORY = {
    "攻略区": "wiki",
    "剧情区": "wiki",
    "旅人图鉴": "wiki",
    "秘纹图鉴": "wiki",
    "怪物图鉴": "wiki",
    "NPC": "wiki",
    "游戏鉴赏": "gallery",
}

# 目录第一段是**真实顶层分区**（实测 9 个）。第二段有的只是页面 UI 提示
# （250 篇怪物页的「建议使用PC端或手机横屏查看」），不能当分类。
# 顶层原名 → 展示用类别名（保留 tree_path 原值作证据）。
_SECTION_CATEGORY = {
    "旅人图鉴": "旅人图鉴",
    "NPC/卫星图鉴": "NPC",
    "秘纹图鉴": "秘纹",
    "怪物图鉴": "怪物",
    "攻略区": "攻略区",
    "剧情区": "剧情区",
    "二创区": "二创区",
    "游戏鉴赏": "游戏鉴赏",
}
# 只做兜底防御：万一顶层本身写着提示语，也不当分类
_HINT_SEG_RE = re.compile(r"建议使用|横屏|PC端|手机端|手机查看|手机浏览")


# 顶层节点名可能自带斜杠（实测 `NPC/卫星图鉴`），按**前缀**匹配而不是按段切分，
# 否则 '/NPC/卫星图鉴/NPC/卫星图鉴/波西亚' 会被切成 ['NPC','卫星图鉴',…] 认错。
_TOP_PREFIXES = sorted(_SECTION_CATEGORY, key=len, reverse=True)


def _category_from_tree(tree_path: Optional[str]) -> Optional[str]:
    """category 取**真实顶层分区**；目录缺失 → None（不猜、不拿标题/提示顶替）。"""
    if not tree_path:
        return None
    for top in _TOP_PREFIXES:
        if _HINT_SEG_RE.search(top):
            continue
        if tree_path == f"/{top}" or tree_path.startswith(f"/{top}/"):
            return _SECTION_CATEGORY[top]
    return None

_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_SIZE_RE = re.compile(r"/w_(\d+)/h_(\d+)/")

# `content_live_2d` 是"装着 JSON 的字符串"，指针必须经过 `:parsed` 才走得通
_EMBEDDED_LIVE2D = "/content_live_2d:parsed"
_EMBEDDED_MARK = ":parsed"


def _esc(key: str) -> str:
    """JSON Pointer 转义（RFC 6901）。"""
    return str(key).replace("~", "~0").replace("/", "~1")


def _ptr(parent: str, key: Any) -> str:
    return f"{parent}/{_esc(str(key))}"


# ---------------------------------------------------------------- 数据类


@dataclass(slots=True)
class Chunk:
    source_path: str
    heading: str
    body: str
    block_kind: str


@dataclass(slots=True)
class Fact:
    field_key: str
    qualifier: str
    ordinal: int
    source_path: str
    value_text: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MediaRef:
    source_path: str
    ordinal: int
    kind: str
    url: str
    role: str
    role_evidence: str
    role_status: str
    group_label: Optional[str] = None
    locale: Optional[str] = None
    label: Optional[str] = None
    node_path: Optional[str] = None
    side: Optional[str] = None
    equivalent_text: Optional[str] = None


@dataclass(slots=True)
class Term:
    lookup_text: str
    lang: str
    display_term: str
    meaning_text: str
    translation_text: Optional[str]
    source_path: str


@dataclass(slots=True)
class Note:
    """结构性观察：区分「未知/坏结构」「缺关键字段」「仅媒体」的显式记录。"""

    code: str
    source_path: str
    detail: str


@dataclass(slots=True)
class BlockLoc:
    """结构块的位置与标题（回答「这个字段出自哪个块」时用，不是事实本身）。"""

    block_type: str
    title: str
    content_id_path: str
    spec_pointer: str


@dataclass(slots=True)
class ParsedArticle:
    content_id: int
    title: str                      # detail.title 原值（可能为空串）
    title_alt: Optional[str]        # 独立证据得到的题名（如 pages[0].title）
    title_alt_source: Optional[str]
    summary: Optional[str]
    type: Optional[int]
    category: Optional[str]
    tree_path: Optional[str]
    list_entry_id: Optional[int]
    detail_updated_at: Optional[int]
    list_updated_at: Optional[int]
    word_count: Optional[int]
    image_num: Optional[int]
    schema: Optional[str]
    status: str
    source_url: str
    raw_sha256: str
    raw_gzip: bytes
    entity_kind: str
    entity_name: str
    entity_name_source: str
    chunks: List[Chunk] = field(default_factory=list)
    facts: List[Fact] = field(default_factory=list)
    media: List[MediaRef] = field(default_factory=list)
    terms: List[Term] = field(default_factory=list)
    levels: List[int] = field(default_factory=list)
    stat_labels: List[str] = field(default_factory=list)
    blocks: List[BlockLoc] = field(default_factory=list)
    notes: List[Note] = field(default_factory=list)
    links_internal: int = 0
    links_external: int = 0
    icon_svg: int = 0
    dual_side_text_equal: Optional[bool] = None

    @property
    def display_title(self) -> str:
        return self.title or self.title_alt or ""


# ---------------------------------------------------------------- 文本工具


def rich_text(node: Any) -> str:
    """富文本叶 → 纯文本。相邻叶之间不插空格（中文无词边界）。"""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(rich_text(x) for x in node)
    if isinstance(node, dict):
        t = node.get("text")
        if isinstance(t, str):
            return t
        if node.get("type") in ("image", "image-album", "audio", "video"):
            return ""
        for key in ("children", "data"):
            sub = node.get(key)
            if isinstance(sub, (list, dict)):
                return rich_text(sub)
        return ""
    return ""


def editor_text(value: Any) -> str:
    """`simpleEditor` / 富文本容器 → 纯文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") in ("simpleEditor", "paragraph") or "data" in value:
            return rich_text(value.get("data") if "data" in value else value)
        return rich_text(value)
    if isinstance(value, list):
        return "".join(editor_text(x) for x in value)
    return ""


def editor_paragraphs(value: Any) -> List[Any]:
    """`simpleEditor` → 其 `data` 段落列表；形状不合规时返回空列表。"""
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, list):
            return data
    elif isinstance(value, list):
        return value
    return []


def _norm_space(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _editor_para_path(base: str, field: str, index: int) -> str:
    """富文本容器里第 index 段的完整 JSON Pointer：`<base>/<field>/data/<index>`。"""
    return _ptr(_ptr(_ptr(base, field), "data"), index)


def _url_class(url: str) -> str:
    low = url.lower()
    if "/live2d/" in low or re.search(
        r"\.(moc3|model3\.json|physics3\.json|motion3\.json|cdi3\.json|exp3\.json"
        r"|pose3\.json|userdata3\.json)$",
        low,
    ):
        return KIND_LIVE2D
    if re.search(r"\.(ogg|mp3|wav|m4a|aac|flac|opus)(\?|$)", low):
        return KIND_AUDIO
    if re.search(r"\.(mp4|webm|mov|mkv|m3u8|flv)(\?|$)", low):
        return KIND_VIDEO
    return KIND_IMAGE


# ---------------------------------------------------------------- 解析主体


class _Ctx:
    """一次解析的累积状态（标题栈、序号计数）。"""

    def __init__(self, art: ParsedArticle) -> None:
        self.art = art
        self.headings: List[str] = []
        self.fact_seq: Dict[Tuple[str, str], int] = {}
        self.struct_errors = 0
        # 文章头图/署名区：首个章节标题、分隔线或结构化块之前的区域。
        # 这个区域里的独立图片实测是署名横幅（728502 的 710×189），结构不足以判成正文图。
        self.preamble_open = True
        # 正在解析「嵌套在其它字段里的块」的层数：此时只收文本与媒体，不登记事实
        self.nested_depth = 0

    def heading(self) -> str:
        return " / ".join(h for h in self.headings if h)

    def note(self, code: str, path: str, detail: str) -> None:
        self.art.notes.append(Note(code, path, detail))

    def add_chunk(self, path: str, body: str, block_kind: str) -> None:
        body = body.strip()
        if not body:
            return
        self.art.chunks.append(Chunk(path, self.heading(), body, block_kind))

    def add_fact(
        self,
        field_key: str,
        value_text: str,
        path: str,
        *,
        qualifier: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        value_text = value_text.strip()
        if not value_text:
            return
        if self.nested_depth:
            # 嵌套块里的事实已在原块登记过；再登记一次会让「语音条数」这类计数翻倍
            return
        key = (field_key, qualifier)
        self.fact_seq[key] = self.fact_seq.get(key, 0) + 1
        self.art.facts.append(
            Fact(
                field_key=field_key,
                qualifier=qualifier,
                ordinal=self.fact_seq[key],
                source_path=path,
                value_text=value_text,
                extra=extra or {},
            )
        )

    def add_media(
        self,
        url: str,
        path: str,
        ordinal: int,
        *,
        kind: str,
        role: str,
        evidence: str,
        status: str,
        group_label: Optional[str] = None,
        locale: Optional[str] = None,
        label: Optional[str] = None,
        node_path: Optional[str] = None,
        side: Optional[str] = None,
        equivalent_text: Optional[str] = None,
    ) -> None:
        url = (url or "").strip()
        if not url:
            return
        self.art.media.append(
            MediaRef(
                source_path=path,
                ordinal=ordinal,
                kind=kind,
                url=url,
                role=role,
                role_evidence=evidence,
                role_status=status,
                group_label=group_label,
                locale=locale,
                label=label,
                node_path=node_path,
                side=side,
                equivalent_text=equivalent_text,
            )
        )


def _entity_from_tree(
    content_id: int, tree_path: Optional[str], title: str
) -> Tuple[str, str, str]:
    """实体种类与名称：优先目录树（真实归属证据），否则用文章题名。"""
    if tree_path:
        segs = [s for s in tree_path.split("/") if s]
        top = segs[0] if segs else ""
        name = segs[-1] if segs else title
        kind = {
            "旅人图鉴": "traveler",
            "秘纹图鉴": "sigil",
            "怪物图鉴": "monster",
            "NPC": "npc",
        }.get(top, "article")
        return kind, name, f"tree:{tree_path}"
    if title:
        return "article", title, "detail.title"
    return "article", "", "missing"


def parse_detail(
    detail: Dict[str, Any],
    *,
    tree_path: Optional[str] = None,
    list_item: Optional[Dict[str, Any]] = None,
    list_updated_at: Optional[int] = None,
) -> ParsedArticle:
    """解析一份 `/v1/content/detail/{id}` 响应体（`data` 段）。"""
    data = detail
    content_id = int(data.get("content_id") or data.get("id") or 0)
    title = (data.get("title") or "").strip()
    raw = data.get("content_json") or ""
    raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else b""

    kind, name, name_src = _entity_from_tree(content_id, tree_path, title)
    category = _category_from_tree(tree_path)

    art = ParsedArticle(
        content_id=content_id,
        title=title,
        title_alt=None,
        title_alt_source=None,
        summary=(data.get("summary") or None),
        type=data.get("type"),
        category=category,
        tree_path=tree_path,
        list_entry_id=(list_item or {}).get("entry_id") or data.get("entry_id"),
        detail_updated_at=data.get("updated_at"),
        list_updated_at=list_updated_at or (list_item or {}).get("updated_at"),
        word_count=data.get("word_count"),
        image_num=data.get("image_num"),
        schema=None,
        status=STATUS_OK,
        source_url=f"https://www.gamekee.com/stellasora/{content_id}.html",
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw_gzip=_gzip(raw_bytes),
        entity_kind=kind,
        entity_name=name,
        entity_name_source=name_src,
    )

    ctx = _Ctx(art)
    if data.get("type") is None:
        ctx.note("missing_key", "", "detail.type 缺失")
    if not isinstance(raw, str) or not raw.strip():
        art.status = STATUS_EMPTY
        ctx.note("empty_content_json", "", "content_json 为空")
        _finalize(art)
        return art

    try:
        doc = json.loads(raw)
    except Exception as exc:  # 坏 JSON 与「未收录」严格分开
        art.status = STATUS_BAD_JSON
        ctx.note("bad_json", "", f"{type(exc).__name__}: {exc}")
        _finalize(art)
        return art

    if isinstance(doc, list):
        art.schema = SCHEMA_ARRAY
        _parse_array(doc, ctx)
    elif isinstance(doc, dict) and doc.get("__tjMeta"):
        art.schema = SCHEMA_TJMETA
        _parse_tjmeta(doc, ctx)
    elif isinstance(doc, dict) and isinstance(doc.get("pages"), list):
        art.schema = SCHEMA_TJMETA
        ctx.note("missing_key", "", "顶层对象缺 __tjMeta 但有 pages，按立绘设计稿解析")
        _parse_tjmeta(doc, ctx)
    else:
        art.status = STATUS_UNKNOWN_SCHEMA
        ctx.note(
            "unknown_schema",
            "",
            f"顶层类型 {type(doc).__name__}，键 {sorted(doc)[:8] if isinstance(doc, dict) else '-'}",
        )
        _finalize(art)
        return art

    # 42 篇对象格式的 detail.title 实测 39 篇为空，题名要从 pages[0].title 独立取
    if not art.title and art.schema == SCHEMA_TJMETA:
        alt = _first_page_title(doc)
        if alt:
            art.title_alt = alt
            art.title_alt_source = "pages[0].title"
            if art.entity_name_source in ("missing", "detail.title"):
                art.entity_name = alt
                art.entity_name_source = "pages[0].title"

    _finalize(art)
    return art


def _first_page_title(doc: Dict[str, Any]) -> Optional[str]:
    for page in doc.get("pages") or []:
        if isinstance(page, dict):
            t = (page.get("title") or "").strip()
            if t:
                return t
    return None


def _gzip(raw: bytes) -> bytes:
    import gzip as _gz

    return _gz.compress(raw, 6) if raw else b""


def _finalize(art: ParsedArticle) -> None:
    """收尾定状态：文字、事实、媒体三类证据分开判定（不按 word_count 猜纯图）。

    「既无文字也无媒体」不是 `media_only`——那个状态的含义是「有结构、有媒体、
    只是没有可提取文字」。两者混用会让「纯图页有多少」这个数字虚高。
    """
    has_text = bool(art.chunks) or bool(art.facts) or bool(art.terms)
    has_media = bool(art.media)
    hard = [n for n in art.notes if n.code in (
        "missing_block_data", "bad_level_list", "unknown_block", "bad_embedded_json",
        "pages_missing_side", "bad_page_shape",
    )]
    if art.status in (STATUS_EMPTY, STATUS_BAD_JSON, STATUS_UNKNOWN_SCHEMA):
        return
    if hard:
        art.status = STATUS_PARTIAL
    elif has_text:
        art.status = STATUS_OK
    elif has_media:
        art.status = STATUS_MEDIA_ONLY
    else:
        art.status = STATUS_NO_TEXT
        art.notes.append(Note("no_text_no_media", "", "结构可枚举但既无文字叶也无媒体"))


# ---------------------------------------------------------------- 数组分支


def _parse_array(nodes: List[Any], ctx: _Ctx) -> None:
    _walk_nodes(nodes, "", ctx, field_key=None, side=None)


def _walk_nodes(
    node: Any,
    path: str,
    ctx: _Ctx,
    *,
    field_key: Optional[str],
    side: Optional[str],
    entry_name: Optional[str] = None,
) -> None:
    """递归遍历节点数组分支。

    `field_key` 非空表示当前位于结构化字段内部（此时媒体不是正文图）；
    `side` 只对 tjmeta 分支有意义。
    """
    if isinstance(node, list):
        for i, child in enumerate(node):
            _walk_nodes(
                child, _ptr(path, i), ctx,
                field_key=field_key, side=side, entry_name=entry_name,
            )
        return
    if not isinstance(node, dict):
        if isinstance(node, str) and node.strip():
            ctx.add_chunk(path, node, "text")
        return

    ntype = node.get("type")

    if ntype in _HEADER_TYPES:
        text = rich_text(node.get("children")).strip()
        if text:
            ctx.preamble_open = False
            ctx.headings.append(text)
            ctx.add_chunk(path, text, ntype)
        # 标题节点 inline 也可能挂图（实测 724713 的 header2 就带一张整图）
        for i, child in enumerate(node.get("children") or []):
            if isinstance(child, dict) and child.get("type") in (
                "image", "image-album", "audio", "video", "link", "popover-comment",
            ):
                _walk_nodes(child, _ptr(_ptr(path, "children"), i), ctx,
                            field_key=field_key, side=side, entry_name=entry_name)
        return

    if ntype == "illustrated-book":
        _walk_illustrated_book(node, path, ctx, media_only=field_key is not None)
        return

    if ntype in _BLOCK_TYPES:
        # 块容器出现在非 illustrated-book 位置（实测：语音条目的 desc 里又嵌了 audio-info）。
        # 媒体位置是真的要收；事实已在原块登记过，nested_depth 会把它挡掉。
        bdata = node.get("data")
        if isinstance(bdata, dict):
            dpath = _ptr(path, "data")
            ctx.note(
                "nested_illustrated_book",
                path,
                f"嵌套在其它字段里的 {ntype} 块：只取文本与媒体，不重复登记事实",
            )
            ctx.nested_depth += 1
            try:
                _BLOCK_HANDLERS[ntype](bdata, dpath, ctx)
            finally:
                ctx.nested_depth -= 1
            return
        ctx.note("missing_block_data", path, f"{ntype}.data 不是对象")
        return

    if ntype == "flod":
        title = (node.get("title") or "").strip()
        if title:
            ctx.headings.append(title)
        for i, child in enumerate(node.get("children") or []):
            _walk_nodes(
                child, _ptr(_ptr(path, "children"), i), ctx,
                field_key=field_key, side=side, entry_name=entry_name,
            )
        if title:
            ctx.headings.pop()
        return

    if ntype == "popover-comment":
        _walk_popover(node, path, ctx, field_key=field_key)
        return

    if ntype == "table":
        _walk_table(node, path, ctx)
        return

    if ntype in ("bulleted-list", "numbered-list"):
        for i, child in enumerate(node.get("children") or []):
            _walk_nodes(
                child, _ptr(_ptr(path, "children"), i), ctx,
                field_key=field_key, side=side, entry_name=entry_name,
            )
        return

    if ntype == "list-item":
        text = rich_text(node.get("children")).strip()
        if text:
            ctx.add_chunk(path, text, "list-item")
        # 继续递归以收取其中的媒体
        for i, child in enumerate(node.get("children") or []):
            if isinstance(child, dict) and child.get("type") in (
                "image", "image-album", "audio", "video", "link",
            ):
                _walk_nodes(
                    child, _ptr(_ptr(path, "children"), i), ctx,
                    field_key=field_key, side=side, entry_name=entry_name,
                )
        return

    if ntype == "image":
        _add_rich_image(node, path, ctx, field_key=field_key, entry_name=entry_name)
        return

    if ntype == "image-album":
        urls = node.get("data")
        if isinstance(urls, list):
            for i, url in enumerate(urls):
                if isinstance(url, str):
                    ctx.add_media(
                        url,
                        _ptr(_ptr(path, "data"), i),
                        i,
                        kind=KIND_ALBUM,
                        role=ROLE_BODY,
                        evidence=f"富文本图集节点 {path}",
                        status=JUDGED,
                        label=f"图集第 {i + 1} 张",
                        node_path=path,
                    )
        else:
            ctx.note("missing_block_data", path, "image-album 的 data 不是数组")
        for i, child in enumerate(node.get("children") or []):
            _walk_nodes(child, _ptr(_ptr(path, "children"), i), ctx,
                        field_key=field_key, side=side, entry_name=entry_name)
        return

    if ntype == "audio":
        url = node.get("src")
        if isinstance(url, str):
            ctx.add_media(
                url, _ptr(path, "src"), 0,
                kind=KIND_AUDIO,
                role=ROLE_AUDIO_ASSET,
                evidence=f"富文本音频节点 {path}",
                status=NEEDS_REVIEW,
                node_path=path,
            )
        return

    if ntype == "video":
        url = node.get("src")
        if isinstance(url, str) and url.strip():
            # 实测存在 `<iframe src="//player.bilibili.com/...">` 这种外站嵌入：
            # 它是嵌入引用，不是 CDN 媒体，登记成 embed 且不当作可发送文件
            stripped = url.strip()
            if stripped.lstrip().startswith("<"):
                m_src = re.search(r'src="([^"]+)"', stripped)
                target = m_src.group(1) if m_src else ""
                # source_path 指向**原文 iframe 串**（可回读），url 是从中派生的引用：
                # 派生关系写进 evidence，避免被当成 GameKee 图片地址使用
                ctx.add_media(
                    target or stripped, _ptr(path, "src"), 0,
                    kind=KIND_EMBED,
                    role=ROLE_EMBED,
                    evidence=(
                        f"外站播放器嵌入：原文 {path}/src 是 iframe 串，"
                        f"此处 url 为从该串提取的 src（派生值，非 GameKee 媒体）"
                    ),
                    status=NEEDS_REVIEW,
                    node_path=path,
                )
            else:
                ctx.add_media(
                    stripped, _ptr(path, "src"), 0,
                    kind=KIND_VIDEO,
                    role=ROLE_VIDEO_ASSET,
                    evidence=f"富文本视频节点 {path}",
                    status=JUDGED,
                    node_path=path,
                )
        poster = node.get("poster")
        if isinstance(poster, str) and poster.strip():
            ctx.add_media(
                poster.strip(), _ptr(path, "poster"), 1,
                kind=KIND_IMAGE,
                role=ROLE_UNKNOWN,
                evidence=f"视频封面（poster）{path}；封面用途未判定",
                status=NEEDS_REVIEW,
                node_path=path,
            )
        return

    if ntype == "link":
        url = node.get("url")
        _count_link(ctx, url)
        text = rich_text(node.get("children")).strip()
        if text:
            ctx.add_chunk(path, text, "link")
        return

    if ntype == "mention-doc":
        content = node.get("content")
        if isinstance(content, str) and content.strip():
            ctx.add_chunk(path, content, "mention-doc")
        _count_link(ctx, node.get("url"))
        return

    if ntype == "divider":
        return

    if ntype in ("paragraph",):
        text = rich_text(node.get("children")).strip()
        if text:
            ctx.add_chunk(path, text, "paragraph")
        for i, child in enumerate(node.get("children") or []):
            if isinstance(child, dict) and child.get("type") in (
                "image", "image-album", "audio", "video", "link", "popover-comment",
            ):
                _walk_nodes(child, _ptr(_ptr(path, "children"), i), ctx,
                            field_key=field_key, side=side, entry_name=entry_name)
        return

    # 无 type 的节点按文本叶判断，不当未知正文块
    if ntype is None:
        text = rich_text(node).strip()
        if text:
            ctx.add_chunk(path, text, "text-leaf")
        for key, value in node.items():
            if isinstance(value, (list, dict)):
                _walk_nodes(value, _ptr(path, key), ctx,
                            field_key=field_key, side=side, entry_name=entry_name)
        return

    # 未知 type：显式记录，不静默丢
    ctx.note("unknown_block", path, f"未知节点类型 {ntype!r}")
    text = rich_text(node.get("children")).strip()
    if text:
        ctx.add_chunk(path, text, f"unknown:{ntype}")
    for key, value in node.items():
        if key == "children":
            continue
        if isinstance(value, (list, dict)):
            _walk_nodes(value, _ptr(path, key), ctx,
                        field_key=field_key, side=side, entry_name=entry_name)


def _count_link(ctx: _Ctx, url: Any) -> None:
    if not isinstance(url, str) or not url:
        return
    if "gamekee.com" in url:
        ctx.art.links_internal += 1
    elif url.startswith(("http://", "https://", "//")):
        ctx.art.links_external += 1


def _walk_table(node: Dict[str, Any], path: str, ctx: _Ctx) -> None:
    """表格：按行列输出，图片不变成伪文本。

    表格是「字段 → 值」的天然结构，因此单元格里的**纯图**（没有同格文字）不按正文图
    处理：实测怪物图鉴的弱点列就是只有图示（705924），此时只能答「原页只有图示」，
    不能从图里读属性名。这类图登记为 `unknown` + `needs_review`，并把单元格坐标写进
    evidence 供人工标注。
    """
    rows = node.get("children") or []
    for ri, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        cells = row.get("children") or []
        row_texts: List[str] = []
        for ci, cell in enumerate(cells):
            if not isinstance(cell, dict):
                continue
            children = cell.get("children")
            text = rich_text(children).strip()
            cpath = _ptr(_ptr(_ptr(path, "children"), ri), "children")
            cpath = _ptr(cpath, ci)
            if text:
                row_texts.append(text)
            # 单元格文字由 paragraph 分支自己登记，这里不重复 add_chunk；
            # 纯图单元格交给 _add_rich_image 的 table-cell 分支标 needs_review
            _walk_nodes(
                children, _ptr(cpath, "children"), ctx,
                field_key="table-cell" if not text else None,
                side=None,
                entry_name=text or None,
            )
        if row_texts:
            ctx.add_chunk(_ptr(path, "children"), "\t".join(row_texts), "table-row")


def _walk_popover(node: Dict[str, Any], path: str, ctx: _Ctx, *, field_key: Optional[str]) -> None:
    """`children` 是被注释原文，`data` 是注释（术语金矿，日中对照）。"""
    original = rich_text(node.get("children")).strip()
    comment = editor_text(node.get("data")).strip()
    if original and comment:
        lang = "ja" if _KANA_RE.search(original) else "zh"
        trans = None
        if lang == "ja" and not _KANA_RE.search(comment):
            trans = comment
        ctx.art.terms.append(
            Term(
                lookup_text=original,
                lang=lang,
                display_term=original,
                meaning_text=comment,
                translation_text=trans,
                source_path=path,
            )
        )
    if comment:
        ctx.add_chunk(path, f"{original}：{comment}" if original else comment, "popover-comment")
    for i, child in enumerate(editor_paragraphs(node)):
        _walk_nodes(child, _ptr(_ptr(path, "data"), i), ctx,
                    field_key=field_key, side=None)


def _add_rich_image(
    node: Dict[str, Any],
    path: str,
    ctx: _Ctx,
    *,
    field_key: Optional[str],
    entry_name: Optional[str],
) -> None:
    url = node.get("src")
    if not isinstance(url, str):
        return
    kind = _url_class(url)
    caption = node.get("title") if isinstance(node.get("title"), str) else None
    alt = node.get("alt")
    alt_url = alt if isinstance(alt, str) and alt.startswith(("//", "http://", "https://")) else None
    # alt 与 src 相同时是同一引用的镜像字段（实测 708010/711425/712485），不是新位置；
    # 只有 alt 指向另一个 URL 时才另记一条
    alt_note = ""
    if alt_url:
        alt_note = "；alt 与 src 同 URL，未重复登记" if alt_url == url else "；alt 指向另一 URL，另记一条"
        if alt_url == url:
            ctx.note(
                "alt_mirror_same_url",
                _ptr(path, "alt"),
                "图节点 alt 与 src 同 URL；同一真实位置不登记两条",
            )

    if field_key is None:
        # 富文本正文图：位置证据要够才能判 body_image。
        # - 文章开头区（首个章节标题/分隔线/结构化块之前）里、且此前已有文字的图，
        #   实测是署名横幅/封面（728502 的 710×189 横幅），结构判不出用途 → 人工复核；
        # - 动图常用于引导/装饰（705622 段末的跳转 GIF），也不自动当正文图；
        # - 其余正文位置的图按正文图登记，仍保留 source_path 供逐图复核。
        if ctx.preamble_open and ctx.art.chunks:
            ctx.add_media(
                url, _ptr(path, "src"), 0,
                kind=kind,
                role=ROLE_UNKNOWN,
                evidence=(
                    f"文章开头、章节标题之前的图片 {path}，且此前已有文字；"
                    "可能为署名横幅/封面，结构不足以判定"
                ),
                status=NEEDS_REVIEW,
                label=caption,
                node_path=path,
            )
            return
        if kind == KIND_IMAGE and url.lower().split("?")[0].endswith(".gif"):
            ctx.add_media(
                url, _ptr(path, "src"), 0,
                kind=kind,
                role=ROLE_UNKNOWN,
                evidence=f"动图（gif）富文本节点 {path}；动图常用于引导/装饰，用途未判定",
                status=NEEDS_REVIEW,
                label=caption,
                node_path=path,
            )
            return
        ctx.add_media(
            url, _ptr(path, "src"), 0,
            kind=kind,
            role=ROLE_BODY,
            evidence=f"富文本图节点 {path}（不在结构化字段内，位于正文文本之后）{alt_note}",
            status=JUDGED,
            label=caption,
            node_path=path,
        )
        if alt_url and alt_url != url:
            ctx.add_media(
                alt_url, _ptr(path, "alt"), 1,
                kind=_url_class(alt_url),
                role=ROLE_UNKNOWN,
                evidence=f"富文本图节点 {path} 的 alt 字段指向另一 URL",
                status=NEEDS_REVIEW,
                label=caption,
                node_path=path,
            )
        return
    if field_key == "name":
        ctx.add_media(
            url, _ptr(path, "src"), 0,
            kind=kind,
            role=ROLE_ICON,
            evidence=f"结构化条目 name 字段内嵌图 {path}{alt_note}",
            status=JUDGED,
            label=caption,
            node_path=path,
            equivalent_text=entry_name,
        )
    elif field_key == "table-cell":
        # 单元格里只有图没有文字：图可能承载语义（如怪物弱点属性），但结构不足以判定，
        # 只能标 needs_review；答复时必须说「原页只有图示」，不从图里读属性名。
        ctx.add_media(
            url, _ptr(path, "src"), 0,
            kind=kind,
            role=ROLE_UNKNOWN,
            evidence=f"表格单元格内纯图、同格无可提取文字 {path}；图内语义未核实{alt_note}",
            status=NEEDS_REVIEW,
            label=caption,
            node_path=path,
        )
    else:
        ctx.add_media(
            url, _ptr(path, "src"), 0,
            kind=kind,
            role=ROLE_UNKNOWN,
            evidence=f"结构化字段 {field_key} 的文本容器内嵌图 {path}；图标与配图未判定{alt_note}",
            status=NEEDS_REVIEW,
            label=caption,
            node_path=path,
        )
    if alt_url and alt_url != url:
        ctx.add_media(
            alt_url, _ptr(path, "alt"), 1,
            kind=_url_class(alt_url),
            role=ROLE_UNKNOWN,
            evidence=f"结构化字段 {field_key} 的图节点 alt 指向另一 URL（{path}）",
            status=NEEDS_REVIEW,
            label=caption,
            node_path=path,
        )


# ---------------------------------------------------------------- illustrated-book 分支


def _walk_illustrated_book(
    node: Dict[str, Any], path: str, ctx: _Ctx, *, media_only: bool = False
) -> None:
    """`media_only=True` 表示这个块容器挂在别的块字段里（实测：语音条目的 desc 里又嵌了
    一整块 audio-info 语音表），此时只收取媒体与文本，不再重复登记事实——否则同一份
    语音会在库里出现两遍。
    """
    blocks = node.get("data")
    if not isinstance(blocks, list):
        ctx.note("missing_block_data", path, "illustrated-book.data 不是数组")
        return
    for bi, block in enumerate(blocks):
        bpath = _ptr(_ptr(path, "data"), bi)
        if not isinstance(block, dict):
            ctx.note("bad_page_shape", bpath, "块不是对象")
            continue
        btype = block.get("type")
        bdata = block.get("data")
        if not isinstance(bdata, dict):
            ctx.note("missing_block_data", bpath, f"{btype}.data 不是对象")
            continue
        # 字段指针以块内 data 为基准，source_path 才能直接回指原文
        dpath = _ptr(bpath, "data")
        ctx.art.blocks.append(
            BlockLoc(
                block_type=str(btype),
                title=str(bdata.get("title") or ""),
                content_id_path=bpath,
                spec_pointer=dpath,
            )
        )
        if media_only:
            # 嵌套在别的字段里的块：媒体位置是真的、要收；事实已在原块登记过。
            ctx.note(
                "nested_illustrated_book",
                bpath,
                f"嵌套在其它字段里的 {btype} 块：只取文本与媒体，不重复登记事实",
            )
            ctx.nested_depth += 1
            try:
                handler = _BLOCK_HANDLERS.get(btype)
                if handler is not None:
                    handler(bdata, dpath, ctx)
                else:
                    _walk_nodes(block, bpath, ctx, field_key="__nested__", side=None)
            finally:
                ctx.nested_depth -= 1
            continue
        handler = _BLOCK_HANDLERS.get(btype)
        if handler is not None:
            handler(bdata, dpath, ctx)
        elif btype == "character-info":
            _block_character_info(bdata, dpath, ctx)
        else:
            ctx.note("unknown_block", bpath, f"未知 illustrated-book 块类型 {btype!r}")
            _walk_nodes(bdata, dpath, ctx, field_key=None, side=None)
    # 块容器自身的 children（实测多为空）
    for i, child in enumerate(node.get("children") or []):
        _walk_nodes(child, _ptr(_ptr(path, "children"), i), ctx, field_key=None, side=None)


def _block_profile(bdata: Dict[str, Any], bpath: str, ctx: _Ctx) -> None:
    """character-profile：`基础信息` 与 `基本信息` 是同一类型（实测 68 + 43）。"""
    ctx.headings.append("")
    name = editor_text(bdata.get("name")).strip()
    if name:
        ctx.add_fact("profile.name", name, _ptr(bpath, "name"))
    desc_title = editor_text(bdata.get("descTitle")).strip()
    if desc_title:
        ctx.add_fact("profile.tagline", desc_title, _ptr(bpath, "descTitle"))
    desc = editor_text(bdata.get("desc")).strip()
    if desc:
        ctx.add_fact("profile.desc", desc, _ptr(bpath, "desc"))
        for i, para in enumerate(editor_paragraphs(bdata.get("desc"))):
            ctx.add_chunk(
                _editor_para_path(bpath, "desc", i), rich_text(para), "profile-desc"
            )
    for sub in ("name", "descTitle", "desc"):
        holder = bdata.get(sub)
        if not isinstance(holder, dict):
            continue
        for pi, para in enumerate(editor_paragraphs(holder)):
            _walk_nodes(
                para, _ptr(_ptr(_ptr(bpath, sub), "data"), pi), ctx,
                field_key=sub, side=None, entry_name=name or None,
            )
    for ai, attr in enumerate(bdata.get("attrList") or []):
        if not isinstance(attr, dict):
            continue
        apath = _ptr(_ptr(bpath, "attrList"), ai)
        label = editor_text(attr.get("title")).strip()
        value = editor_text(attr.get("content")).strip()
        if not label:
            ctx.note("missing_key", apath, "attrList 项缺标题")
            continue
        key = _PROFILE_FIELD.get(label, f"profile.{label}")
        ctx.add_fact(key, value, apath, extra={"label": label})
    for key in ("imageList", "imagesList"):
        urls = bdata.get(key)
        if not isinstance(urls, list):
            continue
        for i, url in enumerate(urls):
            if not isinstance(url, str):
                continue
            ctx.add_media(
                url, _ptr(_ptr(bpath, key), i), i,
                kind=KIND_IMAGE,
                role=ROLE_ART,
                evidence=f"character-profile.{key}[{i}]（立绘/形象图字段）",
                status=JUDGED,
                group_label="立绘",
                node_path=f"{bpath}/{key}",
            )
    ctx.headings.pop()


def _block_fight(bdata: Dict[str, Any], bpath: str, ctx: _Ctx) -> None:
    """fight-info：等级面板。等级一律按该页实际 levelList 读，不硬编码十级。"""
    block_title = (bdata.get("title") or "").strip()
    if block_title:
        ctx.headings.append(block_title)
    levels = bdata.get("levelList")
    if not isinstance(levels, list):
        ctx.note("bad_level_list", bpath, "levelList 不是数组")
        if block_title:
            ctx.headings.pop()
        return
    for li, entry in enumerate(levels):
        if not isinstance(entry, dict):
            ctx.note("bad_level_list", _ptr(_ptr(bpath, "levelList"), li), "等级项不是对象")
            continue
        raw_level = entry.get("level")
        try:
            level = int(str(raw_level).strip())
        except (TypeError, ValueError):
            ctx.note(
                "bad_level_list",
                _ptr(_ptr(bpath, "levelList"), li),
                f"等级值不可解析 {raw_level!r}",
            )
            continue
        if level not in ctx.art.levels:
            ctx.art.levels.append(level)
        epath = _ptr(_ptr(bpath, "levelList"), li)
        for ii, item in enumerate(entry.get("levelInfoList") or []):
            if not isinstance(item, dict):
                continue
            ipath = _ptr(_ptr(epath, "levelInfoList"), ii)
            label = editor_text(item.get("title")).strip()
            value = editor_text(item.get("content")).strip()
            if not label:
                ctx.note("missing_key", ipath, "面板项缺指标名")
                continue
            if label not in ctx.art.stat_labels:
                ctx.art.stat_labels.append(label)
            ctx.add_fact(
                stat_field_key(label),
                value,
                ipath,
                qualifier=f"level:{level}",
                extra={"label": label, "level": level},
            )
    ctx.art.levels.sort()
    if block_title:
        ctx.headings.pop()


def _block_skill(bdata: Dict[str, Any], bpath: str, ctx: _Ctx) -> None:
    """skill-info：标题决定 field_key；同字段多条必须带 ordinal。"""
    block_title = (bdata.get("title") or "").strip()
    field_key = _SKILL_FIELD.get(block_title, f"skill.{block_title or 'unnamed'}")
    if block_title:
        ctx.headings.append(block_title)
    tabs = bdata.get("tabs") or []
    tab_label = {}
    for tab in tabs:
        if isinstance(tab, dict):
            tab_label[tab.get("key")] = (tab.get("label") or "").strip()
    entries = bdata.get("skillList")
    if not isinstance(entries, list):
        ctx.note("missing_block_data", bpath, "skillList 不是数组")
        if block_title:
            ctx.headings.pop()
        return
    for si, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        epath = _ptr(_ptr(bpath, "skillList"), si)
        name = editor_text(entry.get("name")).strip()
        desc = editor_text(entry.get("desc")).strip()
        more = editor_text(entry.get("moreInfo")).strip()
        group = tab_label.get(entry.get("filterTabKey"), None)
        icon = entry.get("icon")
        if isinstance(icon, str) and icon.strip().startswith(("//", "http://", "https://")):
            ctx.add_media(
                icon.strip(), _ptr(epath, "icon"), 0,
                kind=KIND_IMAGE,
                role=ROLE_ICON,
                evidence=f"skill-info 条目图标字段 {epath}/icon",
                status=JUDGED,
                label=name or None,
                node_path=epath,
                equivalent_text=name or None,
            )
        if desc:
            ctx.add_fact(
                field_key,
                desc,
                _ptr(epath, "desc"),
                extra={
                    "name": name or None,
                    "block_title": block_title or None,
                    "tab": group,
                },
            )
        elif name:
            ctx.add_fact(
                field_key, name, _ptr(epath, "name"),
                extra={"name": name, "block_title": block_title or None, "tab": group},
            )
        else:
            ctx.note("missing_key", epath, "skill-info 条目既无 desc 也无 name")
        if more:
            ctx.add_chunk(_ptr(epath, "moreInfo"), more, "skill-more-info")
        for sub in ("name", "desc", "moreInfo"):
            holder = entry.get(sub)
            if isinstance(holder, dict):
                for pi, para in enumerate(editor_paragraphs(holder)):
                    _walk_nodes(
                        para, _ptr(_ptr(_ptr(epath, sub), "data"), pi), ctx,
                        field_key=sub, side=None, entry_name=name or None,
                    )
    if block_title:
        ctx.headings.pop()


def _block_tab(bdata: Dict[str, Any], bpath: str, ctx: _Ctx) -> None:
    """tab-info：页签媒体分组（立绘/CG/壁纸/视频）。

    `tabList[i].content` 实测两种形状：**纯 URL 字符串数组**（715315 的 67 条立绘引用
    全在这里，不在富文本 image 节点内），以及**带 cover/src 的对象数组**（视频页签）。
    页签自身的 `type` 说明该组是 image 还是 video。
    """
    block_title = (bdata.get("title") or "").strip()
    if block_title:
        ctx.headings.append(block_title)
    tabs = bdata.get("tabList")
    if not isinstance(tabs, list):
        ctx.note("missing_block_data", bpath, "tabList 不是数组")
        if block_title:
            ctx.headings.pop()
        return
    for ti, tab in enumerate(tabs):
        if not isinstance(tab, dict):
            continue
        tpath = _ptr(_ptr(bpath, "tabList"), ti)
        group = (tab.get("title") or "").strip() or f"页签{ti + 1}"
        tab_type = (tab.get("type") or "").strip() or KIND_IMAGE
        items = tab.get("content")
        if not isinstance(items, list):
            ctx.note("missing_block_data", tpath, "页签 content 不是数组")
            continue
        for ui, item in enumerate(items):
            ipath = _ptr(_ptr(tpath, "content"), ui)
            if isinstance(item, str):
                item_kind = _url_class(item)
                item_role = _asset_role_for(item_kind)
                ctx.add_media(
                    item, ipath, ui,
                    kind=item_kind,
                    role=item_role,
                    evidence=(
                        f"tab-info 页签「{group}」的 content[{ui}] 直链"
                        + ("" if item_role == ROLE_ART else f"（{item_kind} 资产，不作图片候选）")
                    ),
                    status=JUDGED,
                    group_label=group,
                    node_path=f"{tpath}/content",
                )
                continue
            if not isinstance(item, dict):
                ctx.note("bad_page_shape", ipath, "页签媒体项既不是字符串也不是对象")
                continue
            for key, kind_hint in (("src", _url_class(tab_type)), ("cover", KIND_IMAGE)):
                url = item.get(key)
                if not isinstance(url, str) or not url.strip():
                    continue
                if key == "src":
                    item_kind = _url_class(url)
                    item_role = _asset_role_for(item_kind)
                    item_status = JUDGED
                    extra_evidence = (
                        "" if item_role == ROLE_ART else f"（{item_kind} 资产，不作图片候选）"
                    )
                else:
                    # 封面是视频缩略图：按真实类型登记为图片，但**不当立绘**，进待复核
                    item_kind = KIND_IMAGE
                    item_role = ROLE_UNKNOWN
                    item_status = NEEDS_REVIEW
                    extra_evidence = "（视频封面，按图片登记但用途待复核，不作立绘候选）"
                ctx.add_media(
                    url, _ptr(ipath, key), ui,
                    kind=item_kind,
                    role=item_role,
                    evidence=f"tab-info 页签「{group}」的 content[{ui}].{key}" + extra_evidence,
                    status=item_status,
                    group_label=group,
                    node_path=ipath,
                )
    if block_title:
        ctx.headings.pop()


def _block_audio(bdata: Dict[str, Any], bpath: str, ctx: _Ctx) -> None:
    """audio-info：标题「基本信息」是秘纹元信息容器（实测 106 篇），不是语音。"""
    block_title = (bdata.get("title") or "").strip()
    if block_title:
        ctx.headings.append(block_title)
    is_voice = block_title != _AUDIO_META_TITLE
    tabs = bdata.get("tabs") or []
    locale_by_key = {}
    for tab in tabs:
        if isinstance(tab, dict):
            locale_by_key[tab.get("key")] = (tab.get("label") or "").strip() or None
    entries = bdata.get("list")
    if not isinstance(entries, list):
        ctx.note("missing_block_data", bpath, "audio-info.list 不是数组")
        if block_title:
            ctx.headings.pop()
        return
    for gi, group in enumerate(entries):
        if not isinstance(group, dict):
            continue
        gpath = _ptr(_ptr(bpath, "list"), gi)
        raw_gtitle = editor_text(group.get("title")).strip()
        # `分组标题1` 是上游未填分组名的占位串，不是真实分组名
        gtitle = "" if raw_gtitle in _AUDIO_PLACEHOLDER_GROUPS else raw_gtitle
        if gtitle:
            ctx.add_chunk(_ptr(gpath, "title"), gtitle, "audio-group-title")
        locale = locale_by_key.get(group.get("filterTabKey"))
        for ci, item in enumerate(group.get("content") or []):
            if not isinstance(item, dict):
                continue
            ipath = _ptr(_ptr(gpath, "content"), ci)
            name = editor_text(item.get("name")).strip()
            desc = editor_text(item.get("desc")).strip()
            if is_voice:
                if name:
                    ctx.add_fact(
                        "voice.line", name, _ptr(ipath, "name"),
                        extra={"locale": locale, "group": gtitle or None, "text": desc or None},
                    )
                if desc:
                    ctx.add_chunk(_ptr(ipath, "desc"), desc, "voice-text")
            else:
                if desc:
                    ctx.add_chunk(_ptr(ipath, "desc"), desc, "audio-meta")
            audio_url = item.get("audio")
            if isinstance(audio_url, str) and audio_url:
                ctx.add_media(
                    audio_url, _ptr(ipath, "audio"), ci,
                    kind=KIND_AUDIO,
                    role=ROLE_AUDIO_ASSET,
                    evidence=(
                        f"audio-info 语音条目 {ipath}"
                        if is_voice
                        else f"audio-info 元信息容器内的音频 {ipath}（非语音列表）"
                    ),
                    status=JUDGED if is_voice else NEEDS_REVIEW,
                    group_label=gtitle or None,
                    locale=locale,
                    label=name or None,
                    node_path=f"{ipath}/audio",
                )
            for sub in ("name", "desc"):
                holder = item.get(sub)
                if isinstance(holder, dict):
                    for pi, para in enumerate(editor_paragraphs(holder)):
                        _walk_nodes(
                            para, _ptr(_ptr(_ptr(ipath, sub), "data"), pi), ctx,
                            field_key=sub, side=None, entry_name=name or None,
                        )
    if block_title:
        ctx.headings.pop()


# illustrated-book 块类型 → 处理函数。集中映射避免同一类型在两处各写一遍分派
# （嵌套块与非嵌套块都用它，nested_depth 负责不重复登记事实）。
_BLOCK_TYPES = (
    "character-profile", "fight-info", "skill-info", "tab-info", "audio-info",
)

def _block_character_info(bdata: Dict[str, Any], bpath: str, ctx: _Ctx) -> None:
    """character-info：导航条目。图片与其 link 分处同一条目的 content / title 子树。

    这正是 728502 的反例：6 张导航图不是正文图，判定证据是「同一 infoList 项的
    title 子树带 link.url」，而不是图片尺寸。
    """
    ctx.headings.append((bdata.get("title") or "").strip() or "导航")
    entries = bdata.get("infoList")
    if not isinstance(entries, list):
        ctx.note("missing_block_data", bpath, "infoList 不是数组")
        ctx.headings.pop()
        return
    for ii, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        ipath = _ptr(_ptr(bpath, "infoList"), ii)
        title_text = editor_text(item.get("title")).strip()
        link_url = _first_link_url(item.get("title"))
        if title_text:
            ctx.add_chunk(_ptr(ipath, "title"), title_text, "nav-title")
        for sub in ("content", "title"):
            holder = item.get(sub)
            if not isinstance(holder, dict):
                continue
            for pi, para in enumerate(editor_paragraphs(holder)):
                ppath = _ptr(_ptr(_ptr(ipath, sub), "data"), pi)
                _walk_nav_para(para, ppath, ctx, link_url=link_url, title_text=title_text)
    ctx.headings.pop()


_BLOCK_HANDLERS = {
    "character-profile": _block_profile,
    "fight-info": _block_fight,
    "skill-info": _block_skill,
    "tab-info": _block_tab,
    "audio-info": _block_audio,
}

def _walk_nav_para(
    para: Any, path: str, ctx: _Ctx, *, link_url: Optional[str], title_text: str
) -> None:
    if not isinstance(para, dict):
        return
    for ci, child in enumerate(para.get("children") or []):
        cpath = _ptr(_ptr(path, "children"), ci)
        if not isinstance(child, dict):
            continue
        if child.get("type") == "link":
            _count_link(ctx, child.get("url"))
            continue
        if child.get("type") == "image" and isinstance(child.get("src"), str):
            if link_url:
                ctx.add_media(
                    child["src"], _ptr(cpath, "src"), ci,
                    kind=_url_class(child["src"]),
                    role=ROLE_NAV,
                    evidence=(
                        f"同一 infoList 项 {path.rsplit('/data/', 1)[0]} 的 title 子树带 link "
                        f"{link_url}，图片位于 content 子树 → 导航图"
                    ),
                    status=JUDGED,
                    label=title_text or None,
                    node_path=cpath,
                )
            else:
                ctx.add_media(
                    child["src"], _ptr(cpath, "src"), ci,
                    kind=_url_class(child["src"]),
                    role=ROLE_UNKNOWN,
                    evidence=f"导航条目内图片但未找到对应 link：{cpath}",
                    status=NEEDS_REVIEW,
                    label=title_text or None,
                    node_path=cpath,
                )


def _first_link_url(node: Any) -> Optional[str]:
    if isinstance(node, dict):
        if node.get("type") == "link" and isinstance(node.get("url"), str) and node["url"]:
            return node["url"]
        for value in node.values():
            found = _first_link_url(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _first_link_url(value)
            if found:
                return found
    return None


# ---------------------------------------------------------------- tjmeta 分支


def _parse_tjmeta(doc: Dict[str, Any], ctx: _Ctx) -> None:
    art = ctx.art
    for key, kind in (("content_bg", KIND_BACKGROUND), ("content_wap_bg", KIND_BACKGROUND)):
        url = doc.get(key)
        if isinstance(url, str) and url.strip():
            ctx.add_media(
                url.strip(), f"/{key}", 0,
                kind=kind,
                role=ROLE_PAGE_BACKGROUND,
                evidence=f"页面背景字段 {key}（不是攻略正文图）",
                status=JUDGED,
                group_label="页面背景",
                node_path=f"/{key}",
            )

    live2d_raw = doc.get("content_live_2d")
    if isinstance(live2d_raw, str) and live2d_raw.strip():
        try:
            live2d = json.loads(live2d_raw)
        except Exception as exc:
            ctx.note("bad_embedded_json", "/content_live_2d", f"{type(exc).__name__}: {exc}")
            live2d = None
        if live2d is not None:
            # `:parsed` 段表示「先对该字段的 JSON 字符串做 json.loads，再继续按指针走」，
            # 回读时由 resolve_pointer() 处理，不是原文里真有的键名
            _register_live2d(live2d, _EMBEDDED_LIVE2D, ctx)
    elif isinstance(live2d_raw, list):
        _register_live2d(live2d_raw, _EMBEDDED_LIVE2D, ctx)

    pages = doc.get("pages")
    if not isinstance(pages, list):
        ctx.note("bad_page_shape", "/pages", "pages 不是数组")
        return
    side_texts: Dict[str, List[str]] = {}
    for pi, page in enumerate(pages):
        if not isinstance(page, dict):
            ctx.note("bad_page_shape", _ptr("/pages", pi), "页不是对象")
            continue
        ppath = _ptr("/pages", pi)
        page_title = (page.get("title") or "").strip()
        ctx.headings.append(page_title or f"第{pi + 1}页")
        if isinstance(page.get("icon"), str) and page["icon"].lstrip().startswith("<svg"):
            art.icon_svg += 1
        for side_key, side_name in (("data", "pc"), ("wapData", "mobile")):
            side_doc = page.get(side_key)
            spath = _ptr(ppath, side_key)
            if not isinstance(side_doc, dict):
                ctx.note("pages_missing_side", spath, f"缺 {side_key} 设计稿")
                continue
            texts = _walk_design(side_doc, spath, ctx, side=side_name, page_title=page_title)
            side_texts[side_name] = texts
        ctx.headings.pop()

    if "pc" in side_texts or "mobile" in side_texts:
        pc = _norm_space("".join(side_texts.get("pc") or []))
        mb = _norm_space("".join(side_texts.get("mobile") or []))
        art.dual_side_text_equal = bool(pc) and pc == mb
        if pc and mb and pc != mb:
            ctx.note(
                "dual_side_text_differs",
                "/pages/0",
                "PC 与移动设计稿归一化文本不一致（保留两端原位置）",
            )


def _register_live2d(entries: Any, path: str, ctx: _Ctx) -> None:
    """立绘资源：cover/lh 是图片；model3/moc3/纹理是模型资源，不是 QQ 图片。"""
    if not isinstance(entries, list):
        return
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        epath = _ptr(path, i)
        title = (entry.get("title") or "").strip()
        for key in ("cover", "lh"):
            url = entry.get(key)
            if isinstance(url, str) and url.strip():
                ctx.add_media(
                    url.strip(), _ptr(epath, key), 0,
                    kind=KIND_IMAGE,
                    role=ROLE_ART,
                    evidence=f"立绘资源 {epath}.{key}（封面/立绘整图）",
                    status=JUDGED,
                    group_label=f"立绘/{title}" if title else "立绘",
                    label=title or None,
                    node_path=epath,
                )
        _walk_model_assets(entry.get("live2d"), _ptr(epath, "live2d"), ctx, title=title)


def _walk_model_assets(node: Any, path: str, ctx: _Ctx, *, title: str) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk_model_assets(item, _ptr(path, i), ctx, title=title)
        return
    if isinstance(node, str):
        _register_asset_url(node, path, ctx, title=title)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if isinstance(value, (str, list, dict)):
            _walk_model_assets(value, _ptr(path, key), ctx, title=title)


def _register_asset_url(url: str, path: str, ctx: _Ctx, *, title: str) -> None:
    url = url.strip()
    if not url.startswith(("//", "http://", "https://")):
        return
    kind = _url_class(url)
    ctx.add_media(
        url, path, 0,
        kind=kind,
        role=ROLE_LIVE2D_ASSET,
        evidence=(
            f"Live2D 模型资源 {path}；资源不是可直接发送的图片"
            if kind == KIND_LIVE2D
            else f"Live2D 附属纹理/资源 {path}"
        ),
        status=NEEDS_REVIEW,
        group_label=f"Live2D/{title}" if title else "Live2D",
        node_path=path,
    )


def _walk_design(
    doc: Dict[str, Any], path: str, ctx: _Ctx, *, side: str, page_title: str
) -> List[str]:
    """设计稿：container/element 树，element 的 props.content 是富文本、props.src 是图。"""
    texts: List[str] = []
    state = {"ord": 0}

    def rec(node: Any, npath: str) -> None:
        if isinstance(node, list):
            for i, child in enumerate(node):
                rec(child, _ptr(npath, i))
            return
        if not isinstance(node, dict):
            return
        ntype = node.get("nodeType")
        et = node.get("elementType")
        props = node.get("props") if isinstance(node.get("props"), dict) else {}
        if ntype == "element":
            label = (node.get("name") or "").strip()
            if et == "text":
                content = props.get("content")
                text = "".join(rich_text(p) for p in content or []) if isinstance(content, list) else rich_text(content)
                text = text.replace("\ufeff", "").strip()
                if text:
                    texts.append(text)
                    ctx.add_chunk(
                        _ptr(npath, "props"),
                        text,
                        f"design-text:{side}",
                    )
            elif et == "image":
                src = props.get("src")
                if isinstance(src, str) and src.strip():
                    state["ord"] += 1
                    ctx.add_media(
                        src.strip(), _ptr(_ptr(npath, "props"), "src"), state["ord"],
                        kind=KIND_IMAGE,
                        role=ROLE_ART,
                        evidence=f"立绘设计稿 {side} 端 {npath} 的图片元素（页名「{page_title}」）",
                        status=JUDGED,
                        group_label=f"设计稿/{page_title}/{side}" if page_title else f"设计稿/{side}",
                        label=label or None,
                        node_path=npath,
                        side=side,
                    )
            elif et not in (None, "shape"):
                ctx.note("unknown_block", npath, f"未知设计稿元素类型 {et!r}")
        for key, value in node.items():
            if key in ("props", "style"):
                continue
            if isinstance(value, (list, dict)):
                rec(value, _ptr(npath, key))

    # `children` 这一层不能省：source_path 必须能按 JSON Pointer 回原文取到节点
    rec(doc.get("children"), _ptr(path, "children"))
    return texts


# ---------------------------------------------------------------- 便捷入口


def parse_raw_dir_entry(
    detail_payload: Dict[str, Any],
    *,
    tree_path: Optional[str] = None,
    list_item: Optional[Dict[str, Any]] = None,
    list_updated_at: Optional[int] = None,
) -> ParsedArticle:
    """响应体可能是 `{code,msg,data}` 包一层，也可能直接是 data 段。"""
    data = detail_payload.get("data") if isinstance(detail_payload, dict) and "data" in detail_payload else detail_payload
    if not isinstance(data, dict):
        raise ValueError("detail 响应缺少 data 段")
    code = detail_payload.get("code") if isinstance(detail_payload, dict) else None
    art = parse_detail(
        data,
        tree_path=tree_path,
        list_item=list_item,
        list_updated_at=list_updated_at,
    )
    if code not in (None, 0):
        art.status = STATUS_PARTIAL
        art.notes.append(Note("upstream_code", "", f"上游 code={code}"))
    return art


def iter_media_by_kind(media: Iterable[MediaRef], kinds: Tuple[str, ...]) -> List[MediaRef]:
    return [m for m in media if m.kind in kinds]


def resolve_pointer(doc: Any, pointer: str) -> Tuple[bool, Any]:
    """按 `source_path` 回读原文，返回 `(是否命中, 值)`。

    处理两种非纯 RFC 6901 的情况（都是"字符串里装着 JSON"造成的）：

    - `.../content_live_2d:parsed/...`：先把该字段的 JSON 字符串 `json.loads`，再继续下钻；
    - `.../src` 指向 iframe 串时，命中值就是**原串**（派生 URL 在 `media_ref.url` 里，
      证据在 `role_evidence` 里写明）。

    复核工具与测试都走这个函数，避免「解析器能写、回读走不通」的路径。
    """
    cur = doc
    for raw in [s for s in pointer.split("/") if s]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            try:
                index = int(key)
            except ValueError:
                return False, None
            if index < 0 or index >= len(cur):
                return False, None
            cur = cur[index]
            continue
        if isinstance(cur, dict):
            if key not in cur:
                if key.endswith(_EMBEDDED_MARK):
                    base = key[: -len(_EMBEDDED_MARK)]
                    if base in cur and isinstance(cur[base], str):
                        try:
                            cur = json.loads(cur[base])
                            continue
                        except Exception:
                            return False, None
                return False, None
            cur = cur[key]
            continue
        return False, None
    return True, cur


def _walk_and_collect_paths(doc: Any) -> Iterable[str]:
    """调试辅助：把原文里所有可达指针列出来（不做索引，只在诊断时用）。"""
    def rec(node: Any, path: str) -> Iterable[str]:
        yield path
        if isinstance(node, dict):
            for key, value in node.items():
                yield from rec(value, _ptr(path, key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from rec(value, _ptr(path, index))

    return rec(doc, "")
