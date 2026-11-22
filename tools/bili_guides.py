# -*- coding: utf-8 -*-
"""B 站「铃儿的猫猫球」一图流攻略源：清单匹配、opus 详情解析、webp 压缩与磁盘缓存。

数据链路（实机验证，2026-11-22）：
- 详情：`GET https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail?id=<opus_id>`，
  必须带浏览器 UA + `Referer: https://www.bilibili.com/opus/`；匿名 200。
  参数名是 `id`（`oid` 返回 -400）。图片在 `data.item.modules[]` 中
  MODULE_TYPE_CONTENT 的 `module_content.paragraphs[]` 里 `para_type==2` 的
  `pic.pics[].url`（即原图直链，无 @ 后缀）；标题在 MODULE_TYPE_TITLE 的
  `module_title.text`。
- 图片：直链 GET 无防盗链，主机必须在 `_IMAGE_HOSTS` 白名单内（跳转目标同样受限）。
- 不扫列表接口：运行时只按内置清单的 opus_id 精确取详情；扫列表只在
  tools/bili_update.py 维护脚本里做。

格式与缓存：
- webp 必需，无原图兜底：下载后本地 Pillow 压缩（原分辨率、quality=88、
  method=6），原图处理完即丢，磁盘只留 webp。Pillow 缺失或编码失败一律抛
  BiliGuideError（含 opus_id/阶段/异常），绝不静默发原图。
- 缓存永久：`<cache_root>/<opus_id>/`，无 TTL/无容量淘汰/卸载不清理。宿主
  runtime_dir 若在临时目录，清临时或重装后自然重抓（README 已注明）。
- 重建走 staging 目录 + 目录级原子切换：全新写完才换上去，中途失败不会留下
  "新图 + 旧 meta" 的假完整缓存。

并发纪律：同 opus_id 的抓取用模块级 per-opus 锁合并（single-flight），不同
opus_id 可并行；单个 opus 内的多张图按 `_IMAGE_WORKERS` 并发下载。
本模块全部为同步函数，调用方负责 asyncio.to_thread。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import io
import json
import logging
import re
import threading
import time

from net_common import (
    HttpBytesError as _HttpBytesError,
    atomic_write as _atomic_write,
    read_http_bytes as _read_http_bytes,
)

logger = logging.getLogger("stellasora.bili_guides")

MANIFEST_SCHEMA_VERSION = 1

_OPUS_DETAIL_HOST = "api.bilibili.com"
_OPUS_DETAIL_API = f"https://{_OPUS_DETAIL_HOST}/x/polymer/web-dynamic/v1/opus/detail"
_OPUS_REFERER = "https://www.bilibili.com/opus/"
# 浏览器 UA：匿名接口对默认 Python-urllib UA 不稳定，固定 Chrome 级 UA
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 图片 CDN 主机白名单：B 站图片域为 i0/i1/i2.hdslb.com（实机返回 http 直链，
# 统一升 https 后请求）。上游响应里的 URL 不可信，不在此表内一律拒绝。
_IMAGE_HOSTS = ("i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com")

# 单张原图下载上限：实机最大约 6.2MB（1080x 级 PNG），留 2 倍余量
_MAX_IMAGE_BYTES = 16 * 1024 * 1024
# opus 详情响应上限（含正文文本，实机约 20KB）
_MAX_DETAIL_BYTES = 4 * 1024 * 1024

# webp 编码参数：原分辨率、有损 88、method=6
_WEBP_QUALITY = 88
_WEBP_METHOD = 6

# 缓存内的清单文件名（提交标记：写完它才算缓存有效）
_META_NAME = "meta.json"
# 重建用的暂存目录后缀（与正式目录同父级，rename 才是原子的）
_STAGING_SUFFIX = ".staging"

# 单 opus 内并发下载/编码的线程数（攻略图一张 3-6MB，4 路足够且不压垮上游）
_IMAGE_WORKERS = 4

# 单图解码像素上限（1080x 级攻略图远小于此；防解压炸弹，不用全局 MAX_IMAGE_PIXELS）
_MAX_PIXELS = 12000 * 12000

# 模块级 per-opus 锁：同 opus_id 并发抓取合并为一次（single-flight）
_OPUS_LOCKS: Dict[str, threading.Lock] = {}
_OPUS_LOCKS_GUARD = threading.Lock()


class BiliGuideError(Exception):
    """猫猫球攻略源失败（清单损坏 / 详情请求失败 / 结构漂移 / 压缩失败 / 缓存重建失败）。"""


def _opus_lock(opus_id: str) -> threading.Lock:
    with _OPUS_LOCKS_GUARD:
        lock = _OPUS_LOCKS.get(opus_id)
        if lock is None:
            lock = threading.Lock()
            _OPUS_LOCKS[opus_id] = lock
        return lock


# ===== 清单加载与校验 =====


def load_manifest(assets_dir: Path) -> Dict[str, Any]:
    """读取并校验内置清单（assets/bili_guides/manifest.json）。

    assets_dir 为**插件根目录**（清单随包发放于 <root>/assets/bili_guides/）。
    结构漂移即抛 BiliGuideError（不猜字段、不静默降级成空清单——空清单会让
    所有查询都变成"未收录"，掩盖分发错误）。
    """
    path = Path(assets_dir) / "assets" / "bili_guides" / "manifest.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BiliGuideError(f"猫猫球清单读取失败: {path}: {exc}") from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BiliGuideError(f"猫猫球清单不是合法 JSON: {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BiliGuideError(f"猫猫球清单结构不符（顶层应为对象）: {path}")
    if int(manifest.get("schema_version") or 0) != MANIFEST_SCHEMA_VERSION:
        raise BiliGuideError(
            f"猫猫球清单 schema_version 不符: {manifest.get('schema_version')!r}"
            f"（期望 {MANIFEST_SCHEMA_VERSION}）"
        )
    roles = manifest.get("roles")
    guides = manifest.get("guides")
    if not isinstance(roles, list) or not isinstance(guides, list):
        raise BiliGuideError("猫猫球清单缺少 roles/guides 数组")
    role_names = set()
    for entry in roles:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            raise BiliGuideError(f"猫猫球清单 roles 条目非法: {entry!r}")
        role_names.add(_normalize_role(str(entry["name"])))
    for entry in guides:
        if not isinstance(entry, dict):
            raise BiliGuideError(f"猫猫球清单 guides 条目非法: {entry!r}")
        opus_id = str(entry.get("opus_id") or "").strip()
        # opus_id 必须是纯数字串：B 站 id 有 19 位，走 int 会丢精度，清单里
        # 一律存字符串，此处校验格式防止维护脚本写坏
        if not opus_id.isdigit():
            raise BiliGuideError(f"猫猫球清单 opus_id 非法（应为数字串）: {entry.get('opus_id')!r}")
        role = entry.get("role")
        if role is not None and _normalize_role(str(role)) not in role_names:
            raise BiliGuideError(
                f"猫猫球清单角色不在 roles 快照中: opus={opus_id} role={role!r}"
            )
    return manifest


def _normalize_role(name: str) -> str:
    """角色名归一：全角括号 → 半角括号（与 tools/gamekee 的括号归一同一口径）。"""
    return (name or "").strip().replace("（", "(").replace("）", ")")


def _apply_role_aliases(manifest: Dict[str, Any]) -> Dict[str, str]:
    """清单 role_aliases → {B站叫法: 我方规范名}（归一后）。

    B 站与 GameKee 的官方译名有分歧（实机核对：B 站「埃莉诺」= GameKee
    「埃利诺」；B 站「圣夜菈露」= GameKee「菈露（圣夜）」）。清单显式记录映射，
    不在代码里硬编码译名差异。
    """
    return {
        _normalize_role(str(k)): _normalize_role(str(v))
        for k, v in (manifest.get("role_aliases") or {}).items()
        if k and v
    }


# ===== 角色匹配 =====


def match_guides(
    manifest: Dict[str, Any],
    query: str,
    aliases: Optional[Dict[str, str]] = None,
) -> Tuple[List[dict], str]:
    """按用户问题匹配清单内的攻略条目。

    返回 (命中的 guide 列表, miss_reason)。匹配口径与 GameKee 源保持一致：
    1. 别名归一（config alias→official，再叠清单 role_aliases 的 B 站译名映射）；
    2. 角色命中优先于属性：清单 role 出现在问句中，或问句角色词与 role 互
       为前缀（"科洛妮丝" ↔ "科洛妮丝新春"）；分篇条目按 `·分篇` 后缀处理——
       问句点名某篇只发该篇，未点名发该角色全部条目；
    3. 属性泛查（"水系攻略"/"风队"）→ 该属性在清单内已收录的全部条目。

    `generic: true` 的条目不参与任何匹配（如"角色分位榜"这类泛用图，不属于
    任何角色；它们只留在清单里供维护脚本与人工查阅）。
    """
    q = (query or "").strip()
    if not q:
        return [], "empty_query"
    q_norm = q.replace("(", "（").replace(")", "）")
    # 用户配置的别名优先（配置语义 alias→official），再叠清单内的 B 站译名映射
    alias_map = {k: v for k, v in (aliases or {}).items() if k and v}
    for alias, official in alias_map.items():
        if alias in q_norm:
            q_norm = q_norm.replace(alias, official)
    for alias, official in _apply_role_aliases(manifest).items():
        if alias in _normalize_role(q_norm):
            q_norm = _normalize_role(q_norm).replace(alias, official)

    candidates = [
        g for g in (manifest.get("guides") or [])
        if not g.get("generic") and str(g.get("role") or "").strip()
    ]

    # 问句角色词 = 去掉泛查后缀词的剩余主干（与 gamekee.match_guides 同口径）
    core = re.sub(r"(攻略|怎么玩|玩法|一图流|推荐|配队|指南)", "", q_norm).strip("　 ")

    # 分篇/形态点名：问句里出现的形态词（清单内已有的 `·分篇` 后缀，或常见形态词）。
    # 判定"点名了形态"不能只看清单已知后缀——否则问「科洛妮丝直伤篇」时因为清单
    # 只收录了新春篇、没有 `·直伤篇` 后缀，就会静默降级发新春篇（答非所问）。
    known_parts = {
        g["role"].split("·", 1)[1]
        for g in candidates
        if "·" in str(g.get("role")) and g["role"].split("·", 1)[1]
    }
    form_words = known_parts | set(_FORM_WORDS)
    part_named = {w for w in form_words if w in q_norm}

    hits: List[dict] = []
    for guide in candidates:
        role = str(guide["role"])
        base = role.split("·", 1)[0]
        matched = (
            role in q_norm
            or _normalize_role(base) in _normalize_role(q_norm)
            or (
                core
                and (
                    _normalize_role(base).startswith(_normalize_role(core))
                    or _normalize_role(core).startswith(_normalize_role(base))
                )
            )
        )
        if not matched:
            continue
        if "·" in role:
            part = role.split("·", 1)[1]
            if part_named and part not in part_named:
                continue
        hits.append(guide)
    if hits:
        return _order_default_within_role(_uniq_guides(hits)), ""

    # 问句点名了某个形态/分篇，但清单里没有这个形态：不降级到基础形态或属性泛查。
    # 例：「科洛妮丝直伤篇」只收录了「科洛妮丝(新春)」时，发新春篇会答非所问。
    if part_named:
        return [], "form_not_found"

    # 属性泛查：问句含「属性词 + 队/系/属性」或裸属性名
    elems = _match_element(q_norm)
    if elems:
        grouped = [g for g in candidates if g.get("element") in elems]
        if grouped:
            return _order_default_within_role(_uniq_guides(grouped)), ""
    return [], "no_match"


def _order_default_within_role(guides: List[dict]) -> List[dict]:
    """同角色多条目时 default=true 的排前；不同角色之间保持命中顺序。

    只对**同一 role** 的连续组内排序——全局按 default 排会把不同角色的顺序也
    打乱（如属性泛查里把千都世的推荐条提到艾蕾前面）。
    """
    out: List[dict] = []
    for role in dict.fromkeys(str(g.get("role") or "") for g in guides):
        group = [g for g in guides if str(g.get("role") or "") == role]
        out.extend(sorted(group, key=lambda g: 0 if g.get("default") else 1))
    return out


def _uniq_guides(guides: List[dict]) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for guide in guides:
        key = str(guide.get("opus_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(guide)
    return out


_ELEMENT_CNS = {"water": "水", "fire": "火", "light": "光", "dark": "暗", "wind": "风", "earth": "地"}

# 常见分篇/流派形态词：问句点名这些词而清单无对应条目时判为 form_not_found，
# 不降级发基础形态（例：清单只收录「科洛妮丝(新春)」，问「科洛妮丝直伤篇」不该发新春篇）
_FORM_WORDS = ("印记篇", "直伤篇", "技伤篇", "流派1", "流派2", "省流版", "小作文版")


def _match_element(q: str) -> List[str]:
    """问句 → 命中的属性键列表（含单字属性名包含匹配，与 GameKee 源同口径）。"""
    hits = []
    for key, cn in _ELEMENT_CNS.items():
        if f"{cn}队" in q or f"{cn}系" in q or f"{cn}属性" in q or q == cn or cn in q:
            hits.append(key)
    return hits


# ===== opus 详情 =====


def parse_opus_detail(payload: Any, opus_id: str) -> Dict[str, Any]:
    """opus/detail 响应 → {title, image_urls}（结构不符抛 BiliGuideError）。

    只认 para_type==2 的图片段落；文本段落忽略（攻略信息在图里）。
    """
    if not isinstance(payload, dict):
        raise BiliGuideError(f"opus 详情响应不是对象: opus={opus_id}")
    code = payload.get("code")
    if code != 0:
        raise BiliGuideError(
            f"opus 详情业务码非 0: opus={opus_id} code={code!r} message={payload.get('message')!r}"
        )
    data = payload.get("data")
    item = (data or {}).get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise BiliGuideError(f"opus 详情缺 data.item: opus={opus_id}")
    modules = item.get("modules")
    if not isinstance(modules, list):
        raise BiliGuideError(f"opus 详情缺 modules 数组: opus={opus_id}")

    title = ""
    urls: List[str] = []
    for module in modules:
        if not isinstance(module, dict):
            continue
        mtype = module.get("module_type")
        if mtype == "MODULE_TYPE_TITLE":
            title = str((module.get("module_title") or {}).get("text") or "").strip()
        elif mtype == "MODULE_TYPE_CONTENT":
            paragraphs = (module.get("module_content") or {}).get("paragraphs") or []
            for para in paragraphs:
                if not isinstance(para, dict) or para.get("para_type") != 2:
                    continue
                pics = ((para.get("pic") or {}).get("pics")) or []
                for pic in pics:
                    url = str((pic or {}).get("url") or "").strip()
                    if url:
                        urls.append(url)
    if not urls:
        raise BiliGuideError(f"opus 详情无图片段落: opus={opus_id} title={title!r}")
    return {"title": title, "image_urls": urls}


def fetch_opus_detail(
    opus_id: str,
    proxy_url: Optional[str] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    """请求 opus 详情（同步；调用方负责 to_thread）。"""
    url = f"{_OPUS_DETAIL_API}?id={opus_id}"
    try:
        body = _read_http_bytes(
            url,
            allowed_hosts=(_OPUS_DETAIL_HOST,),
            headers={"User-Agent": _UA, "Referer": _OPUS_REFERER},
            proxy_url=proxy_url,
            timeout=timeout,
            max_bytes=_MAX_DETAIL_BYTES,
        )
    except _HttpBytesError as exc:
        raise BiliGuideError(f"opus 详情请求失败: opus={opus_id}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BiliGuideError(f"opus 详情不是合法 JSON: opus={opus_id}: {exc}") from exc
    return parse_opus_detail(payload, opus_id)


# ===== 图片下载与 webp 压缩 =====


def download_image(url: str, proxy_url: Optional[str] = None, timeout: int = 30) -> bytes:
    """下载原图字节（主机白名单 + 大小上限；格式由解码器判断）。"""
    # 接口返回的是 http 直链；hdslb 两种 scheme 都 200，统一升 https 避免明文
    fetch_url = url.replace("http://", "https://", 1) if url.startswith("http://") else url
    try:
        data = _read_http_bytes(
            fetch_url,
            allowed_hosts=_IMAGE_HOSTS,
            headers={"User-Agent": _UA, "Referer": _OPUS_REFERER},
            proxy_url=proxy_url,
            timeout=timeout,
            max_bytes=_MAX_IMAGE_BYTES,
        )
    except _HttpBytesError as exc:
        raise BiliGuideError(f"图片下载失败: {exc}") from exc
    if not data:
        raise BiliGuideError(f"图片响应为空: {fetch_url[:120]}")
    return data


def ensure_webp_encoder_available() -> None:
    """发请求前先确认 Pillow + WebP 编码器可用（冷态检查，别白下载几 MB 才失败）。

    宿主 Python 必须带 Pillow 且编译了 WebP 支持；缺失即抛 BiliGuideError，
    由调用方转成用户可见提示。
    """
    try:
        from PIL import features
    except ImportError as exc:
        raise BiliGuideError(f"Pillow 不可用，无法把攻略图压成 webp: {exc}") from exc
    if not features.check("webp"):
        raise BiliGuideError(
            "宿主 Pillow 缺少 WebP 编码支持（features.check('webp') 为 False），"
            "无法把攻略图压成 webp"
        )


def to_webp(raw: bytes, opus_id: str, index: int) -> bytes:
    """原图字节 → webp 字节（原分辨率、quality=88、method=6）。

    Pillow 缺失或编码失败一律抛 BiliGuideError（webp 是硬要求：B 站原图单张
    3-6MB，直接发原图既慢又会被平台二次压缩）。
    """
    try:
        from PIL import Image
    except ImportError as exc:  # 宿主 Python 必须带 Pillow，缺失即报错不降级
        raise BiliGuideError(
            f"Pillow 不可用，无法把攻略图压成 webp: opus={opus_id} 第{index}张: {exc}"
        ) from exc

    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            # 按单图尺寸检查上限，不改全局 Image.MAX_IMAGE_PIXELS（避免污染宿主内
            # 其它 Pillow 使用方；解压炸弹在这里就挡住，不等解码时抛）
            if width * height > _MAX_PIXELS:
                raise BiliGuideError(
                    f"攻略图尺寸超限: opus={opus_id} 第{index}张 {width}x{height}"
                    f"（上限 {_MAX_PIXELS} 像素）"
                )
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                # 调色板/灰度/CMYK 统一成 RGB；RGBA 保留透明（webp 支持）
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=_WEBP_QUALITY, method=_WEBP_METHOD)
            out = buf.getvalue()
    except BiliGuideError:
        raise
    except Exception as exc:
        raise BiliGuideError(
            f"攻略图 webp 编码失败: opus={opus_id} 第{index}张: {type(exc).__name__}: {exc}"
        ) from exc
    if not out:
        raise BiliGuideError(f"攻略图 webp 编码结果为空: opus={opus_id} 第{index}张")
    return out


# ===== 磁盘缓存（永久，无 TTL/无容量淘汰）=====


def opus_cache_dir(cache_root: Path, opus_id: str) -> Path:
    return Path(cache_root) / str(opus_id)


def _staging_dir(cache_root: Path, opus_id: str) -> Path:
    return Path(cache_root) / f"{opus_id}{_STAGING_SUFFIX}"


def _read_meta(opus_dir: Path) -> Optional[dict]:
    meta_path = opus_dir / _META_NAME
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("猫猫球缓存 meta 损坏（按缺失处理，重建）: %s: %s", meta_path, exc)
        return None
    return meta if isinstance(meta, dict) else None


def _is_webp_file(path: Path) -> bool:
    """文件是否 WebP（RIFF....WEBP 头 12 字节）。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return len(head) == 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _cached_files(opus_dir: Path, meta: dict) -> Optional[List[Path]]:
    """按 meta 校验缓存完整性：类型、张数、文件存在且为 WebP。

    任一项不符即返回 None（触发重建）。文件级别不够可信：目录可能被半途写坏，
    所以连签名一起验，避免把"新图+旧 meta"的混合状态认成完整缓存。
    """
    if not isinstance(meta, dict):
        return None
    raw_count = meta.get("count")
    if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count <= 0:
        return None
    files = [opus_dir / f"{i:04d}.webp" for i in range(1, raw_count + 1)]
    for path in files:
        if not path.is_file() or path.stat().st_size <= 0 or not _is_webp_file(path):
            return None
    return files


