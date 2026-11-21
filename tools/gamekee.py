# -*- coding: utf-8 -*-
"""GameKee 星塔旅人 wiki 数据源：六属性一图流索引、更新探查与图片下载。

数据源（实机验证，2026-11-22）：
- API：GET https://www.gamekee.com/v1/content/detail/{文章id}，请求头必须带
  `game-alias: stellasora`（另加 Lang: zh-cn、X-Requested-With: XMLHttpRequest 更稳），
  匿名可用，返回 {"code":0, "data":{..., "content_json": "..."}}。
- content_json 为 JSON 字符串，反序列化后是 ProseMirror 风格节点数组：
  * header2 节点 = 角色小节标题（如"艾蕾一图流攻略"）；
  * 其后第一个含图节点 = 该角色攻略图：单图为 paragraph.children[] 里
    type=="image"（src 字段），多图为 type=="image-album"（data 数组），
    **末张 = 完整版长图**；
  * 页尾 header1 文本含"队伍码"后进入队伍码区，不再视为角色小节。
- 六属性页固定 id：水 714580 / 火 715868 / 光 715966 / 暗 715957 / 风 715960 / 地 715965。
- 图片直链：src 为协议相对 "//cdnimg-v2.gamekee.com/..."；裸 GET 返回 567，
  必须带 `Referer: https://www.gamekee.com/`；URL 追加
  `?eo-img.format=webp&x-image-process=image/format,webp/ignore-error,1`
  可得同尺寸 webp（原图 PNG 约 2.1MB → webp 约 230KB，不缩分辨率）。

更新探查纪律（计划 P2）：三个时间戳分离（last_probe_attempt / last_verified /
index_built_at，由 plugin.py 持久化到 state.json）；六页整批校验成功才推进
last_verified 并发布索引；部分失败保留旧快照不发布。本模块只提供无状态函数，
时间戳与调度由 plugin.py 管理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import base64
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from net_common import atomic_write as _atomic_write, build_opener as _build_opener, resolve_proxy as _resolve_proxy

# 供 plugin.py 复用的原子写别名（单一事实源在 net_common.atomic_write）
atomic_write = _atomic_write

logger = logging.getLogger("stellasora.gamekee")

_API_BASE = "https://www.gamekee.com"
_CDN_HOST = "cdnimg-v2.gamekee.com"
_IMAGE_REFERER = "https://www.gamekee.com/"

# webp 转换参数（CDN 图片处理服务能力；不缩分辨率，仅转格式）
_WEBP_QUERY = "?eo-img.format=webp&x-image-process=image/format,webp/ignore-error,1"

# 六属性页文章 id（顺序即展示顺序；与攻略组页面一致）
ELEMENT_PAGES: Dict[str, int] = {
    "water": 714580,
    "fire": 715868,
    "light": 715966,
    "dark": 715957,
    "wind": 715960,
    "earth": 715965,
}

# 属性键 → 中文名（与旧服务层 _ELEMENT_CN 的六属性一致）
ELEMENT_CN = {
    "water": "水",
    "fire": "火",
    "light": "光",
    "dark": "暗",
    "wind": "风",
    "earth": "地",
}

# 单图下载上限（webp 后单张约 230-400KB；PNG 原图约 2.1MB；取 PNG 上限的 2 倍余量）
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 图片磁盘缓存上限（字节数）与条目过期（秒）
IMAGE_CACHE_MAX_BYTES = 128 * 1024 * 1024
IMAGE_CACHE_TTL_SECONDS = 7 * 24 * 3600

# 请求头（实机验证必须项）
_API_HEADERS = {
    "game-alias": "stellasora",
    "Lang": "zh-cn",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0",
}

# 角色小节标题的后缀噪声（规范化角色名用）
_TITLE_SUFFIX_RE = re.compile(r"(一图流|攻略|\s|：.*$|:.*$)+")

# 队伍码编成码格式（36 位 ASCII base64 风格；AAAA 只是已见前缀不是格式约束）
_TEAM_CODE_RE = re.compile(r"[A-Za-z0-9+/]{36}")

# 索引 schema 版本：2 = 新增 pages[element].team_codes（v1 快照缺码，加载时触发重建）
INDEX_SCHEMA_VERSION = 2


def index_schema_version() -> int:
    """当前索引 schema 版本（plugin.py 迁移判定用；函数化避免跨模块复制常量）。"""
    return INDEX_SCHEMA_VERSION


class GameKeeError(RuntimeError):
    """GameKee 数据源错误（结构漂移/鉴权拒绝/网络失败统一类型，含文章 id 与状态）。"""


def _api_opener(proxy_url: Optional[str] = None):
    return _build_opener(_resolve_proxy(proxy_url))


def _request_article(
    page_id: int,
    proxy_url: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """请求单篇文章 API 并校验外层结构（六页路径共用）。

    返回 data 字段 dict；HTTP/网络/结构异常统一抛 GameKeeError（含文章 id）。
    """
    url = f"{_API_BASE}/v1/content/detail/{page_id}"
    opener = _api_opener(proxy_url)
    try:
        req = urllib.request.Request(url, headers=_API_HEADERS)
        with opener.open(req, timeout=timeout) as response:
            if response.getcode() != 200:
                raise GameKeeError(f"文章 {page_id} HTTP {response.getcode()}")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GameKeeError(f"文章 {page_id} HTTP {exc.code}（可能加鉴权）") from exc
    except GameKeeError:
        raise
    except Exception as exc:
        raise GameKeeError(f"文章 {page_id} 网络失败: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise GameKeeError(f"文章 {page_id} API 返回异常: code={payload.get('code') if isinstance(payload, dict) else '?'}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GameKeeError(f"文章 {page_id} 缺少 data 字段")
    return data


def fetch_element_page(
    element: str,
    proxy_url: Optional[str] = None,
    timeout: int = 30,
) -> Tuple[int, str, list]:
    """拉取单属性页并解析 content_json，返回 (updated_at, 标题, 节点数组)。

    结构/鉴权异常抛 GameKeeError（含文章 id 与错误类型），不做静默降级——
    上游漂移必须完整暴露（AGENTS.md 禁兜底）。
    """
    page_id = ELEMENT_PAGES.get(element)
    if page_id is None:
        raise GameKeeError(f"未知属性页: {element}")
    data = _request_article(page_id, proxy_url, timeout)
    updated_at = int(data.get("updated_at") or 0)
    title = str(data.get("title") or "")
    raw_json = data.get("content_json")
    if not isinstance(raw_json, str) or not raw_json.strip():
        raise GameKeeError(f"文章 {page_id} content_json 缺失或为空（结构漂移）")
    try:
        nodes = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise GameKeeError(f"文章 {page_id} content_json 解析失败: {exc}") from exc
    if not isinstance(nodes, list) or not nodes:
        raise GameKeeError(f"文章 {page_id} content_json 非节点数组")
    return updated_at, title, nodes


def _node_text(node: dict) -> str:
    """提取节点全部文本（children[].text 拼接）。"""
    if not isinstance(node, dict):
        return ""
    parts = []
    for child in node.get("children") or []:
        if isinstance(child, dict):
            t = child.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)


def normalize_character_name(raw_title: str) -> str:
    """角色小节标题 → 规范角色名。

    "艾蕾一图流攻略" → "艾蕾"；"尘沙（源初）一图流攻略" → "尘沙（源初）"；
    "科洛妮丝新春攻略一图流：印记篇" → "科洛妮丝新春·印记篇"（分篇信息保留）。
    """
    text = (raw_title or "").strip()
    if not text:
        return ""
    # 先截掉"："之后的分篇后缀，再剥离"一图流/攻略"噪声
    main, sep, part = text.partition("：")
    if not sep:
        main, sep, part = text.partition(":")
    core = _TITLE_SUFFIX_RE.sub("", main).strip("　 ")
    if part.strip():
        core = f"{core}·{part.strip()}"
    return core


def _image_srcs_from_node(node: dict) -> List[str]:
    """从单个节点提取图片 src 列表（paragraph 内嵌 image 或 image-album.data）。"""
    srcs: List[str] = []
    ntype = node.get("type")
    if ntype == "image-album":
        data = node.get("data")
        if isinstance(data, list):
            srcs = [str(s) for s in data if isinstance(s, str) and s.strip()]
    else:
        for child in node.get("children") or []:
            if isinstance(child, dict) and child.get("type") == "image":
                src = child.get("src")
                if isinstance(src, str) and src.strip():
                    srcs.append(src)
    return srcs


def parse_content_json(
    nodes: list,
    element: str,
) -> Tuple[List[dict], List[dict]]:
    """解析 content_json 节点数组 → (角色小节列表, 队伍码列表)。

    角色小节每项 {"name", "note", "image_urls"}：
    - name：规范化角色名（含分篇后缀）；
    - note：小节标题后、图片前的段落文本（一句话备注）；
    - image_urls：该小节全部图片（相册保序；旧"末张=完整版长图"假设实测
      只对部分角色成立，发送侧已改为按序全发）。

    备注只取图片之前的首个非空段落：图片之后的段落是网页相册轮播提示类噪声
    （见下方位置闸注释）。

    队伍码区（header1 含"队伍码"之后的页级公共段）不再整段丢弃：
    header2=编队名（仅清空白与末尾冒号，不进角色索引、不参与角色匹配），
    其后第一个非空 paragraph 的文本必须完整匹配 36 位 ASCII 编成码
    （A-Za-z0-9+/；AAAA 只是已见前缀不是格式约束），否则抛 GameKeeError
    （结构漂移即整批失败，保留旧快照——与 build_index 同纪律）。
    """
    entries: List[dict] = []
    team_codes: List[dict] = []
    current: Optional[dict] = None
    in_team_code_zone = False
    pending_code_title: Optional[str] = None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if ntype in ("header1", "header2"):
            text = _node_text(node).strip()
            if ntype == "header1" and "队伍码" in text:
                in_team_code_zone = True
                current = None
                continue
            if in_team_code_zone:
                if ntype == "header2" and text:
                    pending_code_title = text.rstrip("：: ")
                continue
            if ntype == "header2" and text:
                current = {
                    "name": normalize_character_name(text),
                    "raw_title": text,
                    "note": "",
                    "image_urls": [],
                }
                entries.append(current)
                continue
            # header1 非队伍码（如"水系角色一图流攻略"总标题）：不中断当前小节
            continue
        if current is None and not (in_team_code_zone and pending_code_title):
            continue
        if in_team_code_zone:
            if pending_code_title and ntype == "paragraph":
                text = _node_text(node).strip()
                if not text:
                    continue
                if not _TEAM_CODE_RE.fullmatch(text):
                    raise GameKeeError(
                        f"队伍码格式异常（element={element} 编队={pending_code_title!r}）: {text[:60]}"
                    )
                team_codes.append({"title": pending_code_title, "code": text})
                pending_code_title = None
            continue
        srcs = _image_srcs_from_node(node)
        if srcs:
            current["image_urls"].extend(srcs)
            continue
        # note 只取**图前**首段：图片之后的段落是网页相册的轮播提示类噪声
        #（实机「左右滑动可以查看下一张图！！」，图后作首备注的 5 处全是这一句），
        # 而 QQ 转发里相册图已逐段展示、无需滑动提示。位置闸优于文案黑名单——
        # 不猜文案，也不会误删图前真正的备注（如地页优叶原文含连续码的段落）。
        if ntype == "paragraph" and not current["note"] and not current["image_urls"]:
            text = _node_text(node).strip()
            if text:
                current["note"] = text
    if pending_code_title is not None:
        raise GameKeeError(
            f"队伍码区编队标题后缺码（element={element} 编队={pending_code_title!r}）"
        )
    result = [e for e in entries if e["name"] and e["image_urls"]]
    for e in result:
        e.pop("raw_title", None)
        e["element"] = element
    return result, team_codes


def build_index(
    proxy_url: Optional[str] = None,
    pages: Optional[Dict[str, Tuple[int, str, list]]] = None,
) -> Dict[str, Any]:
    """拉取/解析六属性页 → 索引结构（一次构建整批发布，六页全部成功才返回）。

    pages 供测试注入预解析数据（键=element，值=(updated_at,title,nodes)；提供
    单页时其余页以空节点数组占位，便于最小化测试 fixture）。生产路径自行
    fetch_element_page。任一页失败抛 GameKeeError（整批失败，
    由调用方保留旧快照——计划 P2 整批校验纪律）。
    """
    index_characters: List[dict] = []
    page_meta: Dict[str, Any] = {}
    for element, page_id in ELEMENT_PAGES.items():
        if pages is not None:
            if element in pages:
                updated_at, title, nodes = pages[element]
            else:
                updated_at, title, nodes = 0, "", []
        else:
            updated_at, title, nodes = fetch_element_page(element, proxy_url=proxy_url)
        entries, team_codes = parse_content_json(nodes, element)
        page_meta[element] = {
            "id": page_id,
            "title": title,
            "updated_at": updated_at,
            "characters": len(entries),
            "team_codes": team_codes,
        }
        index_characters.extend(entries)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "built_at": int(time.time()),
        "pages": page_meta,
        "characters": index_characters,
    }


def probe_updates(
    proxy_url: Optional[str] = None,
    timeout: int = 20,
    known_updates: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """轻量更新探查：拉六页仅取 updated_at（解析 content_json 前即返回）。

    返回 {"pages": {element: {"updated_at", "published_at"}},
    "changed": bool}——与调用方传入的 known 快照比对，全部一致
    即 changed=False（六页无变化，仅推进时间戳）；任一页失败抛 GameKeeError
    （整批失败语义，与 build_index 一致）。
    """
    known = known_updates or {}
    result: Dict[str, Dict[str, int]] = {}
    changed = False
    for element, page_id in ELEMENT_PAGES.items():
        data = _request_article(page_id, proxy_url, timeout)
        updated_at = int(data.get("updated_at") or 0)
        result[element] = {
            "updated_at": updated_at,
            "published_at": int(data.get("published_at") or 0),
        }
        if known.get(element) != updated_at:
            changed = True
    return {"pages": result, "changed": changed}

def match_guides(
    index: Dict[str, Any],
    query: str,
    aliases: Optional[Dict[str, str]] = None,
) -> Tuple[List[dict], str]:
    """按用户问题匹配攻略小节。

    返回 (命中小节列表, miss_reason)。匹配优先级：
    1. 别名/角色名整词命中（规范化括号统一全角）——角色优先于属性；
    2. 分篇后缀命中（"直伤篇"等，附属于角色名一起匹配）；
    3. 属性泛查（"水系攻略/风队"）→ 该属性全部小节；
    未命中返回 ([], reason)。
    """
    q = (query or "").strip()
    if not q:
        return [], "empty_query"
    q_norm = q.replace("(", "（").replace(")", "）")

    characters: List[dict] = index.get("characters") or []
    alias_map: Dict[str, str] = {k: v for k, v in (aliases or {}).items() if k and v}

    # 问句侧别名归一：问句中出现的别名替换为官方名（config 语义 alias→official），
    # 替换后与索引规范角色名做包含匹配（"春科攻略"→"科洛妮丝新春攻略"）。
    for alias, official in alias_map.items():
        if alias in q_norm:
            q_norm = q_norm.replace(alias, official)

    # 1. 角色命中：官方名（含分篇 base）出现在归一化问句中；角色优先于属性。
    # 分篇规则：问句点名某分篇（如"直伤篇"）→ 只发该篇；未点名 → 该角色全部分篇。
    # base 匹配取互为前缀口径："科洛妮丝攻略"（短）也能命中 base="科洛妮丝新春"（长）——
    # 问句角色词与索引名互为前缀即同一角色的不同叫法跨度（新春前后缀、版本括号）。
    hits: List[dict] = []
    named_parts = {entry["name"].split("·", 1)[1] for entry in characters if "·" in entry["name"]}
    named_in_query = {p for p in named_parts if p and p in q_norm}
    # 问句角色词 = 去掉泛查后缀词的剩余主干（"科洛妮丝攻略"→"科洛妮丝"）
    core = re.sub(r"(攻略|怎么玩|玩法|一图流|推荐|配队|指南)", "", q_norm).strip("　 ")
    for entry in characters:
        base_name = entry["name"].split("·")[0]
        matched = base_name in q_norm or (
            core and (base_name.startswith(core) or core.startswith(base_name))
        )
        if not matched:
            continue
        part = entry["name"].split("·", 1)
        if len(part) == 1 or not named_in_query or part[1] in named_in_query:
            hits.append(entry)
    if hits:
        # 去重保序（同名多小节各自保留）
        seen = set()
        uniq = []
        for e in hits:
            key = (e["element"], e["name"])
            if key not in seen:
                seen.add(key)
                uniq.append(e)
        return uniq, ""

    # 2. 属性泛查：问句含「属性词 + 队/系/属性」或属性名+攻略
    element_hits = _match_element(q_norm)
    if element_hits:
        elems = element_hits
    else:
        # 别名指向属性名（如 土→地）
        for alias, official in alias_map.items():
            if alias and alias in q_norm and official in ELEMENT_CN.values():
                elems = [k for k, v in ELEMENT_CN.items() if v == official]
                break
        else:
            return [], "no_match"
    grouped = [e for e in characters if e.get("element") in elems]
    if grouped:
        return grouped, ""
    return [], "no_match"


def _match_element(q: str) -> List[str]:
    """问句 → 命中的属性键列表。

    「属性词 + 队/系/属性」泛查（水队/火系/光属性）；问句恰为单属性词（经
    别名归一后，如"土印记队"→"地印记队"）时按"地印记队含属性词"的包含匹配——
    单字属性名无歧义，出现在玩法词组中仍指向该属性。
    """
    hits = []
    for key, cn in ELEMENT_CN.items():
        if f"{cn}队" in q or f"{cn}系" in q or f"{cn}属性" in q or q == cn or cn in q:
            hits.append(key)
    return hits


def image_url(src: str, prefer_webp: bool = True) -> str:
    """协议相对 src → 完整 https URL（可选 webp 转换参数）。"""
    url = src if src.startswith("http") else f"https:{src}"
    if prefer_webp and _WEBP_QUERY not in url:
        url = url + _WEBP_QUERY
    return url


def _cache_path(cache_dir: Path, url: str, mime_ext: str) -> Path:
    """图片缓存文件名：规范化 URL 的 SHA-256 + 实际格式后缀（不用用户输入作路径）。"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}{mime_ext}"


