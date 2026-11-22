# -*- coding: utf-8 -*-
"""当期排行榜数据层（自 tools/service.py 与 tools/fetcher_stelladb.py 原样迁出）。

1.5.0 重写：榜单的赛季指针、BB meta 降级、FE 未开榜判定、TTL 瘦身缓存、
下载与瘦身落盘全部集中到本模块，语义零变化。渲染层（1.4.x 的文本转发节点）
留在 tools/service.py 直到步骤 5 切换，届时本模块只提供数据。

上游：AutumnVN/ssleaderboard（GitHub raw）。缓存目录由调用方显式传入，
1.5.0 起活动榜单缓存位于 <data_dir>/leaderboard/（旧 offline/ssleaderboard/
不再读写）。渲染层在 tools/leaderboard_render.py（本模块纯数据）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request

from net_common import atomic_write as _atomic_write

logger = logging.getLogger("stellasora.leaderboard")

_SS_LB_BASE = "https://raw.githubusercontent.com/AutumnVN/ssleaderboard/refs/heads/main"

# 赛季前缀 → 中文模式名（与 ssleaderboard 仓库 sid 约定一致）
LB_MODE_CN = {"bb": "联合讨伐", "fe": "终焉绝响"}

# 每个转发节点承载的排名条数：Top100 拆成表头 + 4 块共 5 节点，
# 卡片预览有层次且单节点不至于过长



def format_age(seconds: float) -> str:
    """缓存年龄 → 相对时间文本（文字榜表头用，1.4.x 口径）。"""
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{int(seconds // 3600)} 小时前"


def format_fetch_time(fetched_at: float) -> str:
    """抓取时刻 → 时间文本（图片榜 meta 用，`MM-DD HH:MM`，24 小时制）。

    相对时间每分钟都在变：同一份缓存数据在 TTL 内会渲染出逐分钟不同的图，既
    浪费渲染也让"同数据同图"不成立。只显示到分钟则数据不变文本不变，渲染输入
    随之完全一致。始终带日期：跨午夜后仍能看出数据是哪天抓的（当天折叠成
    HH:MM 会让昨天的榜单看起来像刚抓的）。
    """
    return time.strftime("%m-%d %H:%M", time.localtime(fetched_at))


def board_fingerprint(cn_data: Dict[str, Any]) -> str:
    """国服榜单数据指纹（total + 每名 rank/id/score）。

    用于判断"这份数据是否与上次渲染时完全相同"：配合精确时间戳（同数据 →
    同 HTML → 同图），指纹相同即可直接复用上次的渲染结果。
    """
    top = cn_data.get("top") or []
    parts = [str(int(cn_data.get("total") or 0))]
    parts.extend(f"{e.get('rank')}/{e.get('id')}/{e.get('score')}" for e in top)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def load_top(leaderboard_dir: Path, sid: str) -> Tuple[Optional[dict], float]:
    """读取 {sid}_top.json 瘦身缓存，返回 (data, 距上次拉取的秒数)。

    无文件 / 结构损坏 / 时间戳缺失一律返回 (None, inf)——由调用方决定重拉。
    """
    f = leaderboard_dir / f"{sid}_top.json"
    if not f.is_file():
        return None, float("inf")
    try:
        payload = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("榜单瘦身缓存损坏（按缺失处理，触发重拉）: %s: %s", f.name, exc)
        return None, float("inf")
    except OSError as exc:
        logger.warning("榜单瘦身缓存不可读（按缺失处理）: %s: %s", f.name, exc)
        return None, float("inf")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("regions"), dict):
        return None, float("inf")
    ts = float(payload.get("timestamp") or 0)
    return data, (time.time() - ts) if ts > 0 else float("inf")


def extract_board_top(raw: Any) -> Optional[dict]:
    """从赛季原始 JSON 提取玩家榜瘦身结构。

    只保留渲染所需字段（各服 total / last_refresh / top 的 rank/id/name/score），
    丢弃 Potentials/Notes/队伍明细等大体积字段与 Usage* 预聚合。
    结构不符（缺 region 或全空）返回 None。
    """
    if not isinstance(raw, dict):
        return None
    region = raw.get("region")
    if not isinstance(region, dict):
        return None

    slim_regions: Dict[str, dict] = {}
    for rk, rd in region.items():
        if not isinstance(rd, dict):
            continue
        top = []
        for entry in rd.get("Rank") or []:
            if not isinstance(entry, dict):
                continue
            top.append(
                {
                    "rank": entry.get("Rank"),
                    "id": str(entry.get("Id") or ""),
                    "name": str(entry.get("NickName") or "").strip(),
                    "score": entry.get("Score") or 0,
                }
            )
        slim_regions[str(rk)] = {
            "total": rd.get("Total") or 0,
            "last_refresh": str(rd.get("LastRefreshTime") or ""),
            "top": top,
        }
    return {"regions": slim_regions} if slim_regions else None


def fetch_json_from_url(url: str, proxy_url: Optional[str] = None, retries: int = 1) -> Optional[dict]:
    """抓取 JSON 数据并解析为 dict（带 1 次重试）。

    proxy_url 直接透传给 `build_opener`（唯一解析点）：""=强制直连且不读环境变量，
    非空=用该代理，None=环境变量链。这里**不做预解析**——空串先被 resolve 成 None
    再交给 build_opener 会被解释成"读环境变量"，"留空强制直连"就失效了。
    """
    from net_common import build_opener as _build_opener

    opener = _build_opener(proxy_url)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=30) as response:
                if response.getcode() == 200:
                    text = response.read().decode("utf-8")
                    data = json.loads(text)
                    if isinstance(data, dict):
                        return data
        except Exception:
            if attempt < retries:
                time.sleep(1.5)
    return None


def read_persisted_dataset(file_path: Path, name: str) -> Optional[dict]:
    """读取 ssleaderboard 离线持久化 JSON 数据集（meta/season），带 mtime 模块级缓存。

    语义同旧 fetcher_stelladb._read_offline_dataset（仅榜单使用；外层包装
    {"url","name","timestamp","data"} 提取 data 字段，容错直接数据集 dict）。
    """
    if not file_path.is_file():
        return None
    try:
        mtime = file_path.stat().st_mtime
    except OSError:
        mtime = -1.0

    cache_key = (file_path.resolve(), name)
    cached = _dataset_cache.get(cache_key)
    if cached is not None:
        cached_mtime, cached_data = cached
        if cached_mtime == mtime:
            return cached_data

    try:
        text = file_path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        payload = json.loads(text)
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                _dataset_cache[cache_key] = (mtime, data)
                return data
            # 容错：若文件直接为数据集 dict（无外层包装且非包装字段）
            if "data" not in payload and "url" not in payload:
                _dataset_cache[cache_key] = (mtime, payload)
                return payload
        return None
    except Exception:
        return None


# 模块级数据集缓存：(文件绝对路径, 数据集名称) -> (mtime, parsed_dict)
_dataset_cache: Dict[Tuple[Path, str], Tuple[float, dict]] = {}


def _persist_dataset(file_path: Path, url: str, name: str, data: dict) -> None:
    """数据集原子落盘（外层包装含 url/name/timestamp），并刷新 mtime 缓存。"""
    payload = {
        "url": url,
        "name": name,
        "timestamp": int(time.time()),
        "data": data,
    }
    try:
        _atomic_write(file_path, json.dumps(payload, ensure_ascii=False, indent=2))
        mtime = file_path.stat().st_mtime
        _dataset_cache[(file_path.resolve(), name)] = (mtime, data)
    except Exception:
        pass


def fetch_leaderboard_meta(
    leaderboard_dir: Path,
    proxy_url: Optional[str] = None,
    force_update: bool = False,
) -> Optional[dict]:
    """获取 ssleaderboard 元数据，本地优先（语义同旧 fetcher，路径换新目录）。"""
    offline_file = leaderboard_dir / "meta.json"
    if not force_update:
        offline_data = read_persisted_dataset(offline_file, "leaderboard")
        if offline_data is not None:
            return offline_data

    url = f"{_SS_LB_BASE}/meta.json"
    res = fetch_json_from_url(url, proxy_url=proxy_url)
    if res is not None:
        _persist_dataset(offline_file, url, "leaderboard", res)
        return res

    return read_persisted_dataset(offline_file, "leaderboard")


def fetch_leaderboard_season(
    leaderboard_dir: Path,
    proxy_url: Optional[str] = None,
    force_update: bool = False,
) -> Optional[dict]:
    """获取 ssleaderboard 赛季数据，本地优先（语义同旧 fetcher，路径换新目录）。"""
    offline_file = leaderboard_dir / "season.json"
    if not force_update:
        offline_data = read_persisted_dataset(offline_file, "season")
        if offline_data is not None:
            return offline_data

    url = f"{_SS_LB_BASE}/season.json"
    res = fetch_json_from_url(url, proxy_url=proxy_url)
    if res is not None:
        _persist_dataset(offline_file, url, "season", res)
        return res

    return read_persisted_dataset(offline_file, "season")


def fetch_leaderboard_board(
    leaderboard_dir: Path,
    season_id: str,
    proxy_url: Optional[str] = None,
    force_update: bool = False,
) -> Tuple[Optional[dict], str]:
    """获取当期赛季玩家榜瘦身数据，返回 (data, status)。

    上游 {sid}.json 为 20MB 级单体文件（95% 体积是 Potentials/Notes 明细），
    这里下载后仅提取各服玩家榜 Top 列表（extract_board_top），raw 不落盘；
    瘦身结果持久化到 {sid}_top.json 供 TTL 内零网络复用。

    status: "ok"=成功（网络拉取或离线读取）；"not_found"=上游无该赛季文件
    （当期未开榜，如 fe 赛季初）；"error"=网络/解析失败。
    """
    sid = str(season_id or "").strip().lower()
    if not sid:
        return None, "error"

    offline_file = leaderboard_dir / f"{sid}_top.json"
    if not force_update:
        offline_data = read_persisted_dataset(offline_file, f"board_{sid}")
        if offline_data is not None:
            return offline_data, "ok"

    url = f"{_SS_LB_BASE}/{sid}.json"
    try:
        from net_common import build_opener as _build_opener

        opener = _build_opener(proxy_url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=60) as response:
            if response.getcode() != 200:
                logger.warning("赛季榜单拉取失败: %s HTTP %s", sid, response.getcode())
                return None, "error"
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info("赛季榜单文件不存在（当期可能未开榜）: %s", sid)
            return None, "not_found"
        logger.warning("赛季榜单拉取失败: %s HTTP %s", sid, exc.code)
        return None, "error"
    except Exception:
        logger.warning("赛季榜单拉取异常: %s", sid, exc_info=True)
        return None, "error"

    slim = extract_board_top(raw)
    if slim is None:
        logger.warning("赛季榜单解析失败（结构不符）: %s", sid)
        return None, "error"

    payload = {
        "url": url,
        "name": f"board_{sid}",
        "timestamp": int(time.time()),
        "data": slim,
    }
    try:
        _atomic_write(offline_file, json.dumps(payload, ensure_ascii=False, indent=2))
        _dataset_cache[(offline_file.resolve(), f"board_{sid}")] = (
            offline_file.stat().st_mtime,
            slim,
        )
    except Exception:
        pass
    return slim, "ok"


# ===== 渲染层（1.4.x 文本节点；步骤 5 切换图片渲染后本段被 leaderboard_render 取代）=====


def resolve_current_board(
    mode: str,
    leaderboard_dir: Path | None = None,
    proxy_url: Optional[str] = None,
) -> Tuple[str, str]:
    """定位当期赛季 sid（/st_bb・/st_fe 数据源指针逻辑，语义同旧 query_lb_board）。

    返回 (prefix, sid)。bb 指针缺失时按 meta 键表取最大 bbNN 降级；fe 严格
    跟随指针、缺失即空（"不猜、不兜底"）。mode 非法抛 ValueError。
    """
    prefix = str(mode or "").strip().lower()
    if prefix not in LB_MODE_CN:
        raise ValueError(f"未知排行榜类型: {mode}")
    if leaderboard_dir is None:
        from net_common import host_data_dir

        leaderboard_dir = host_data_dir() / "leaderboard"

    season = fetch_leaderboard_season(leaderboard_dir, proxy_url=proxy_url, force_update=True)
    if prefix == "bb":
        sid = str((season or {}).get("BB_SEASON") or "")
        if not sid:
            meta = fetch_leaderboard_meta(leaderboard_dir, proxy_url=proxy_url)
            candidates = sorted(
                (k for k in (meta or {}) if re.match(r"^bb\d+$", k)),
                key=lambda k: int(k[2:]),
                reverse=True,
            )
            sid = candidates[0] if candidates else ""
    else:
        sid = str((season or {}).get("FE_SEASON") or "")
    return prefix, sid


def load_board_for_render(
    prefix: str,
    sid: str,
    ttl_minutes: int = 10,
    leaderboard_dir: Path | None = None,
    proxy_url: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str, float]:
    """取当期国服榜单瘦身数据（供图片渲染层消费），返回 (cn_data, header, age)。

    notice 语义收敛为异常抛出（BoardUnavailable），正常路径恒返回数据：
    - 指针缺失 / 未开榜 / 拉取失败且无缓存 → 抛 BoardUnavailable（notice 文案
      作为异常消息，调用方走短文本）；
    - 拉取失败但有历史缓存 → 降级返回陈旧数据（年龄由表头"更新于"自明）。
    cn_data 为 regions.cn dict（total/top），ttl 内直读本地瘦身缓存零网络。
    """
    mode_cn = LB_MODE_CN.get(prefix, prefix.upper())
    if not sid:
        raise BoardUnavailable(f"【{mode_cn}】当期赛季指针缺失。")
    if leaderboard_dir is None:
        from net_common import host_data_dir

        leaderboard_dir = host_data_dir() / "leaderboard"

    match = re.fullmatch(r"[a-z]+(\d+)", sid)
    season_num = match.group(1) if match else "?"
    header = f"【{mode_cn} S{season_num}】"

    ttl_seconds = max(0, int(ttl_minutes)) * 60
    data, age = load_top(leaderboard_dir, sid)
    if data is None or age > ttl_seconds:
        slim, status = fetch_leaderboard_board(
            leaderboard_dir, sid, proxy_url=proxy_url, force_update=True
        )
        if status == "ok" and slim is not None:
            data, age = slim, 0.0
        elif status == "not_found":
            raise BoardUnavailable(f"{header} 当期未开榜（上游尚未发布榜单数据）。")
        elif data is None:
            raise BoardUnavailable(f"{header} 榜单拉取失败，请稍后再试。")

    cn = (data or {}).get("regions", {}).get("cn")
    if not isinstance(cn, dict) or not cn.get("top"):
        raise BoardUnavailable(f"{header} 国服暂无榜单数据。")
    return cn, header, age


class BoardUnavailable(RuntimeError):
    """榜单当期不可用（指针缺失/未开榜/拉取失败且无缓存/国服空），消息即用户可见文案。"""



# 每个文字转发节点承载的排名条数（1.4.x 文字榜口径原样保留）
_LB_TEXT_CHUNK = 25

_LB_TEXT_NICKNAME = "星塔旅人"


def _lb_text_node(text: str) -> Dict[str, Any]:
    return {"nickname": _LB_TEXT_NICKNAME, "segments": [{"type": "text", "content": text}]}


def render_text_board_nodes(
    cn_data: Dict[str, Any],
    header: str,
    age_seconds: float,
) -> List[Dict[str, Any]]:
    """国服榜单 → 文字合并转发节点（1.4.x 旧格式原样复刻，供 render_images=false）。

    表头节点 1 条写实际条数（赛季初上游 top 可能不足 100，写死 Top100 会失真）；
    其后每 25 名一节点，首行"第 X–Y 名"，每行 `{rank}. {name} — {score:,}`。
    排名不补零、分数完整千分位（用户红线：禁"万"缩写）。
    """
    top = cn_data.get("top") or []
    total = int(cn_data.get("total") or 0)
    nodes = [
        _lb_text_node(f"{header} 国服 Top{len(top)} · 共 {total:,} 人 · 更新于 {format_age(age_seconds)}")
    ]
    for start in range(0, len(top), _LB_TEXT_CHUNK):
        chunk = top[start:start + _LB_TEXT_CHUNK]
        lines = [f"第 {chunk[0].get('rank')}–{chunk[-1].get('rank')} 名"]
        for entry in chunk:
            rank = entry.get("rank")
            name = entry.get("name") or "?"
            score = int(entry.get("score") or 0)
            lines.append(f"{rank}. {name} — {score:,}")
        nodes.append(_lb_text_node("\n".join(lines)))
    return nodes




def refresh_board_for_update(
    leaderboard_dir: Path,
    proxy_url: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """/st_update 强刷两榜（P1 强刷语义：绕过榜单内容 TTL，保留指针语义）。

    用同一份新指针分别处理两榜；各自独立返回状态
    {"bb": {"status": "ok|not_found|error|no_pointer", "detail": ...}, "fe": {...}}。
    一榜失败不吞掉另一榜（K 节断言：三者独立——两榜 + GameKee 探查互不影响）。
    强刷 = 对当期 sid 强制 fetch_leaderboard_board(force_update=True) 重建瘦身缓存。
    """
    results: Dict[str, Dict[str, str]] = {}
    try:
        season = fetch_leaderboard_season(leaderboard_dir, proxy_url=proxy_url, force_update=True)
    except Exception as exc:
        return {
            "bb": {"status": "error", "detail": str(exc)[:120]},
            "fe": {"status": "error", "detail": str(exc)[:120]},
        }

    for prefix in ("bb", "fe"):
        try:
            if prefix == "bb":
                sid = str((season or {}).get("BB_SEASON") or "")
                if not sid:
                    meta = fetch_leaderboard_meta(leaderboard_dir, proxy_url=proxy_url)
                    candidates = sorted(
                        (k for k in (meta or {}) if re.match(r"^bb\d+$", k)),
                        key=lambda k: int(k[2:]),
                        reverse=True,
                    )
                    sid = candidates[0] if candidates else ""
            else:
                sid = str((season or {}).get("FE_SEASON") or "")
            if not sid:
                results[prefix] = {"status": "no_pointer", "detail": "赛季指针缺失"}
                continue
            slim, status = fetch_leaderboard_board(
                leaderboard_dir, sid, proxy_url=proxy_url, force_update=True
            )
            if status == "ok" and slim is not None:
                cn = slim.get("regions", {}).get("cn", {})
                results[prefix] = {
                    "status": "ok",
                    "detail": f"{sid} Top{len(cn.get('top') or [])}",
                }
            elif status == "not_found":
                results[prefix] = {"status": "not_found", "detail": f"{sid} 当期未开榜"}
            else:
                results[prefix] = {"status": "error", "detail": f"{sid} 拉取失败"}
        except Exception as exc:
            results[prefix] = {"status": "error", "detail": str(exc)[:120]}
    return results
