# -*- coding: utf-8 -*-
"""stellasora_what 交付层：结构化查询结果 + 显式 intent → 合并转发节点 → send.forward。

职责边界（计划 §5.4 / §6.2 / §6.3 / §6.5 / §12.1）：

- **事实只取自库内原文**：文本来自 `candidate.value` 与 `chunk.body`，数值按项目规范补千分位；
  `source_path`、内部字段键名、`field_notes` 的 blocks JSON、`rejected`/`stats` 一律**不进正文**，
  只留在 `DeliveryResult.details` 供日志。来源统一落成每包末尾一行 `SOURCE_LINE`。
- **整篇 ≠ 目录**：`mode=article` + `shape=whole_article` 时按 candidates 里**已核实的 content_id**
  读同一代连接里的 `chunk`（原文，按 JSON pointer 自然序）与 `media_ref`（`kind∈{image,album}`、
  `role=body_image`、`role_status=judged`），图文交错；`article.catalog` / `article.title` 这类
  伪候选的概览串**绝不冒充正文**。正文为空（media_only）时只诚实说明「原页无可提取文字，只有图片」。
- **片段只带证据相关图**：`shape≠whole_article` 时只发 chunk 原文；配图要求媒体指针落在该片段
  指针的子树内（`media.source_path` 以 `chunk.source_path + "/"` 开头），其余一律不发。
  真实语料实测：judged body_image 160 行里只有 2 行满足该关系 → 片段题基本是纯文字，这是证据边界，
  不是漏发（要看图文请用 whole_article）。
- **媒体用途分流**：整篇/片段只发 `body_image`；`character_art` / `card_banner` 仅在 `mode=media`
  （planner 明确要立绘/卡池）才发；`inline_icon` / `unknown` / `nav_decoration` / `video` /
  `live2d` / `audio` / `background` / `embed` 一律不发。许可范围（可发 GameKee 星塔图）不等于
  「本篇必要图」。
- **体积按两跳真实封套算，取较大者**：一次 `send.forward` 会经过两帧——
  ① Runner→Host `cap.call`；② Host→QQ 网关插件 `plugin.invoke_message_gateway`，其中节点被
  `_session_message_to_dict`/`_component_to_dict` 展开（每节点多 `user_id`/`user_nickname`/
  `user_cardname`/生成的 `message_id`，每段多 `data` 包装，图片多 64 字符 `hash`）。
  因此 `frame_bytes()`（第一跳）与 `gateway_frame_bytes()`（第二跳同形模拟）分开算，
  `packet_bytes()` 取两者较大值加固定未知字段余量 `SECOND_HOP_FIXED_ALLOWANCE`。
  逐节点膨胀实测约 +54 B/节点（100 节点 +5,390 B、1,000 节点 +54,890 B），固定 64 KiB
  余量包不住几千节点，所以按结构逐项计入而不是靠一个常数糊过去。
  上限按目标宿主版本取（16 MiB 默认 / 已核对的 1.4.6~1.4.8 取 32 MiB）。
  不设固定张数、不默认截断：一包放不下就按原序拆多包。
- **状态不造假**：`sent / nothing / partial / unknown / failed` 五态分流；送达未知（异常 / False /
  超时）**立即停止、绝不重发**，同一 plan 只允许交付一次（防迟到重打）。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from what_query import natural_key

logger = logging.getLogger("stellasora.what.delivery")

# ---------------------------------------------------------------- 常量

SOURCE_LINE = "来源：https://www.gamekee.com/stellasora"
"""每个合并转发的最后一行（精确文本，计划 §6.2/§12.1）。"""

FORWARD_NICKNAME = "星塔旅人"
"""与插件 `_FORWARD_NICKNAME` 同值；不 import plugin.py（会拉起宿主依赖）。"""

PLUGIN_ID = "ggsfly.stellasora-plugin"
RPC_METHOD = "cap.call"
CAPABILITY = "send.forward"
PROTOCOL_VERSION = "1.0.0"
RPC_TIMEOUT_MS = 30_000

FRAME_LIMIT_DEFAULT = 16 * 1024 * 1024
"""未知宿主版本的保守上限：16 MiB（v1.3.15 实测值）。"""

FRAME_LIMIT_BY_HOST: Dict[str, int] = {
    "1.3.15": 16 * 1024 * 1024,
    "1.4.6": 32 * 1024 * 1024,
    "1.4.7": 32 * 1024 * 1024,
    "1.4.8": 32 * 1024 * 1024,
}
"""已核对过 `MAX_FRAME_SIZE` 的宿主版本（v1.4.6~1.4.8 = 32 MiB）。"""

SECOND_HOP_FIXED_ALLOWANCE = 4_096
"""第二跳里**无法在插件侧精确知道**的固定字段余量（route/metadata/message_info/
processed_plain_text/received_at 等）。第一、二跳的节点与段膨胀按结构逐项计入，
这里只补这些不定字段，不再假装一个 64 KiB 常数能包住任意节点数。"""

GATEWAY_METHOD = "plugin.invoke_message_gateway"
"""Host→QQ 网关插件的 RPC 方法（`supervisor.invoke_message_gateway`）。"""

GATEWAY_PLUGIN_ID = "message_gateway"
"""网关插件 id 的占位（真实 id 由宿主路由决定，差异仅几十字节）。"""

MAX_IMAGE_BYTES = 8 * 1024 * 1024
"""单图字节上限（与 gamekee 下载上限一致）；超限即明确未发，不塞进包。"""

MAX_SEGMENTS_PER_NODE = 28
"""单节点段数结构护栏（超出在同一包内新起续节点，内容不丢、不截断）。

