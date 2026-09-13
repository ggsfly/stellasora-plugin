from pathlib import Path
from typing import Optional
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
else:
    from cache import CacheManager
    from net_common import (
        atomic_write as _atomic_write,
        build_opener as _build_opener,
        read_offline_file as _read_offline_file,
        resolve_offline_dir,
        resolve_proxy as _resolve_proxy,
    )


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
        self.offline_dir = resolve_offline_dir(cache_dir, offline_dir)

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
