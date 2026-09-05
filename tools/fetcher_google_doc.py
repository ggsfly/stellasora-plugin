import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from cache import CacheManager

class GoogleDocFetcher:
    def __init__(self, cache_dir: Path):
        self.cache = CacheManager(cache_dir, ttl_seconds=86400)
        self.url = "https://docs.google.com/document/d/1FtGfPUNSJe8Psx4F3ZIcA5m8eBwoiTu8e504-Uw6ZmQ/export?format=txt"

    def fetch_presets(self) -> str:
        cached = self.cache.get(self.url)
        if cached: return cached
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.getcode() == 200:
                    text = response.read().decode("utf-8")
                    self.cache.set(self.url, text)
                    return text
        except Exception:
            pass
        return "Error fetching presets."