宿主/QQ 对单节点段数的真实上限未实测，这里只防「一个节点几百段」这种结构风险。
"""

MAX_TEXT_SEGMENT_CHARS = 1200
"""单文字段字符上限；超出按行切段（段之间仍属同一包/节点序列）。"""

SENDABLE_MEDIA_KINDS = ("image", "album")
SENDABLE_ROLES = ("body_image", "character_art", "card_banner")
WHOLE_ARTICLE_ROLES = ("body_image",)

STATUS_SENT = "sent"
STATUS_NOTHING = "nothing"
STATUS_PARTIAL = "partial"
STATUS_UNKNOWN = "unknown"
STATUS_FAILED = "failed"

PLAN_OK = "ok"

_LEVEL_QUALIFIER_RE = re.compile(r"^level:(\d+)$")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")

_FIELD_LABELS: Dict[str, str] = {
    "birthday": "生日",
    "affiliation": "所属",
    "weapon": "武器",
    "salary": "薪资",
    "address": "地址",
    "experience": "经验",
    "skill.talent": "天赋",
    "sigil.main_effect": "主位效果",
    "sigil.sub_effect": "辅位效果",
    "monster.weakness": "弱点",
    "stat.hp": "生命",
    "stat.attack": "攻击",
    "stat.defense": "防御",
    "stat.crit_rate": "暴击率",
    "stat.crit_dmg": "暴击伤害",
    "stat.toughness": "韧性",
}

_ELEMENT_CN = {"water": "水", "fire": "火", "earth": "地", "wind": "风", "light": "光", "dark": "暗"}

_MEDIA_KIND_CN = {
    "image": "图片",
    "album": "图集",
    "video": "视频",
    "audio": "语音",
    "live2d": "Live2D",
    "background": "背景图",
    "embed": "外链",
}

_MEDIA_ROLE_CN = {"body_image": "正文图", "character_art": "立绘", "card_banner": "卡池横幅"}

_ENTITY_KIND_CN = {
    "traveler": "旅人",
    "npc": "NPC",
    "sigil": "秘纹",
    "monster": "怪物",
    "article": "文章",
    "term": "术语",
}

# 只把这些 field_notes 的状态转成用户可见说明；status="info" 是 blocks JSON 取证，
# 属于内部 debug，绝不发 QQ。
_USER_FACING_NOTE_STATUS = ("no_source", "field_absent_at_level", "clarify")


class DeliveryError(RuntimeError):
    """交付层的显式错误（用于「发送前就明确失败」的分流）。"""


class DeliveryImageError(DeliveryError):
    """图片字节不合规（魔数/大小），该图明确未发。"""


# ---------------------------------------------------------------- msgpack 体积计量


def _str_bytes(n: int) -> int:
    if n <= 31:
        return 1
    if n <= 255:
        return 2
    if n <= 65_535:
        return 3
    return 5


def _container_bytes(n: int) -> int:
    if n <= 15:
        return 1
    if n <= 65_535:
        return 3
    return 5


def _int_bytes(value: int) -> int:
    if 0 <= value <= 127:
        return 1
    if -32 <= value < 0:
        return 1
    if value > 0:
        if value <= 0xFF:
            return 2
        if value <= 0xFFFF:
            return 3
        if value <= 0xFFFFFFFF:
            return 5
        return 9
    if value >= -128:
        return 2
    if value >= -32_768:
        return 3
    if value >= -2_147_483_648:
        return 5
    return 9


def msgpack_bytes(value: Any) -> int:
    """返回 `msgpack.packb(value, use_bin_type=True)` 的字节数（逐类型最小头部）。

    msgpack 的 C 实现只会选择最小可用头部，因此本函数对受支持的 Python 值给出的是
    **精确大小**（测试用真实 `msgpack.packb` 对拍断言相等）。不引入 msgpack 依赖：
    只需要长度，不需要真的编码。
    """

    if value is None:
        return 1
    if value is True or value is False:
        return 1
    if isinstance(value, int):
        return _int_bytes(value)
    if isinstance(value, float):
        return 9  # 只有 float64 一种打包形式
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _str_bytes(len(raw)) + len(raw)
    if isinstance(value, (bytes, bytearray)):
        n = len(value)
        return (2 if n <= 255 else 3 if n <= 65_535 else 5) + n
    if isinstance(value, (list, tuple)):
        return _container_bytes(len(value)) + sum(msgpack_bytes(v) for v in value)
    if isinstance(value, dict):
        total = _container_bytes(len(value))
        for key, item in value.items():
            total += msgpack_bytes(str(key)) + msgpack_bytes(item)
        return total
    raise TypeError(f"无法估算 msgpack 体积：{type(value).__name__}")


def frame_bytes(nodes: Sequence[Dict[str, Any]], stream_id: str) -> int:
    """一次 `ctx.send.forward` 在 Runner→Host 帧里的实际 payload 字节数。

    与宿主真实结构同形：`Envelope.model_dump()` + `cap.call` 的
    `{"capability":..., "args":...}`（`maibot_sdk/context.py` 的 `call_capability`），
    MsgPack `use_bin_type=True`（`src/plugin_runtime/protocol/codec.py`）。
    """

    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": 1,
        "message_type": "request",
        "method": RPC_METHOD,
        "plugin_id": PLUGIN_ID,
        "timestamp_ms": 0,
        "timeout_ms": RPC_TIMEOUT_MS,
        "payload": {
            "capability": CAPABILITY,
            "args": {
                "messages": list(nodes),
                "stream_id": stream_id,
                "return_details": False,
            },
        },
        "error": None,
    }
    return msgpack_bytes(envelope)


def _gateway_segments(segments: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 `PluginMessageUtils._component_to_dict` 同形展开段（图片多 hash 与 b64 键）。"""

    out: List[Dict[str, Any]] = []
    for segment in segments:
        kind = str(segment.get("type") or "")
        if kind == "image":
            out.append(
                {
                    "type": "image",
                    "data": str(segment.get("data") or ""),
                    "hash": "0" * 64,  # 真实值由 Host 算 sha256；长度固定 64
                    "binary_data_base64": str(segment.get("content") or ""),
                }
            )
        else:
            out.append({"type": kind or "text", "data": str(segment.get("content") or segment.get("data") or "")})
    return out