def download_image(
    src: str,
    cache_dir: Path,
    prefer_webp: bool = True,
    proxy_url: Optional[str] = None,
    timeout: int = 60,
) -> bytes:
    """下载攻略图（带 Referer/校验/磁盘缓存），返回图片字节。

    缓存命中（TTL 内）零网络；下载校验成功后原子写入。CDN 域白名单之外
    的 src 拒绝下载；响应非图片（HTML 拦截页/超限）抛 GameKeeError 完整暴露。
    """
    url = image_url(src, prefer_webp=prefer_webp)
    host = urllib.parse.urlparse(url).hostname or ""
    if host != _CDN_HOST:
        raise GameKeeError(f"图片域名不在白名单: {host}")

    # 探测格式后缀：webp 参数下为 .webp，否则按 src 后缀
    ext = ".webp" if prefer_webp else ("." + (src.rsplit(".", 1)[-1].lower() or "png"))
    cache_file = _cache_path(cache_dir, url, ext)
    if cache_file.is_file():
        try:
            age = time.time() - cache_file.stat().st_mtime
            if age < IMAGE_CACHE_TTL_SECONDS:
                data = cache_file.read_bytes()
                if data:
                    return data
        except OSError:
            pass

    opener = _build_opener(_resolve_proxy(proxy_url))
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": _IMAGE_REFERER,
                "Origin": _IMAGE_REFERER.rstrip("/"),
            },
        )
        with opener.open(req, timeout=timeout) as response:
            code = response.getcode()
            if code != 200:
                raise GameKeeError(f"图片 HTTP {code}: {url[:120]}")
            data = response.read(_MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise GameKeeError(f"图片 HTTP {exc.code}: {url[:120]}") from exc
    except GameKeeError:
        raise
    except Exception as exc:
        raise GameKeeError(f"图片下载失败: {exc}: {url[:120]}") from exc

    if len(data) > _MAX_IMAGE_BYTES:
        raise GameKeeError(f"图片超过大小上限（{_MAX_IMAGE_BYTES}B）: {url[:120]}")
    _validate_image(data, prefer_webp)

    # 原子写入缓存（tmp+replace），失败不阻断返回（缓存是优化不是正确性依赖）
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_name(f".{cache_file.name}.{time.time_ns()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(cache_file)
        _evict_image_cache(cache_dir)
    except OSError:
        logger.warning("图片缓存写入失败（不影响本次发送）: %s", cache_file.name)

    return data


def _validate_image(data: bytes, expect_webp: bool) -> None:
    """魔数校验：只接受 PNG/JPEG/WEBP，HTML 拦截页（裸 GET 567 的产物）报错暴露。"""
    if len(data) < 12:
        raise GameKeeError("图片响应过短（可能为拦截页）")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return
    if data[:3] == b"\xff\xd8\xff":
        return
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if expect_webp:
            return
        # 关 webp 却拿到 webp：CDN 参数未生效，仍接受（视觉一致）但记录
        logger.info("请求原 PNG 但 CDN 返回 webp（参数可能被忽略）")
        return
    head = data[:64].decode("ascii", errors="replace")
    raise GameKeeError(f"响应非图片（疑似拦截页）: head={head!r}")


def _evict_image_cache(cache_dir: Path) -> None:
    """图片缓存超限回收：TTL 过期先删；总量超 IMAGE_CACHE_MAX_BYTES 按最旧删除。"""
    try:
        files = [p for p in cache_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
        now = time.time()
        total = 0
        kept: List[Tuple[float, Path, int]] = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            if now - st.st_mtime > IMAGE_CACHE_TTL_SECONDS:
                p.unlink(missing_ok=True)
                continue
            total += st.st_size
            kept.append((st.st_mtime, p, st.st_size))
        if total <= IMAGE_CACHE_MAX_BYTES:
            return
        kept.sort(key=lambda x: x[0])
        for _, p, size in kept:
            if total <= IMAGE_CACHE_MAX_BYTES:
                break
            p.unlink(missing_ok=True)
            total -= size
    except OSError:
        pass
