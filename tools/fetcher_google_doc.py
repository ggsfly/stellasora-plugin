import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from cache import CacheManager

_OFFLINE_DIR = Path(__file__).resolve().parent.parent / "data" / "offline"

# 默认代理地址；可通过环境变量 HTTPS_PROXY / HTTP_PROXY 或构造函数 proxy 参数覆盖
_DEFAULT_PROXY = "http://127.0.0.1:7890"


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
                content = data.get("data")
                return content if isinstance(content, str) and content else None
            elif isinstance(data, str) and data:
                return data
        return text
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


class GoogleDocFetcher:
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
        self.cache = CacheManager(cache_dir, ttl_seconds=86400)
        self.url = "https://docs.google.com/document/d/1FtGfPUNSJe8Psx4F3ZIcA5m8eBwoiTu8e504-Uw6ZmQ/export?format=txt"
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

    def fetch_presets(self, force_update: bool = False) -> str:
        """抓取预设码，本地优先。"""
        offline_file = None
        if self.offline_dir:
            txt_file = self.offline_dir / "presets" / "presets.txt"
            json_file = self.offline_dir / "presets" / "presets.json"
            offline_file = txt_file if txt_file.exists() else json_file

        if not force_update and offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data

        if not force_update:
            cached = self.cache.get(self.url)
            if cached:
                return cached

        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": "Mozilla/5.0"})
            with self._opener.open(req, timeout=15) as response:
                if response.getcode() == 200:
                    text = response.read().decode("utf-8")
                    self.cache.set(self.url, text)
                    if self.offline_dir:
                        target_file = self.offline_dir / "presets" / "presets.txt"
                        try:
                            _atomic_write(target_file, text)
                        except Exception:
                            pass
                    return text
        except Exception:
            pass

        if offline_file:
            offline_data = _read_offline_file(offline_file)
            if offline_data:
                return offline_data

        return "Error fetching presets."
