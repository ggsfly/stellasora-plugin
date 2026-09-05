import hashlib
import json
import time
from pathlib import Path
from typing import Optional

class CacheManager:
    def __init__(self, cache_dir: Path, ttl_seconds: int = 3600):
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache = {}

    def _get_path(self, key: str) -> Path:
        safe_key = hashlib.md5(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{safe_key}.json"

    def get(self, key: str) -> Optional[str]:
        if key in self._memory_cache:
            entry = self._memory_cache[key]
            if time.time() - entry["timestamp"] < self.ttl_seconds:
                return entry["data"]
        path = self._get_path(key)
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as f:
                    entry = json.load(f)
                if time.time() - entry["timestamp"] < self.ttl_seconds:
                    self._memory_cache[key] = entry
                    return entry["data"]
            except Exception:
                pass
        return None

    def set(self, key: str, data: str) -> None:
        entry = {"timestamp": time.time(), "data": data}
        self._memory_cache[key] = entry
        path = self._get_path(key)
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)
        except Exception:
            pass
