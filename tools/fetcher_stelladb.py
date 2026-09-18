from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
import logging
import time
import urllib.request

if __package__:
    from .cache import CacheManager
    from .net_common import (
        atomic_write as _atomic_write,
        build_opener as _build_opener,
        read_offline_file as _read_offline_file,
        resolve_offline_dir,
        resolve_proxy as _resolve_proxy,
    )
    from .text_clean import extract_ssr_content
else:
    from cache import CacheManager
    from net_common import (
        atomic_write as _atomic_write,
        build_opener as _build_opener,
        read_offline_file as _read_offline_file,
        resolve_offline_dir,
        resolve_proxy as _resolve_proxy,
    )
    from text_clean import extract_ssr_content

_SS_DATA_BASE = "https://raw.githubusercontent.com/AutumnVN/ss-data/refs/heads/main"
_SS_LB_BASE = "https://raw.githubusercontent.com/AutumnVN/ssleaderboard/refs/heads/main"

logger = logging.getLogger("stellasora.fetcher")

# 模块级数据集缓存：(文件绝对路径, 数据集名称) -> (mtime, parsed_dict)
_ssdata_cache: Dict[Tuple[Path, str], Tuple[float, dict]] = {}

# 抓取后本地修正（上游脏数据补丁）：键为元素名，值为 (旧串, 新串) 替换对列表。
# 每次 fetch_infodoc 抓取落盘前应用——上游不改也保证本地恒为修正后数据，
# 且 17:00 st_update 重新抓取时自动重新修正（持久生效）。
_INFODOC_FIXES: Dict[str, List[Tuple[str, str]]] = {
    # 上游误标：Freesia (Main Skill) 队首个 Teresa (4★) 段实为 Freesia 的主技能 build
    #（含 Ice Vortex 等 Freesia 专属数据），改回 Freesia (5★) 使成员定位与数据归属正确。
    "aqua": [
        (
            "| Teresa (4★) |  |  |  |  |  |  |  |  | 1/10/1/1 (Main Skill only)",
            "| Freesia (5★) |  |  |  |  |  |  |  |  | 1/10/1/1 (Main Skill only)",
        ),
    ],
}


def _apply_infodoc_fixes(elem_key: str, text: str) -> str:
    """对抓取到的元素 infodoc 文本应用本地修正（_INFODOC_FIXES）。

    逐个执行 (old, new) 替换；旧串未命中（上游已自改或结构变更）时记录
    warning 并跳过，不改变文本——补丁幂等，重复抓取无副作用。
    """
    for old, new in _INFODOC_FIXES.get(elem_key, []):
        if old in text:
            text = text.replace(old, new)
        else:
            logger.warning("infodoc 修正未命中（上游可能已自改）: element=%s old=%r", elem_key, old[:60])
    return text


def _read_offline_dataset(file_path: Path, name: str) -> Optional[dict]:
    """读取 ssdata / ssleaderboard 离线持久化 JSON 数据集，带 mtime 模块级缓存。"""
    if not file_path.is_file():
        return None
    try:
        mtime = file_path.stat().st_mtime
    except OSError:
        mtime = -1.0

    cache_key = (file_path.resolve(), name)
    cached = _ssdata_cache.get(cache_key)
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
                _ssdata_cache[cache_key] = (mtime, data)
                return data
            # 容错：若文件直接为数据集 dict（无外层包装且非包装字段）
            if "data" not in payload and "url" not in payload:
                _ssdata_cache[cache_key] = (mtime, payload)
                return payload
        return None
    except Exception:
        return None


