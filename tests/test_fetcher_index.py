#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StelladbFetcher.fetch_infodoc_index 单元测试。

验证项：
  1. 首次调用返回非空字符串，且向 https://stelladb.pages.dev/infodoc 发起请求。
  2. 二次调用命中本地缓存，urlopen 调用次数保持为 1。
  3. 网络异常（urlopen 抛出异常）时返回空字符串 ""，不崩溃。
"""

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from fetcher_stelladb import StelladbFetcher


class FakeHTTPResponse:
    def __init__(self, data: bytes, code: int = 200):
        self._data = data
        self._code = code

    def getcode(self) -> int:
        return self._code

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def test_fetch_infodoc_index() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_dir = Path(tmpdir)
        fetcher = StelladbFetcher(cache_dir)

        calls: list[str] = []
        html_content = "<html><body><div>Team Rotation: Aqua Team</div></body></html>".encode("utf-8")

        def fake_urlopen(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            calls.append(url)
            return FakeHTTPResponse(html_content, code=200)

        original_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = fake_urlopen

            # 1. 首次调用：请求目标 URL 且返回非空字符串
            res1 = fetcher.fetch_infodoc_index()
            assert bool(res1), "fetch_infodoc_index() 应该返回非空字符串"
            assert "Team Rotation: Aqua Team" in res1
            assert len(calls) == 1, f"期望 1 次调用，实际为 {len(calls)}"
            assert calls[0] == "https://stelladb.pages.dev/infodoc", f"请求的 URL 不符: {calls[0]}"

            # 2. 二次调用：命中缓存，urlopen 次数保持为 1
            res2 = fetcher.fetch_infodoc_index()
            assert res2 == res1, "缓存返回结果应与首次调用一致"
            assert len(calls) == 1, f"期望命中缓存，实际 urlopen 调用了 {len(calls)} 次"

            # 3. 发生异常时返回空字符串 ""，不崩溃
            with tempfile.TemporaryDirectory() as error_tmpdir:
                error_fetcher = StelladbFetcher(Path(error_tmpdir))

                def error_urlopen(req, timeout=10):
                    raise urllib.error.URLError("Network unreachable")

                urllib.request.urlopen = error_urlopen
                res_err = error_fetcher.fetch_infodoc_index()
                assert res_err == "", f"抓取失败时应返回空字符串，实际返回: {res_err!r}"

        finally:
            urllib.request.urlopen = original_urlopen

    print("ALL TESTS PASSED: test_fetcher_index")
    return 0


if __name__ == "__main__":
    sys.exit(test_fetch_infodoc_index())