def _gateway_forward_nodes(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 `_forward_component_to_dict` 同形展开节点（多 user_id/nickname/cardname/message_id）。"""

    out: List[Dict[str, Any]] = []
    for index, node in enumerate(nodes):
        out.append(
            {
                "user_id": "",
                "user_nickname": str(node.get("nickname") or ""),
                "user_cardname": "",
                "message_id": f"plugin_forward_{index}",
                "content": _gateway_segments(list(node.get("segments") or [])),
            }
        )
    return out


def gateway_frame_bytes(nodes: Sequence[Dict[str, Any]], stream_id: str) -> int:
    """第二跳（Host→QQ 网关插件）payload 的同形尺寸上界。

    结构取自 `src/platform_io/drivers/plugin_driver.py` 的出站路径与
    `src/plugin_runtime/host/message_utils.py` 的 `_session_message_to_dict` /
    `_component_to_dict` / `_forward_component_to_dict`；节点/段的展开逐项计入，
    只对 route/metadata/message_info 这类插件侧不可知字段加固定余量。
    """

    message = {
        "message_id": "plugin_forward_0",
        "timestamp": "1795000000.0",
        "platform": "qq",
        "account_id": "",
        "scope": "",
        "session_id": stream_id,
        "message_info": {
            "user_info": {"user_id": "", "user_nickname": "", "user_cardname": ""},
            "group_info": {"group_id": "", "group_name": ""},
            "additional_config": {},
        },
        "raw_message": [{"type": "forward", "data": _gateway_forward_nodes(nodes)}],
        "is_mentioned": False,
        "is_at": False,
        "is_emoji": False,
        "is_picture": True,
        "is_command": False,
        "is_notify": False,
        "processed_plain_text": "[转发消息]",
    }
    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": 1,
        "message_type": "request",
        "method": GATEWAY_METHOD,
        "plugin_id": GATEWAY_PLUGIN_ID,
        "timestamp_ms": 0,
        "timeout_ms": RPC_TIMEOUT_MS,
        "payload": {
            "component_name": "message_gateway",
            "args": {
                "message": message,
                "route": {"platform": "qq", "account_id": "", "scope": ""},
                "metadata": {},
            },
        },
        "error": None,
    }
    return msgpack_bytes(envelope) + SECOND_HOP_FIXED_ALLOWANCE


def packet_bytes(nodes: Sequence[Dict[str, Any]], stream_id: str) -> int:
    """一次 `send.forward` 的保守占用：两跳取较大者（每跳各自独立成帧）。"""

    return max(frame_bytes(nodes, stream_id), gateway_frame_bytes(nodes, stream_id))


def resolve_frame_limit(host_version: Optional[str] = None) -> Tuple[int, bool, str]:
    """目标宿主的单帧上限。返回 `(上限字节, 是否保守值, 说明)`。"""

    version = (host_version if host_version is not None else os.environ.get("MAIBOT_HOST_VERSION") or "").strip()
    if version in FRAME_LIMIT_BY_HOST:
        return FRAME_LIMIT_BY_HOST[version], False, f"宿主 {version} 已核对单帧上限 {FRAME_LIMIT_BY_HOST[version]}"
    return (
        FRAME_LIMIT_DEFAULT,
        True,
        f"宿主版本 {version or '未知'} 未核对，按 16 MiB 保守值",
    )


# ---------------------------------------------------------------- 文本规范化


def format_thousands(value: str) -> str:
    """数值补千分位（项目规范：完整千分位、禁「万」缩写）；非纯数值原文不动。"""

    text = (value or "").strip()
    if not _NUMBER_RE.match(text):
        return text
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    integer, _, fraction = text.partition(".")
    if len(integer) <= 3:
        return sign + text
    groups = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)
    out = sign + ",".join(groups)
    if fraction:
        out += "." + fraction
    return out


def _clean_text(text: str) -> str:
    """去掉首尾空白，避免转发节点出现空行尾巴。"""

    return (text or "").strip()


def _text_pieces(text: str, *, max_chars: int = MAX_TEXT_SEGMENT_CHARS) -> List[str]:
    """把一段原文切成若干文字段：整段只净化一次，片段逐字保留（含换行）。

    切片规则：
    - 首尾空白只在**原整段**上净化一次；片段本身不再 strip，否则会吃掉行尾换行、
      把「拼回去等于原文」破坏掉。
    - 优先在换行处切（`splitlines(keepends=True)` 把换行留在前一段里），
      这样 `"".join(pieces)` 与原整段逐字相等，切片处不新加也不丢换行。
    - 单行仍超 `max_chars` 时按字符硬切，切口不加/删任何字符。
    """

    cleaned = _clean_text(text)
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]
    pieces: List[str] = []
    buffer = ""
    for line in cleaned.splitlines(keepends=True):
        if len(buffer) + len(line) <= max_chars:
            buffer += line
            continue
        if buffer:
            pieces.append(buffer)
            buffer = ""
        while len(line) > max_chars:
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        buffer = line
    if buffer:
        pieces.append(buffer)
    return pieces


def _field_label(field_key: Optional[str]) -> str:
    if not field_key:
        return ""
    if field_key in _FIELD_LABELS:
        return _FIELD_LABELS[field_key]
    element = re.match(r"stat\.element_damage\.(\w+)", field_key)
    if element:
        return f"{_ELEMENT_CN.get(element.group(1), element.group(1))}系伤害"
    if field_key.startswith("term."):
        return "术语"
    if field_key.startswith("media_count."):
        return "媒体数量"
    if field_key.startswith("article."):
        # catalog / title 伪候选只表示「有条目」，不当正文也不当字段展示
        return ""
    return field_key


def _display_qualifier(qualifier: str) -> str:
    match = _LEVEL_QUALIFIER_RE.match((qualifier or "").strip())
    if match:
        return f"{match.group(1)}级"
    return (qualifier or "").strip()


def _safe_filename(url: str, *, fallback: str = "image") -> str:
    name = (url or "").rsplit("/", 1)[-1]
    name = name.split("?", 1)[0].split("#", 1)[0]
    name = re.sub(r"[^0-9A-Za-z._-]", "_", name).strip("._")
    return name or fallback


def _sanitize_message(text: Any, *, limit: int = 200) -> str:
    """把 result.message 收成用户可读的一行：剔除 JSON 取证块、路径形态串、换行。"""

    raw = _clean_text(str(text or ""))
    if not raw:
        return ""
    # 只剔除带引号的 JSON 取证串；`[1, 90]` 这种「可用等级」是给用户看的信息，保留
    raw = re.sub(r'\{[^{}]*"[^{}]*\}', "", raw)
    raw = re.sub(r'\[[^\[\]]*"[^\[\]]*\]', "", raw)
    raw = raw.replace("\n", " ").replace("\r", " ")
    # 任何以 / 开头或以 / 分隔的指针样串都不进用户文本
    raw = re.sub(r"(^|[（(，,；;：: ])(/[0-9A-Za-z_\-./]+)", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if len(raw) > limit:
        raw = raw[: limit - 1].rstrip() + "…"
    return raw


# ---------------------------------------------------------------- 计划模型


@dataclass(slots=True)
class TextItem:
    """一段要发出的原文文本。"""

    text: str
    origin: str = ""  # 内部来源说明（只进日志）


@dataclass(slots=True)
class ImageItem:
    """一张要发出的图片（按离线证据的独立位置，同 URL 不同位置各算一条）。"""

    content_id: int
    source_path: str
    ordinal: int
    url: str
    media_kind: str
    role: str
    filename: str
    origin: str = ""


@dataclass(slots=True)
class DeliveryGroup:
    """一个候选/来源分组：先文字，再该组的图片；一组 = 一个转发节点（放不下则续节点）。"""

    label: str
    content_id: Optional[int] = None
    items: List[Any] = field(default_factory=list)
    note: str = ""

    def count_images(self) -> int:
        return sum(1 for item in self.items if isinstance(item, ImageItem))


@dataclass(slots=True)
class DeliveryPlan:
    """`prepare_delivery` 的产物：纯本地组装结果，不含任何图片字节。"""

    kind: str  # ok / not_found / clarify / no_source / invalid_intent
    message: str  # 面向用户的短语（已剔除内部细节）
    groups: List[DeliveryGroup] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    intent_mode: str = ""
    shape: str = ""
    _claimed: bool = False  # 同一 plan 只允许交付一次（防迟到重打）

    def image_count(self) -> int:
        return sum(g.count_images() for g in self.groups)


@dataclass(slots=True)
class DeliveryResult:
    status: str
    success: bool
    stop_after_execution: bool
    user_text: str = ""
    error: str = ""
    packs_planned: int = 0
    packs_sent: int = 0
    images_planned: int = 0
    images_sent: int = 0
    images_failed: int = 0
    skipped_reasons: List[str] = field(default_factory=list)
    unsent_groups: int = 0
    partial_delivery: bool = False
    frame_limit: int = FRAME_LIMIT_DEFAULT
    frame_reserve: int = SECOND_HOP_FIXED_ALLOWANCE
    conservative: bool = True
    budget_note: str = ""
    frames: List[int] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "success": self.success,
            "stop_after_execution": self.stop_after_execution,
            "user_text": self.user_text,
            "error": self.error,
            "packs_planned": self.packs_planned,
            "packs_sent": self.packs_sent,
            "images_planned": self.images_planned,
            "images_sent": self.images_sent,
            "images_failed": self.images_failed,
            "skipped_reasons": self.skipped_reasons,
            "unsent_groups": self.unsent_groups,
            "partial_delivery": self.partial_delivery,
            "frame_limit": self.frame_limit,
            "frame_reserve": self.frame_reserve,
            "conservative": self.conservative,
            "budget_note": self.budget_note,
            "frames": self.frames,
            "details": self.details,
        }


# ---------------------------------------------------------------- 计划构建


def _terminal_plan(kind: str, message: str, note: str, *, mode: str = "", shape: str = "") -> DeliveryPlan:
    return DeliveryPlan(
        kind=kind,
        message=message,
        notes=[note] if note else [],
        intent_mode=mode,
        shape=shape,
    )


_CLIENT_LOCATOR_RE = re.compile(r"(^|\s)(https?://|//)", re.IGNORECASE)
_PATHISH_RE = re.compile(r"[A-Za-z0-9_\-]+/[A-Za-z0-9_\-./]+|/[0-9]+/")


def _client_supplied_locator(intent: Dict[str, Any]) -> str:
    """planner 不允许带 URL 或路径：任何字符串值像 URL/路径就拒绝，绝不采用其值。"""

    for key, value in _walk_scalars(intent):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        if _CLIENT_LOCATOR_RE.search(text):
            return f"{key}={text[:60]}"
        if text.startswith("/") and len(text) > 1:
            return f"{key}={text[:60]}"
        if _PATHISH_RE.search(text) and "." in text.rsplit("/", 1)[-1]:
            return f"{key}={text[:60]}"
    return ""


def _walk_scalars(value: Any, prefix: str = "intent"):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_scalars(item, f"{prefix}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_scalars(item, f"{prefix}[{index}]")
    else:
        yield prefix, value


def _candidate_row(candidate: Any, key: str, default: Any = None) -> Any:
    return getattr(candidate, key, default)


def prepare_delivery(conn: sqlite3.Connection, result: Any, intent: Any) -> DeliveryPlan:
    """把 `QueryResult` + 显式 intent 组装成可发送的分组计划（只读本地库，无网络）。

    - 事实/片段/整篇/媒体/目录五种输出按 `intent.mode` + `intent.shape` 分流；
    - 不做关键词判断（插件看不到问句），该不该发由 planner 的 intent 决定；
    - 群库连接必须是调用方「整次请求固定一代」的那一条，本函数只读。
    """

    status = str(_candidate_row(result, "status", "") or "")
    mode = str(_candidate_row(result, "mode", "") or "")
    intent_obj = intent if isinstance(intent, dict) else {}
    shape = str(intent_obj.get("shape") or "single").strip() or "single"

    if not isinstance(intent, dict):
        return _terminal_plan("invalid_intent", "", "intent 必须是对象", mode=mode)
    locator = _client_supplied_locator(intent_obj)
    if locator:
        return _terminal_plan(
            "invalid_intent", "", f"intent 携带了 URL/路径，已拒绝采用：{locator}", mode=mode, shape=shape
        )
    if status != "ok":
        if status in ("not_found", "clarify", "no_source"):
            return _terminal_plan(status, _user_text_for(result, status), "", mode=mode, shape=shape)
        return _terminal_plan(
            "invalid_intent", "", f"查询结果状态异常：{status or '<empty>'}", mode=mode, shape=shape
        )

    candidates = list(_candidate_row(result, "candidates", []) or [])
    if not candidates:
        return _terminal_plan("invalid_intent", "", "status=ok 但 candidates 为空", mode=mode, shape=shape)

    builders = {
        "field": _groups_field,
        "media": _groups_media,
        "catalog": _groups_catalog,
    }
    notes: List[str] = []
    if mode == "article":
        groups = _groups_article(conn, result, intent_obj, shape, notes)
    elif mode not in builders:
        return _terminal_plan(
            "invalid_intent", "", f"未知 mode：{mode or '<empty>'}", mode=mode, shape=shape
        )
    else:
        groups = builders[mode](conn, result, intent_obj, notes)

    if not groups:
        message = _sanitize_message(_candidate_row(result, "message", ""))
        return DeliveryPlan(
            kind="nothing",
            message=message,
            notes=notes + ["结果没有可发送的合格内容"],
            intent_mode=mode,
            shape=shape,
            stats={"candidates": len(candidates)},
        )

    plan = DeliveryPlan(
        kind=PLAN_OK,
        message="",
        groups=groups,
        notes=notes,
        intent_mode=mode,
        shape=shape,
        stats={
            "candidates": len(candidates),
            "groups": len(groups),
            "images": sum(g.count_images() for g in groups),
            "texts": sum(1 for g in groups for item in g.items if isinstance(item, TextItem)),
        },
    )
    return plan


def _user_text_for(result: Any, status: str) -> str:
    if status == "not_found":
        extra = _sanitize_message(_also_named_line(result))
        return "没查到" + (f"；{extra}" if extra else "")
    return _sanitize_message(_candidate_row(result, "message", ""))


def _also_named_line(result: Any) -> str:
    """同名多候选：只列真实名称与种类（不带 id / 路径），让用户自己收窄。"""

    rows = list(_candidate_row(result, "also_named", []) or [])
    if not rows:
        return ""
    parts: List[str] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("canonical_name") or row.get("title") or "").strip()
        if not name:
            continue
        kind = _ENTITY_KIND_CN.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
        parts.append(f"{name}（{kind}）" if kind else name)
    if not parts:
        return ""
    return "同名条目：" + "、".join(parts)


# ---------------------------------------------------------------- mode=field


def _fact_group_label(title: str, entity_name: Optional[str]) -> str:
    title = _clean_text(title) or "条目"
    entity = _clean_text(entity_name or "")
    if entity and entity != title:
        return f"【{entity}】{title}"
    return f"【{title}】"


def _fact_line(candidate: Any) -> str:
    label = _field_label(_candidate_row(candidate, "field_key"))
    qualifier = _display_qualifier(_candidate_row(candidate, "qualifier", ""))
    value = format_thousands(_candidate_row(candidate, "value", "") or "")
    if not value:
        return ""
    if label and qualifier:
        return f"{label}（{qualifier}）：{value}"
    if label:
        return f"{label}：{value}"
    if qualifier:
        return f"{qualifier}：{value}"
    return value


def _groups_field(
    conn: sqlite3.Connection, result: Any, intent: Dict[str, Any], notes: List[str]
) -> List[DeliveryGroup]:
    """字段题：纯文字，一组 = 一个来源条目；不附任何图片。"""

    groups: Dict[Tuple[Any, str], DeliveryGroup] = {}
    order: List[Tuple[Any, str]] = []
    skipped = 0
    for candidate in _candidate_row(result, "candidates", []) or []:
        if _candidate_row(candidate, "kind") != "fact":
            skipped += 1
            continue
        key = (_candidate_row(candidate, "content_id"), _clean_text(_candidate_row(candidate, "title") or ""))
        group = groups.get(key)
        if group is None:
            group = DeliveryGroup(
                label=_fact_group_label(_candidate_row(candidate, "title") or "", _candidate_row(candidate, "entity_name")),
                content_id=_candidate_row(candidate, "content_id"),
            )
            groups[key] = group
            order.append(key)
        line = _fact_line(candidate)
        if line:
            group.items.append(TextItem(text=line, origin=_candidate_row(candidate, "source_path", "") or ""))
    if skipped:
        notes.append(f"{skipped} 个非事实候选未进入字段题交付")

    out = [groups[key] for key in order]
    notes_text = _user_facing_notes(result)
    if notes_text:
        if out:
            out[-1].items.append(TextItem(text="；".join(notes_text), origin="field_notes"))
        else:
            out.append(DeliveryGroup(label="【说明】", items=[TextItem(text="；".join(notes_text))]))
    return out


def _user_facing_notes(result: Any) -> List[str]:
    """只取用户该知道的字段说明；status=info（blocks JSON 取证）绝不外发。"""

    out: List[str] = []
    for note in _candidate_row(result, "field_notes", []) or []:
        if not isinstance(note, dict):
            continue
        if str(note.get("status") or "") not in _USER_FACING_NOTE_STATUS:
            continue
        text = _sanitize_message(note.get("note"))
        if text and text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------- mode=article


def _article_row(conn: sqlite3.Connection, content_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT content_id,title,title_alt,availability,entity_kind,entity_name,schema,category"
        " FROM article WHERE content_id=?",
        (content_id,),
    ).fetchone()


def _article_title(row: sqlite3.Row) -> str:
    return _clean_text(row["title"] or row["title_alt"] or "") or "条目"


def _whole_article_groups(
    conn: sqlite3.Connection, content_id: int, notes: List[str]
) -> Optional[DeliveryGroup]:
    """按核实 content_id 读正文与正文章节配图，指针原序图文交错。"""

    row = _article_row(conn, content_id)
    if row is None:
        notes.append(f"条目 {content_id} 在库中不存在，未发送")
        return None
    title = _article_title(row)
    chunks = conn.execute(
        "SELECT source_path,heading,block_kind,body FROM chunk WHERE content_id=? ORDER BY chunk_id",
        (content_id,),
    ).fetchall()
    media = conn.execute(
        "SELECT source_path,ordinal,kind,role,role_status,url FROM media_ref"
        " WHERE content_id=? AND kind IN (%s) AND role IN (%s) AND role_status='judged'"
        " ORDER BY source_path,ordinal"
        % (",".join("?" for _ in SENDABLE_MEDIA_KINDS), ",".join("?" for _ in WHOLE_ARTICLE_ROLES)),
        (content_id, *SENDABLE_MEDIA_KINDS, *WHOLE_ARTICLE_ROLES),
    ).fetchall()

    entries: List[Tuple[Any, int, Any]] = []
    seen_bodies: set = set()
    deduped = 0
    for row_chunk in chunks:
        body = _clean_text(row_chunk["body"])
        if not body:
            continue
        if body in seen_bodies:
            # 同一页 pc/mobile 双版面会给出完全相同的正文串：只出现一次，不改写不合并语义
            deduped += 1
            continue
        seen_bodies.add(body)
        path = row_chunk["source_path"] or ""
        entries.append((natural_key(path), 0, TextItem(text=body, origin=path)))
    for row_media in media:
        path = row_media["source_path"] or ""
        entries.append(
            (
                natural_key(path),
                1,
                ImageItem(
                    content_id=content_id,
                    source_path=path,
                    ordinal=int(row_media["ordinal"] or 0),
                    url=row_media["url"] or "",
                    media_kind=row_media["kind"] or "",
                    role=row_media["role"] or "",
                    filename=_safe_filename(row_media["url"] or ""),
                    origin=path,
                ),
            )
        )
    if deduped:
        notes.append(f"条目 {content_id} 有 {deduped} 段双版面重复正文已跳过（相同原文串）")
    if not entries:
        notes.append(f"条目 {content_id} 没有可发送的正文或配图")
        return None

    entries.sort(key=lambda item: (item[0], item[1]))
    group = DeliveryGroup(
        label=f"【{title}】",
        content_id=content_id,
        items=[item for _, _, item in entries],
    )
    if not chunks:
        group.note = "原页无可提取文字，只有图片"
        notes.append(f"条目 {content_id} 无可提取文字（availability={row['availability']}），按图片页说明")
    return group


def _related_media(
    conn: sqlite3.Connection, candidate: Any
) -> List[sqlite3.Row]:
    """片段题只能带「证据相关图」：媒体指针落在该片段指针的子树内。"""

    pointer = str(_candidate_row(candidate, "source_path", "") or "")
    content_id = _candidate_row(candidate, "content_id")
    if pointer in ("", "/") or content_id is None:
        return []
    rows = conn.execute(
        "SELECT source_path,ordinal,kind,role,role_status,url FROM media_ref"
        " WHERE content_id=? AND kind IN (%s) AND role IN (%s) AND role_status='judged'"
        " ORDER BY source_path,ordinal"
        % (",".join("?" for _ in SENDABLE_MEDIA_KINDS), ",".join("?" for _ in WHOLE_ARTICLE_ROLES)),
        (content_id, *SENDABLE_MEDIA_KINDS, *WHOLE_ARTICLE_ROLES),
    ).fetchall()
    return [row for row in rows if (row["source_path"] or "").startswith(pointer + "/")]


def _groups_article(
    conn: sqlite3.Connection, result: Any, intent: Dict[str, Any], shape: str, notes: List[str]
) -> List[DeliveryGroup]:
    candidates = list(_candidate_row(result, "candidates", []) or [])

    if shape == "whole_article":
        groups: List[DeliveryGroup] = []
        seen_ids: List[int] = []
        for candidate in candidates:
            content_id = _candidate_row(candidate, "content_id")
            if content_id is None or content_id in seen_ids:
                continue
            seen_ids.append(content_id)
            group = _whole_article_groups(conn, int(content_id), notes)
            if group is not None:
                groups.append(group)
        if not groups and seen_ids:
            notes.append("整篇请求没有任何可发送正文/配图")
        return groups

    groups_map: Dict[Tuple[Any, str], DeliveryGroup] = {}
    order: List[Tuple[Any, str]] = []
    seen_media: Dict[Tuple[Any, str], set] = {}
    skipped = 0
    for candidate in candidates:
        if _candidate_row(candidate, "kind") != "chunk":
            skipped += 1
            continue
        field_key = str(_candidate_row(candidate, "field_key") or "")
        if field_key.startswith("article."):
            # 目录/标题伪候选不是正文，绝不冒充全文
            skipped += 1
            continue
        body = _clean_text(_candidate_row(candidate, "value") or "")
        if not body:
            skipped += 1
            continue
        key = (_candidate_row(candidate, "content_id"), _clean_text(_candidate_row(candidate, "title") or ""))
        group = groups_map.get(key)
        if group is None:
            group = DeliveryGroup(
                label=_fact_group_label(_candidate_row(candidate, "title") or "", _candidate_row(candidate, "entity_name")),
                content_id=_candidate_row(candidate, "content_id"),
            )
            groups_map[key] = group
            order.append(key)
            seen_media[key] = set()
        group.items.append(TextItem(text=body, origin=_candidate_row(candidate, "source_path", "") or ""))
        for row in _related_media(conn, candidate):
            signature = (row["source_path"] or "", int(row["ordinal"] or 0))
            if signature in seen_media[key]:
                continue
            seen_media[key].add(signature)
            group.items.append(
                ImageItem(
                    content_id=_candidate_row(candidate, "content_id"),
                    source_path=row["source_path"] or "",
                    ordinal=int(row["ordinal"] or 0),
                    url=row["url"] or "",
                    media_kind=row["kind"] or "",
                    role=row["role"] or "",
                    filename=_safe_filename(row["url"] or ""),
                    origin=row["source_path"] or "",
                )
            )
    if skipped:
        notes.append(f"{skipped} 个片段候选不可作为正文（目录/标题命中或空正文），未发送")
    return [groups_map[key] for key in order]


# ---------------------------------------------------------------- mode=media


def _groups_media(
    conn: sqlite3.Connection, result: Any, intent: Dict[str, Any], notes: List[str]
) -> List[DeliveryGroup]:
    wanted_groups = intent.get("groups") or []
    if isinstance(wanted_groups, str):
        wanted_groups = [wanted_groups]
    wanted = [str(g) for g in wanted_groups]

    groups_map: Dict[Tuple[Any, str], DeliveryGroup] = {}
    order: List[Tuple[Any, str]] = []
    skipped = 0
    for candidate in _candidate_row(result, "candidates", []) or []:
        if _candidate_row(candidate, "kind") != "media":
            skipped += 1
            continue
        if str(_candidate_row(candidate, "media_kind") or "") not in SENDABLE_MEDIA_KINDS:
            skipped += 1
            continue
        if str(_candidate_row(candidate, "role") or "") not in SENDABLE_ROLES:
            skipped += 1
            continue
        if str(_candidate_row(candidate, "role_status") or "") != "judged":
            skipped += 1
            continue
        group_label = _candidate_row(candidate, "group_label")
        if wanted and group_label not in wanted:
            skipped += 1
            continue
        key = (_candidate_row(candidate, "content_id"), group_label or "")
        group = groups_map.get(key)
        if group is None:
            role_cn = _MEDIA_ROLE_CN.get(str(_candidate_row(candidate, "role") or ""), "")
            label_tail = role_cn
            if group_label and group_label != role_cn:
                label_tail = f"{role_cn}·{group_label}" if role_cn else str(group_label)
            label = f"【{_clean_text(_candidate_row(candidate, 'title') or '') or '条目'}】" + label_tail
            group = DeliveryGroup(
                label=label,
                content_id=_candidate_row(candidate, "content_id"),
                note="已判定用途的媒体",
            )
            groups_map[key] = group
            order.append(key)
        group.items.append(
            ImageItem(
                content_id=_candidate_row(candidate, "content_id"),
                source_path=_candidate_row(candidate, "source_path", "") or "",
                ordinal=int(_candidate_row(candidate, "ordinal", 0) or 0),
                url=_candidate_row(candidate, "url", "") or "",
                media_kind=_candidate_row(candidate, "media_kind", "") or "",
                role=_candidate_row(candidate, "role", "") or "",
                filename=_safe_filename(_candidate_row(candidate, "url", "") or ""),
                origin=_candidate_row(candidate, "source_path", "") or "",
            )
        )
    if skipped:
        notes.append(f"{skipped} 个媒体候选不合发送条件（非 image/album、用途未判定或分组不符）")
    return [groups_map[key] for key in order]


# ---------------------------------------------------------------- mode=catalog


def _groups_catalog(
    conn: sqlite3.Connection, result: Any, intent: Dict[str, Any], notes: List[str]
) -> List[DeliveryGroup]:
    """目录只列事实（数量/字段/等级/分组），不下载、不编造。"""

    lines: List[str] = []
    for candidate in _candidate_row(result, "candidates", []) or []:
        field_key = str(_candidate_row(candidate, "field_key") or "")
        if not field_key.startswith("media_count."):
            continue
        kind = field_key.split(".", 1)[1]
        value = _candidate_row(candidate, "value", "") or ""
        label = _MEDIA_KIND_CN.get(kind, kind)
        lines.append(f"{label} {format_thousands(value)} 项")

    fields = [_field_label(str(key)) for key in (_candidate_row(result, "available_fields", []) or [])]
    fields = [item for item in fields if item]
    if fields:
        lines.append("可用字段：" + "、".join(sorted(set(fields))))
    levels = _candidate_row(result, "available_levels", []) or []
    if levels:
        lines.append("可用等级：" + "/".join(str(level) for level in levels) + " 级")
    groups = [_clean_text(str(group)) for group in (_candidate_row(result, "available_groups", []) or [])]
    groups = [group for group in groups if group]
    if groups:
        lines.append("可用分组：" + "、".join(groups))

    if not lines:
        notes.append("目录没有可列出的事实")
        return []
    title = _clean_text(_candidate_row(result, "subject_name", "") or "") or "目录"
    return [DeliveryGroup(label=f"【{title}】", items=[TextItem(text="\n".join(lines))])]


# ---------------------------------------------------------------- 发送：分包与状态


class _SendAborted(Exception):
    """发送中止（送达未知）；携带已发生的统计，不用于掩盖异常。"""

    def __init__(self, error: str, *, groups_left: int) -> None:
        super().__init__(error)
        self.error = error
        self.groups_left = groups_left


class _PacketBuilder:
    """按两跳真实占用累积一个合并转发包；装不下就交给调用方先发再续。

    计量用 `packet_bytes()`（第一跳与 Host→网关第二跳取较大者），不是只看节点列表：
    节点数一多，第二跳的 user_id/nickname/message_id/hash 展开会先撞上限。

    **分组 = 节点**：`begin_group()` 之后的第一段一定新开节点（`_force_new_node`），
    计划里"不同候选分节点"的划分与真实节点划分一致；`_probe_nodes()` 用同一套落位规则
    预演，因此"计量"和"实际放进去的节点"不会各算一套。
    """

    def __init__(self, *, stream_id: str, frame_limit: int, log: logging.Logger) -> None:
        self._nodes: List[Dict[str, Any]] = []
        self._stream_id = stream_id
        self._limit = frame_limit
        self._log = log
        self._force_new_node = False

    def _empty_node(self) -> Dict[str, Any]:
        return {"nickname": FORWARD_NICKNAME, "segments": []}

    def begin_group(self) -> None:
        """标记下一个放入的段属于新分组：强开新节点，不与上一个候选挤在同一个节点。"""

        self._force_new_node = True

    def _place(
        self, nodes: List[Dict[str, Any]], segments: Sequence[Dict[str, Any]], *, force_new: bool
    ) -> None:
        """把若干段按同一落位规则追加到 `nodes`（分组首段落在新节点）。"""

        for segment in segments:
            if force_new or not nodes or len(nodes[-1]["segments"]) >= MAX_SEGMENTS_PER_NODE:
                nodes.append(self._empty_node())
                force_new = False
            nodes[-1]["segments"].append(segment)

    def _probe_nodes(self, segments: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """预计加入 `segments` 并补上包尾落款后的完整节点列表（用于计量）。"""

        nodes: List[Dict[str, Any]] = [
            {"nickname": FORWARD_NICKNAME, "segments": list(node["segments"])} for node in self._nodes
        ]
        self._place(nodes, segments, force_new=self._force_new_node)
        if not nodes:
            nodes.append(self._empty_node())
        if len(nodes[-1]["segments"]) >= MAX_SEGMENTS_PER_NODE:
            nodes.append(self._empty_node())
        # seal() 会在最后一段补落款，这里必须一起计入，否则会把包封到超限
        nodes[-1] = {
            "nickname": FORWARD_NICKNAME,
            "segments": list(nodes[-1]["segments"]) + [{"type": "text", "content": SOURCE_LINE}],
        }
        return nodes

    def _would_fit(self, segments: Sequence[Dict[str, Any]]) -> bool:
        return packet_bytes(self._probe_nodes(segments), self._stream_id) <= self._limit

    def add_all(self, segments: Sequence[Dict[str, Any]]) -> bool:
        """整组原子放入：要么这几段一起进包，要么一段都不进（不会留下只有标题的残包）。"""

        if not segments:
            return True
        if not self._would_fit(segments):
            return False
        self._place(self._nodes, segments, force_new=self._force_new_node)
        self._force_new_node = False
        return True

    def add(self, segment: Dict[str, Any]) -> bool:
        return self.add_all([segment])

    def has_content(self) -> bool:
        return bool(self._nodes) and any(node["segments"] for node in self._nodes)

    def content_bytes(self) -> int:
        return packet_bytes(self._nodes, self._stream_id) if self._nodes else 0

    def seal(self, source_line: str) -> List[Dict[str, Any]]:
        """补上包尾落款并交出节点列表（每包恰好一次、最后一段）。"""

        if not self._nodes:
            raise DeliveryError("空包不能封口")
        if len(self._nodes[-1]["segments"]) >= MAX_SEGMENTS_PER_NODE:
            self._nodes.append(self._empty_node())
        self._nodes[-1]["segments"].append({"type": "text", "content": source_line})
        nodes = self._nodes
        self._nodes = []
        self._force_new_node = False
        return nodes


def _text_segment(text: str) -> Dict[str, Any]:
    """文字段：逐字落成，不再二次 strip（净化只对整段原文做一次）。

    `_text_pieces()` 产出的片段必须原样进节点，否则行尾换行被吃掉、切片处字符被改写；
    标题/落款本身就是单行已净化文本，直接落也没有副作用。
    """

    return {"type": "text", "content": text if isinstance(text, str) else str(text)}


def _image_segment(base64_text: str, filename: str) -> Dict[str, Any]:
    return {"type": "image", "content": base64_text, "filename": filename}


def _validate_image_bytes(data: Any) -> bytes:
    """图片字节校验：PNG/JPEG/WEBP 魔数 + 大小上限；拦截页与坏图明确失败。"""

    if not isinstance(data, (bytes, bytearray)):
        raise DeliveryImageError(f"取图结果不是字节：{type(data).__name__}")
    raw = bytes(data)
    if len(raw) < 12:
        raise DeliveryImageError(f"图片响应过短（{len(raw)} 字节，可能是拦截页）")
    if len(raw) > MAX_IMAGE_BYTES:
        raise DeliveryImageError(f"图片 {len(raw)} 字节超过单图上限 {MAX_IMAGE_BYTES}")
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw
    if raw[:3] == b"\xff\xd8\xff":
        return raw
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return raw
    head = raw[:64].decode("ascii", errors="replace")
    raise DeliveryImageError(f"响应非图片（疑似拦截页）：head={head!r}")


async def _load_image_bytes(load_image: Optional[Callable[[str], Any]], url: str) -> bytes:
    """取图：同步函数进线程池，异步函数直接 await；不做任何重试。"""

    if load_image is None:
        raise DeliveryError("未注入取图函数 load_image，无法发送图片")
    if inspect.iscoroutinefunction(load_image):
        value = await load_image(url)
    else:
        value = await asyncio.to_thread(load_image, url)
    if inspect.isawaitable(value):
        value = await value
    return _validate_image_bytes(value)


def _truthy_sent(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("sent") or value.get("success"))
    return bool(value)


async def _send_packet(
    send_forward: Callable[..., Any],
    nodes: List[Dict[str, Any]],
    stream_id: str,
    log: logging.Logger,
) -> bool:
    """一次 forward。返回 True 只代表「Host 明确回执已发送」，不代表 QQ 已读。"""

    try:
        outcome = send_forward(nodes, stream_id)
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 异常=送达未知，绝不重发
        log.error("what 合并转发异常（送达未知，不重发）: stream=%s err=%s", stream_id, exc, exc_info=True)
        return False
    sent = _truthy_sent(outcome)
    if not sent:
        log.error("what 合并转发回执未确认送达（不重发）: stream=%s outcome=%r", stream_id, outcome)
    return sent


async def deliver_what(
    *,
    stream_id: str,
    send_forward: Callable[..., Any],
    plan: Optional[DeliveryPlan] = None,
    conn: Optional[sqlite3.Connection] = None,
    result: Any = None,
    intent: Any = None,
    load_image: Optional[Callable[[str], Any]] = None,
    budget_bytes: Optional[int] = None,
    host_version: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> DeliveryResult:
    """按原序发送计划内的全部分组，返回五态交付结果。

    - `budget_bytes` 传宿主原始 `MAX_FRAME_SIZE`（32/16 MiB）。内部按
      `packet_bytes()` 判定：第一跳（Runner→Host）与第二跳（Host→网关）各算一次，
      取较大值，再对第二跳的不定字段加 `SECOND_HOP_FIXED_ALLOWANCE`。
      为 None 时按 `host_version`（或环境变量 `MAIBOT_HOST_VERSION`）解析，
      未知版本按 16 MiB 保守并标记。
    - 单图/取图/构造的任何异常都只会让「这一项」明确未发（partial），不会伪造成功；
      forward 异常或回执 False 直接判送达未知、停止后续、不重发。
    """

    log = logger or _default_logger()
    if plan is None:
        if conn is None or result is None:
            return _failed_result(
                "缺少 plan，或缺少 conn/result 无法组装计划",
                budget_note="未解析（缺少计划）",
            )
        plan = await asyncio.to_thread(prepare_delivery, conn, result, intent)

    frame_limit, conservative, budget_note = (
        (int(budget_bytes), True, f"调用方指定单帧上限 {int(budget_bytes)}")
        if budget_bytes is not None
        else resolve_frame_limit(host_version)
    )
    if frame_limit <= SECOND_HOP_FIXED_ALLOWANCE:
        return _failed_result(
            f"发送预算 {frame_limit} 不大于第二跳固定余量 {SECOND_HOP_FIXED_ALLOWANCE}",
            frame_limit=frame_limit,
            conservative=conservative,
            budget_note=budget_note,
        )

    if plan.kind != PLAN_OK:
        return _terminal_result(plan, frame_limit=frame_limit, conservative=conservative, budget_note=budget_note)

    images_planned = plan.image_count()
    details: Dict[str, Any] = {
        "plan_notes": list(plan.notes),
        "plan_stats": dict(plan.stats),
        "intent_mode": plan.intent_mode,
        "shape": plan.shape,
    }

    if plan._claimed:
        log.warning("同一 plan 重复交付请求已拒绝（防重打）: groups=%s", len(plan.groups))
        return DeliveryResult(
            status=STATUS_UNKNOWN,
            success=False,
            stop_after_execution=False,
            error="同一 plan 已交付过，未重复发送（迟到重打已拒绝）",
            packs_planned=len(plan.groups),
            images_planned=images_planned,
            frame_limit=frame_limit,
            conservative=conservative,
            budget_note=budget_note,
            details=details,
        )
    plan._claimed = True

    builder = _PacketBuilder(stream_id=stream_id, frame_limit=frame_limit, log=log)
    skipped: List[str] = []
    frames: List[int] = []
    hops: List[List[int]] = []
    packs_sent = 0
    images_sent = 0
    images_failed = 0

    async def flush() -> None:
        nonlocal packs_sent
        if not builder.has_content():
            return
        nodes = builder.seal(SOURCE_LINE)
        hop_first = frame_bytes(nodes, stream_id)
        hop_second = gateway_frame_bytes(nodes, stream_id)
        size = max(hop_first, hop_second)
        if size > frame_limit:
            # 逐项计量后理论上不可达；宁可明确不发，也不发不合规包
            raise DeliveryError(
                f"包内容第一跳 {hop_first} / 第二跳 {hop_second} 字节超过单帧上限 {frame_limit}，未发送"
            )
        if not await _send_packet(send_forward, nodes, stream_id, log):
            raise _SendAborted("合并转发未确认送达", groups_left=0)
        packs_sent += 1
        frames.append(size)
        hops.append([hop_first, hop_second])

    aborted: Optional[_SendAborted] = None
    groups_left = len(plan.groups)
    try:
        for index, group in enumerate(plan.groups):
            groups_left = len(plan.groups) - index
            builder.begin_group()
            header_segment = _text_segment(group.label + (f"：{group.note}" if group.note else ""))
            lead_placed = False

            async def place(segments: List[Dict[str, Any]]) -> bool:
                """放入一段内容；分组标题只随首个真实内容一起落位（不单发空标题）。"""

                nonlocal lead_placed
                if builder.add_all(segments if lead_placed else [header_segment, *segments]):
                    lead_placed = True
                    return True
                await flush()
                if builder.add_all(segments if lead_placed else [header_segment, *segments]):
                    lead_placed = True
                    return True
                return False

            for item in group.items:
                if isinstance(item, TextItem):
                    for piece in _text_pieces(item.text):
                        if await place([_text_segment(piece)]):
                            continue
                        skipped.append(
                            f"「{group.label}」有一段 {len(piece)} 字符文字超单包预算，未发送"
                        )
                        break
                elif isinstance(item, ImageItem):
                    try:
                        raw = await _load_image_bytes(load_image, item.url)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        images_failed += 1
                        skipped.append(
                            f"「{group.label}」{item.filename} 取图失败（{type(exc).__name__}: {exc}）未发送"
                        )
                        continue
                    encoded = await asyncio.to_thread(base64.b64encode, raw)
                    segment = _image_segment(encoded.decode("ascii"), item.filename)
                    if await place([segment]):
                        images_sent += 1
                        continue
                    images_failed += 1
                    skipped.append(
                        f"「{group.label}」{item.filename} 单图编码后 {len(encoded)} 字节，"
                        "单个合并转发放不下，明确未发送"
                    )
                else:
                    skipped.append(f"「{group.label}」出现未知条目类型 {type(item).__name__}，未发送")
            if not lead_placed:
                # 这一组没有任何真实内容落下（如媒体题全部取图失败）：整组不发，
                # 连标题都不发——只发标题和落款不能算把资料送达。
                skipped.append(f"「{group.label}」整组没有任何真实内容可发，标题与落款未发送")
        if builder.has_content():
            await flush()
    except _SendAborted as exc:
        aborted = exc
        aborted.groups_left = groups_left
    except DeliveryError as exc:
        return _failed_result(
            str(exc),
            packs_planned=len(plan.groups),
            images_planned=images_planned,
            images_sent=images_sent,
            images_failed=images_failed,
            skipped=skipped,
            frame_limit=frame_limit,
            conservative=conservative,
            budget_note=budget_note,
            details=details,
        )
    except asyncio.CancelledError:
        raise

    details.update(
        {
            "frame_limit": frame_limit,
            "frame_hops": hops,
            "frames": frames,
            "skipped": skipped,
        }
    )

    if aborted is not None:
        return DeliveryResult(
            status=STATUS_UNKNOWN,
            success=False,
            stop_after_execution=False,
            user_text="送达不确定",
            error=(
                f"{aborted.error}；已发出 {packs_sent} 包（未确认/已确认混合），后续 "
                f"{aborted.groups_left} 个分组未再尝试；按纪律不重发"
            ),
            packs_planned=len(plan.groups),
            packs_sent=packs_sent,
            images_planned=images_planned,
            images_sent=images_sent,
            images_failed=images_failed,
            skipped_reasons=skipped,
            unsent_groups=aborted.groups_left,
            partial_delivery=packs_sent > 0,
            frame_limit=frame_limit,
            conservative=conservative,
            budget_note=budget_note,
            frames=frames,
            details=details,
        )

    if packs_sent == 0:
        # 有分组但一个包都没发出（典型：媒体题的图全挂/全超预算）：不能算 partial 成功，
        # 也没发出任何东西，按「明确未发」出面：一行落款都不发。
        return DeliveryResult(
            status=STATUS_NOTHING,
            success=False,
            stop_after_execution=False,
            user_text="没有资料发出（计划内的内容全部明确未发）",
            error="计划内没有任何一组真实内容成功发出"
            + (f"（{len(skipped)} 项明确未发）" if skipped else ""),
            packs_planned=len(plan.groups),
            packs_sent=0,
            images_planned=images_planned,
            images_sent=images_sent,
            images_failed=images_failed,
            skipped_reasons=skipped,
            unsent_groups=len(plan.groups),
            frame_limit=frame_limit,
            conservative=conservative,
            budget_note=budget_note,
            frames=frames,
            details=details,
        )

    if skipped or images_failed:
        return DeliveryResult(
            status=STATUS_PARTIAL,
            success=True,
            stop_after_execution=False,
            user_text="部分发出",
            error="",
            packs_planned=len(plan.groups),
            packs_sent=packs_sent,
            images_planned=images_planned,
            images_sent=images_sent,
            images_failed=images_failed,
            skipped_reasons=skipped,
            frame_limit=frame_limit,
            conservative=conservative,
            budget_note=budget_note,
            frames=frames,
            details=details,
        )

    return DeliveryResult(
        status=STATUS_SENT,
        success=True,
        stop_after_execution=True,
        packs_planned=len(plan.groups),
        packs_sent=packs_sent,
        images_planned=images_planned,
        images_sent=images_sent,
        images_failed=0,
        frame_limit=frame_limit,
        conservative=conservative,
        budget_note=budget_note,
        frames=frames,
        details=details,
    )


async def deliver_query(
    conn: sqlite3.Connection,
    result: Any,
    intent: Any,
    *,
    stream_id: str,
    send_forward: Callable[..., Any],
    load_image: Optional[Callable[[str], Any]] = None,
    budget_bytes: Optional[int] = None,
    host_version: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> DeliveryResult:
    """一步到位：组装计划并发送（组装在线程里跑，不阻塞事件循环）。"""

    plan = await asyncio.to_thread(prepare_delivery, conn, result, intent)
    return await deliver_what(
        stream_id=stream_id,
        send_forward=send_forward,
        plan=plan,
        load_image=load_image,
        budget_bytes=budget_bytes,
        host_version=host_version,
        logger=logger,
    )


def _default_logger() -> logging.Logger:
    return logger


def _terminal_result(
    plan: DeliveryPlan, *, frame_limit: int, conservative: bool, budget_note: str
) -> DeliveryResult:
    """非 ok 计划：不发任何包，按「合法结论 / 明确失败」分流。"""

    details = {"plan_notes": list(plan.notes), "intent_mode": plan.intent_mode, "shape": plan.shape}
    if plan.kind in ("not_found", "clarify", "no_source", "nothing"):
        message = plan.message or ("没查到" if plan.kind == "not_found" else "")
        return DeliveryResult(
            status=STATUS_NOTHING,
            success=True,
            stop_after_execution=True,
            user_text=message,
            error="",
            frame_limit=frame_limit,
            conservative=conservative,
            budget_note=budget_note,
            details=details,
        )
    return DeliveryResult(
        status=STATUS_FAILED,
        success=False,
        stop_after_execution=False,
        user_text="读取失败",
        error=f"计划无效（{plan.kind}）：" + "；".join(plan.notes or []),
        frame_limit=frame_limit,
        conservative=conservative,
        budget_note=budget_note,
        details=details,
    )


def _failed_result(
    error: str,
    *,
    packs_planned: int = 0,
    images_planned: int = 0,
    images_sent: int = 0,
    images_failed: int = 0,
    skipped: Optional[List[str]] = None,
    frame_limit: int = FRAME_LIMIT_DEFAULT,
    conservative: bool = True,
    budget_note: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> DeliveryResult:
    return DeliveryResult(
        status=STATUS_FAILED,
        success=False,
        stop_after_execution=False,
        user_text="读取失败",
        error=error,
        packs_planned=packs_planned,
        images_planned=images_planned,
        images_sent=images_sent,
        images_failed=images_failed,
        skipped_reasons=list(skipped or []),
        frame_limit=frame_limit,
        conservative=conservative,
        budget_note=budget_note,
        details=dict(details or {}),
    )