class StelladbFetcher:
    def __init__(
        self,
        cache_dir: Path,
        offline_dir: Optional[Path] = None,
        proxy: Optional[str] = None,
    ):
        """
        Args:
            cache_dir: 网络缓存目录。
            offline_dir: 离线数据存储目录（可选）。
            proxy: 代理地址，如 "http://127.0.0.1:7890"。
                   传入空字符串 "" 表示强制直连；
                   传入 None 则自动读取环境变量，否则使用默认代理 127.0.0.1:7890。
        """
        self.cache = CacheManager(cache_dir, ttl_seconds=3600)
        self.headers = {"User-Agent": "Mozilla/5.0"}
        self._proxy_url = _resolve_proxy(proxy)
        self._opener = _build_opener(self._proxy_url)
        self.offline_dir = resolve_offline_dir(cache_dir, offline_dir)

    def fetch_url(self, url: str, retries: int = 1, ignore_cache: bool = False) -> Optional[str]:
        """抓取 URL（带 1 次网络重试——stelladb 偶发 SSL 握手超时）。"""
        if not ignore_cache:
            cached = self.cache.get(url)
            if cached:
                return cached
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with self._opener.open(req, timeout=15) as response:
                    if response.getcode() == 200:
                        html = response.read().decode("utf-8")
                        text = extract_ssr_content(html)
                        self.cache.set(url, text)
                        return text
            except Exception:
                if attempt < retries:
                    time.sleep(1.5)  # 重试前短暂等待
        return None

    def fetch_infodoc(self, element: str, force_update: bool = False) -> str:
        """获取元素 infodoc 攻略，本地优先。"""
        elem_key = element.lower()
        offline_file = None
        if self.offline_dir:
            json_file = self.offline_dir / "infodocs" / f"{elem_key}.json"
            offline_file = json_file if json_file.exists() else None

        if not force_update and offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data

        url = f"https://stelladb.pages.dev/infodoc/{elem_key}"
        res = self.fetch_url(url, ignore_cache=force_update)
        if res:
            # 抓取后本地修正（上游脏数据补丁）：落盘前替换，保证持久正确
            res = _apply_infodoc_fixes(elem_key, res)
            target_file = self.offline_dir / "infodocs" / f"{elem_key}.json" if self.offline_dir else None
            if target_file:
                payload = {
                    "url": url,
                    "element": elem_key,
                    "timestamp": time.time(),
                    "data": res,
                }
                try:
                    _atomic_write(target_file, json.dumps(payload, ensure_ascii=False, indent=2))
                except Exception:
                    pass
            return res

        if offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data
        return "Error fetching infodoc."

    def fetch_infodoc_index(self, force_update: bool = False) -> str:
        """抓取 infodoc 索引页（含各元素队 Rotation / Main Slot / Supp Slot 信息），本地优先。"""
        offline_file = None
        if self.offline_dir:
            json_file = self.offline_dir / "infodocs" / "index.json"
            offline_file = json_file if json_file.exists() else None

        if not force_update and offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data

        url = "https://stelladb.pages.dev/infodoc"
        res = self.fetch_url(url, ignore_cache=force_update)
        if res:
            target_file = self.offline_dir / "infodocs" / "index.json" if self.offline_dir else None
            if target_file:
                payload = {
                    "url": url,
                    "element": "index",
                    "timestamp": time.time(),
                    "data": res,
                }
                try:
                    _atomic_write(target_file, json.dumps(payload, ensure_ascii=False, indent=2))
                except Exception:
                    pass
            return res

        if offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data
        return ""

    def _fetch_json_from_url(self, url: str, retries: int = 1) -> Optional[dict]:
        """抓取 JSON 数据并解析为 dict（带 1 次重试）。"""
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with self._opener.open(req, timeout=30) as response:
                    if response.getcode() == 200:
                        text = response.read().decode("utf-8")
                        data = json.loads(text)
                        if isinstance(data, dict):
                            return data
            except Exception:
                if attempt < retries:
                    time.sleep(1.5)
        return None

    def fetch_ssdata_dataset(self, name: str, force_update: bool = False) -> Optional[dict]:
        """获取 ss-data 数据集（character, disc, gacha, raid, item, word 等），本地优先。

        离线文件固定取 ssdata/<name>.json。
        """
        offline_dir = self.offline_dir
        offline_file: Optional[Path] = None
        if offline_dir is not None:
            offline_file = offline_dir / "ssdata" / f"{name}.json"
        if not force_update and offline_file:
            offline_data = _read_offline_dataset(offline_file, name)
            if offline_data is not None:
                return offline_data

        url = f"{_SS_DATA_BASE}/{name}.json"
        res = self._fetch_json_from_url(url)
        if res is not None:
            if offline_file:
                payload = {
                    "url": url,
                    "name": name,
                    "timestamp": int(time.time()),
                    "data": res,
                }
                try:
                    _atomic_write(offline_file, json.dumps(payload, ensure_ascii=False, indent=2))
                    mtime = offline_file.stat().st_mtime
                    _ssdata_cache[(offline_file.resolve(), name)] = (mtime, res)
                except Exception:
                    pass
            return res

        if offline_file:
            offline_data = _read_offline_dataset(offline_file, name)
            if offline_data is not None:
                return offline_data
        return None

    def fetch_leaderboard_meta(self, force_update: bool = False) -> Optional[dict]:
        """获取 ssleaderboard 元数据，本地优先。

        离线文件固定取 ssleaderboard/meta.json。
        """
        offline_dir = self.offline_dir
        offline_file: Optional[Path] = None
        if offline_dir is not None:
            offline_file = offline_dir / "ssleaderboard" / "meta.json"
        if not force_update and offline_file:
            offline_data = _read_offline_dataset(offline_file, "leaderboard")
            if offline_data is not None:
                return offline_data

        url = f"{_SS_LB_BASE}/meta.json"
        res = self._fetch_json_from_url(url)
        if res is not None:
            if offline_file:
                payload = {
                    "url": url,
                    "name": "leaderboard",
                    "timestamp": int(time.time()),
                    "data": res,
                }
                try:
                    _atomic_write(offline_file, json.dumps(payload, ensure_ascii=False, indent=2))
                    mtime = offline_file.stat().st_mtime
                    _ssdata_cache[(offline_file.resolve(), "leaderboard")] = (mtime, res)
                except Exception:
                    pass
            return res

        if offline_file:
            offline_data = _read_offline_dataset(offline_file, "leaderboard")
            if offline_data is not None:
                return offline_data
        return None

    def fetch_leaderboard_season(self, force_update: bool = False) -> Optional[dict]:
        """获取 ssleaderboard 赛季数据，本地优先。

        离线文件固定取 ssleaderboard/season.json。
        """
        offline_dir = self.offline_dir
        offline_file: Optional[Path] = None
        if offline_dir is not None:
            offline_file = offline_dir / "ssleaderboard" / "season.json"
        if not force_update and offline_file:
            offline_data = _read_offline_dataset(offline_file, "season")
            if offline_data is not None:
                return offline_data

        url = f"{_SS_LB_BASE}/season.json"
        res = self._fetch_json_from_url(url)
        if res is not None:
            if offline_file:
                payload = {
                    "url": url,
                    "name": "season",
                    "timestamp": int(time.time()),
                    "data": res,
                }
                try:
                    _atomic_write(offline_file, json.dumps(payload, ensure_ascii=False, indent=2))
                    mtime = offline_file.stat().st_mtime
                    _ssdata_cache[(offline_file.resolve(), "season")] = (mtime, res)
                except Exception:
                    pass
            return res

        if offline_file:
            offline_data = _read_offline_dataset(offline_file, "season")
            if offline_data is not None:
                return offline_data
        return None
