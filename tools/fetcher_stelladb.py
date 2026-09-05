import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from cache import CacheManager
from text_clean import extract_ssr_content

class StelladbFetcher:
    def __init__(self, cache_dir: Path):
        self.cache = CacheManager(cache_dir, ttl_seconds=3600)
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def fetch_url(self, url: str) -> Optional[str]:
        cached = self.cache.get(url)
        if cached: return cached
        try:
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.getcode() == 200:
                    html = response.read().decode("utf-8")
                    text = extract_ssr_content(html)
                    self.cache.set(url, text)
                    return text
        except Exception:
            pass
        return None

    def fetch_trekker(self, numeric_id: str) -> str:
        url = f"https://stelladb.pages.dev/trekker/{numeric_id}"
        res = self.fetch_url(url)
        return res if res else "Error fetching trekker."

    def fetch_disc(self, numeric_id: str) -> str:
        url = f"https://stelladb.pages.dev/disc/{numeric_id}"
        res = self.fetch_url(url)
        return res if res else "Error fetching disc."

    def fetch_infodoc(self, element: str) -> str:
        url = f"https://stelladb.pages.dev/infodoc/{element.lower()}"
        res = self.fetch_url(url)
        return res if res else "Error fetching infodoc."