def _rmtree(path: Path) -> None:
    """删目录树（staging 清理用；不存在视为已清理）。"""
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        try:
            child.unlink() if child.is_file() else child.rmdir()
        except OSError as exc:
            logger.warning("清理暂存残留失败: %s: %s", child, exc)
    try:
        path.rmdir()
    except OSError as exc:
        logger.warning("清理暂存目录失败: %s: %s", path, exc)


def _download_and_encode(
    url: str,
    opus_id: str,
    index: int,
    staging: Path,
    proxy_url: Optional[str],
) -> Path:
    """下载单张 → webp → 写入 staging（线程池任务）。"""
    raw = download_image(url, proxy_url=proxy_url)
    webp = to_webp(raw, opus_id, index)
    target = staging / f"{index:04d}.webp"
    try:
        target.write_bytes(webp)
    except OSError as exc:
        raise BiliGuideError(
            f"攻略图落盘失败: opus={opus_id} 第{index}张: {exc}"
        ) from exc
    return target


def fetch_guide_images(
    opus_id: str,
    cache_root: Path,
    proxy_url: Optional[str] = None,
) -> List[Path]:
    """取一条 opus 的 webp 图片路径列表（缓存命中零网络）。

    单飞：同 opus_id 的并发调用由模块级锁串行，后到者直接命中前者的缓存。
    重建纪律：全部图先写进 `<opus_id>.staging/`，整批成功后才用目录 rename 切换
    到正式目录——旧缓存（含旧 meta）在切换成功前一直完整可读，任何一张失败都
    只丢弃 staging，不会出现"新图 + 旧 meta"的假完整缓存。
    """
    cache_root = Path(cache_root)
    opus_dir = opus_cache_dir(cache_root, opus_id)
    staging = _staging_dir(cache_root, opus_id)
    with _opus_lock(str(opus_id)):
        meta = _read_meta(opus_dir)
        if meta is not None:
            files = _cached_files(opus_dir, meta)
            if files is not None:
                logger.info("猫猫球缓存命中: opus=%s 共 %d 张（零网络）", opus_id, len(files))
                return files
            logger.info("猫猫球缓存不完整，重建: opus=%s", opus_id)

        # 冷态前置检查：Pillow/WebP 不可用就不要开始下载（省掉几 MB 无效流量）
        ensure_webp_encoder_available()

        # 先确认缓存根可用再发请求（目录被同名文件占位时立刻暴露，别白下载）
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BiliGuideError(
                f"猫猫球缓存目录不可用: opus={opus_id} path={cache_root}: {exc}"
            ) from exc

        detail = fetch_opus_detail(opus_id, proxy_url=proxy_url)
        urls = detail["image_urls"]

        _rmtree(staging)  # 上一轮中断的残留
        try:
            staging.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise BiliGuideError(
                f"猫猫球缓存暂存目录不可用: opus={opus_id} path={staging}: {exc}"
            ) from exc

        try:
            with ThreadPoolExecutor(max_workers=_IMAGE_WORKERS) as pool:
                futures = [
                    pool.submit(_download_and_encode, url, str(opus_id), index, staging, proxy_url)
                    for index, url in enumerate(urls, start=1)
                ]
                # 任一张失败就抛出（其余线程随 with 退出收敛）
                written = [f.result() for f in futures]
        except BiliGuideError:
            _rmtree(staging)
            raise
        except Exception as exc:
            _rmtree(staging)
            raise BiliGuideError(f"攻略图批量抓取失败: opus={opus_id}: {exc}") from exc

        meta_payload = {
            "opus_id": str(opus_id),
            "title": detail.get("title") or "",
            "count": len(written),
            "urls": urls,
            "fetched_at": int(time.time()),
        }
        try:
            _atomic_write(staging / _META_NAME, json.dumps(meta_payload, ensure_ascii=False, indent=1))
        except OSError as exc:
            _rmtree(staging)
            raise BiliGuideError(
                f"缓存 meta 写入失败: opus={opus_id}: {exc}"
            ) from exc

        # 目录级原子切换：旧目录先移开，staging 顶上，再删旧的
        backup = cache_root / f"{opus_id}.old"
        try:
            _rmtree(backup)
            if opus_dir.exists():
                opus_dir.rename(backup)
            staging.rename(opus_dir)
        except OSError as exc:
            # 切换失败：尽力回滚（backup 还在就换回去），再明确抛错
            if not opus_dir.exists() and backup.exists():
                try:
                    backup.rename(opus_dir)
                except OSError as rollback_exc:
                    logger.error(
                        "缓存切换失败且回滚失败: opus=%s: %s", opus_id, rollback_exc
                    )
            _rmtree(staging)
            raise BiliGuideError(
                f"缓存目录切换失败: opus={opus_id}: {exc}"
            ) from exc
        _rmtree(backup)

        final_files = [opus_dir / f"{i:04d}.webp" for i in range(1, len(written) + 1)]
        logger.info(
            "猫猫球缓存已写入: opus=%s 共 %d 张（%s）",
            opus_id, len(final_files), detail.get("title") or "-",
        )
        return final_files
