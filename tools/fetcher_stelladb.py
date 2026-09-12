from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import json
import os
import time
import urllib.request

if __package__:
    from .cache import CacheManager
    from .text_clean import extract_ssr_content
else:
    from cache import CacheManager
    from text_clean import extract_ssr_content

_OFFLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "offline"

# 默认代理地址；可通过环境变量 HTTPS_PROXY / HTTP_PROXY 或构造函数 proxy 参数覆盖
_DEFAULT_PROXY = "http://127.0.0.1:7890"

_SS_DATA_BASE = "https://raw.githubusercontent.com/AutumnVN/ss-data/refs/heads/main"
_SS_LB_BASE = "https://raw.githubusercontent.com/AutumnVN/ssleaderboard/refs/heads/main"

# 模块级数据集缓存：(文件绝对路径, 数据集名称) -> (mtime, parsed_dict)
_ssdata_cache: Dict[Tuple[Path, str], Tuple[float, dict]] = {}


def _atomic_write(file_path: Path, content: str) -> None:
    """原子写入文件：先写临时文件再 rename/replace 覆盖。"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = file_path.with_name(f".{file_path.name}.{time.time_ns()}.tmp")
    try:
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(file_path)
    except Exception:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass
        raise


def _read_offline_file(file_path: Path) -> Optional[str]:
    """读取离线持久化文件，若为 json 则提取 data 字段，若为纯文本则直接返回。"""
    if not file_path.is_file():
        return None
    try:
        text = file_path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        if file_path.suffix == ".json":
            data = json.loads(text)
            if isinstance(data, dict):
                content = data.get("data") or data.get("content")
                return content if isinstance(content, str) and content else None
            elif isinstance(data, str) and data:
                return data
        return text
    except Exception:
        return None


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


def _resolve_proxy(proxy: Optional[str]) -> Optional[str]:
    """解析最终代理地址。

    优先级（高→低）：
      1. 显式传入的 proxy 参数
      2. 环境变量 HTTPS_PROXY / HTTP_PROXY
      3. 默认值 http://127.0.0.1:7890

    传入空字符串 "" 表示强制直连（跳过代理）。
    """
    if proxy is not None:
        # 显式传入空串 → 直连
        return proxy.strip() or None
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if env_proxy:
        return env_proxy.strip()
    return _DEFAULT_PROXY


def _build_opener(proxy_url: Optional[str]) -> urllib.request.OpenerDirector:
    """根据代理地址创建 urllib opener。代理为 None 时透明直连。"""
    if proxy_url:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    else:
        # 显式禁用系统代理，保证直连
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler)


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

        if offline_dir is not None:
            self.offline_dir = Path(offline_dir)
        elif (cache_dir / "offline").is_dir():
            self.offline_dir = cache_dir / "offline"
        elif (cache_dir.parent / "offline").is_dir():
            self.offline_dir = cache_dir.parent / "offline"
        elif cache_dir.name in ("webcache", ".cache", ".cache_test") or "stellasora" in str(cache_dir).lower():
            self.offline_dir = _OFFLINE_DIR
        else:
            self.offline_dir = None

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

    def fetch_trekker(self, numeric_id: str, force_update: bool = False) -> str:
        """获取角色攻略数据，本地优先。"""
        offline_file = self.offline_dir / "trekkers" / f"{numeric_id}.json" if self.offline_dir else None
        if not force_update and offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data

        url = f"https://stelladb.pages.dev/trekker/{numeric_id}"
        res = self.fetch_url(url, ignore_cache=force_update)
        if res:
            if offline_file:
                payload = {
                    "url": url,
                    "id": str(numeric_id),
                    "timestamp": time.time(),
                    "data": res,
                }
                try:
                    _atomic_write(offline_file, json.dumps(payload, ensure_ascii=False, indent=2))
                except Exception:
                    pass
            return res

        if offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data
        return "Error fetching trekker."

    def fetch_disc(self, numeric_id: str, force_update: bool = False) -> str:
        """获取秘纹攻略数据，本地优先。"""
        offline_file = self.offline_dir / "discs" / f"{numeric_id}.json" if self.offline_dir else None
        if not force_update and offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data

        url = f"https://stelladb.pages.dev/disc/{numeric_id}"
        res = self.fetch_url(url, ignore_cache=force_update)
        if res:
            if offline_file:
                payload = {
                    "url": url,
                    "id": str(numeric_id),
                    "timestamp": time.time(),
                    "data": res,
                }
                try:
                    _atomic_write(offline_file, json.dumps(payload, ensure_ascii=False, indent=2))
                except Exception:
                    pass
            return res

        if offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data
        return "Error fetching disc."

    def fetch_infodoc(self, element: str, force_update: bool = False) -> str:
        """获取元素 infodoc 攻略，本地优先。"""
        elem_key = element.lower()
        offline_file = None
        if self.offline_dir:
            json_file = self.offline_dir / "infodocs" / f"{elem_key}.json"
            txt_file = self.offline_dir / "infodocs" / f"{elem_key}.txt"
            offline_file = json_file if json_file.exists() else txt_file

        if not force_update and offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data

        url = f"https://stelladb.pages.dev/infodoc/{elem_key}"
        res = self.fetch_url(url, ignore_cache=force_update)
        if res:
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
            txt_file = self.offline_dir / "infodocs" / "index.txt"
            offline_file = json_file if json_file.exists() else txt_file

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
        """获取 ss-data 数据集（character, disc, gacha, raid, item, word 等），本地优先。"""
        offline_file = self.offline_dir / "ssdata" / f"{name}.json" if self.offline_dir else None
        if offline_file and not offline_file.is_file() and (self.offline_dir / f"{name}.json").is_file():
            offline_file = self.offline_dir / f"{name}.json"
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
        """获取 ssleaderboard 元数据，本地优先。"""
        offline_file = self.offline_dir / "ssleaderboard" / "meta.json" if self.offline_dir else None
        if offline_file and not offline_file.is_file() and (self.offline_dir / "ssleaderboard_meta.json").is_file():
            offline_file = self.offline_dir / "ssleaderboard_meta.json"
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
