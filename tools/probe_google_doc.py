#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反爬验证脚本：验证 Google Docs TXT 接口与 stelladb /infodoc 的可抓取性。

验证项：
1. Google Docs TXT 接口返回 200 + 真实文本（含 "Preset Codes"）
2. stelladb /infodoc/<element> 返回 200 + 含完整攻略（非空白）
3. stelladb /trekker/<id> 返回 200 + 含角色攻略
4. 连续 10 次 GET 无 429/403（限流检测）

用法：
    python tools/probe_google_doc.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Google Docs 预设码文档 TXT 导出接口
GOOGLE_DOC_URL = (
    "https://docs.google.com/document/d/"
    "1FtGfPUNSJe8Psx4F3ZIcA5m8eBwoiTu8e504-Uw6ZmQ/export?format=txt"
)

# stelladb infodoc 代理页（Ventus 元素团队攻略）
STELLADB_INFODOC_URL = "https://stelladb.pages.dev/infodoc/ventus"

# stelladb trekker 角色页（Wraith = Character.143）
STELLADB_TREKKER_URL = "https://stelladb.pages.dev/trekker/143"

# 连续请求次数（限流检测）
PROBE_COUNT = 10

# 请求间隔（秒）
REQUEST_INTERVAL = 0.5

# 请求超时（秒）
REQUEST_TIMEOUT = 30

USER_AGENT = "stellasora-plugin/1.0.0 (probe)"


def fetch(url: str) -> tuple[int, str, dict[str, str]]:
    """发送 GET 请求，返回 (status_code, body_text, headers)。

    Raises:
        urllib.error.URLError: 网络错误时抛出。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, body, headers


def probe_single(url: str, name: str, content_check: str = "") -> bool:
    """验证单个 URL 的可访问性与内容。

    Args:
        url: 目标 URL。
        name: 验证项名称（用于输出）。
        content_check: 期望在正文中出现的关键词（空字符串则跳过内容检查）。

    Returns:
        bool: 是否通过。
    """
    print(f"\n{'='*60}")
    print(f"[probe] {name}")
    print(f"  URL: {url}")
    try:
        status, body, headers = fetch(url)
    except urllib.error.HTTPError as exc:
        print(f"  [FAIL] HTTP {exc.code}: {exc.reason}")
        return False
    except urllib.error.URLError as exc:
        print(f"  [FAIL] 网络错误: {exc.reason}")
        return False

    print(f"  status: {status}")
    print(f"  body length: {len(body):,} chars")

    # Cache-Control 头
    cache_control = headers.get("cache-control", "未设置")
    print(f"  Cache-Control: {cache_control}")

    # 内容检查
    if content_check:
        if content_check.lower() in body.lower():
            print(f"  [OK] 内容检查通过：找到 '{content_check}'")
        else:
            print(f"  [FAIL] 内容检查未通过：未找到 '{content_check}'")
            print(f"  body 前 200 字: {body[:200]}")
            return False

    # 空内容检查
    if len(body.strip()) < 100:
        print(f"  [FAIL] 内容过短（{len(body.strip())} chars），疑似空壳页")
        return False

    print(f"  [OK] {name}")
    return True


def probe_rate_limit(url: str, name: str, count: int = PROBE_COUNT) -> bool:
    """连续请求检测限流。

    Args:
        url: 目标 URL。
        name: 验证项名称。
        count: 连续请求次数。

    Returns:
        bool: 是否通过（无 429/403 即通过）。
    """
    print(f"\n{'='*60}")
    print(f"[probe] 限流检测：{name}")
    print(f"  URL: {url}")
    print(f"  连续 {count} 次，间隔 {REQUEST_INTERVAL}s")

    failures: list[tuple[int, int, str]] = []
    for i in range(count):
        try:
            status, _body, _headers = fetch(url)
            if status in (429, 403):
                failures.append((i + 1, status, "限流或禁止"))
                print(f"  [{i+1}/{count}] HTTP {status} ⚠")
            else:
                print(f"  [{i+1}/{count}] HTTP {status} ✓")
        except urllib.error.HTTPError as exc:
            failures.append((i + 1, exc.code, str(exc.reason)))
            print(f"  [{i+1}/{count}] HTTP {exc.code} ⚠ {exc.reason}")
        except urllib.error.URLError as exc:
            failures.append((i + 1, 0, str(exc.reason)))
            print(f"  [{i+1}/{count}] 网络错误 ⚠ {exc.reason}")

        if i < count - 1:
            time.sleep(REQUEST_INTERVAL)

    if failures:
        print(f"\n  [FAIL] {len(failures)}/{count} 次请求被限流或拒绝")
        for idx, code, reason in failures:
            print(f"    第 {idx} 次: HTTP {code} ({reason})")
        return False

    print(f"\n  [OK] {count}/{count} 次请求全部通过，无限流")
    return True


def main() -> int:
    """执行全部反爬验证。

    Returns:
        int: 0=全部通过，1=有失败项。
    """
    print("=" * 60)
    print("反爬连通性验证")
    print("=" * 60)

    results: list[tuple[str, bool]] = []

    # 1. Google Docs TXT 接口
    results.append((
        "Google Docs TXT 接口",
        probe_single(
            GOOGLE_DOC_URL,
            "Google Docs 预设码 TXT 导出",
            content_check="Preset Codes",
        ),
    ))

    # 2. stelladb infodoc 代理页
    results.append((
        "stelladb /infodoc/ventus",
        probe_single(
            STELLADB_INFODOC_URL,
            "stelladb infodoc Ventus 代理页",
            content_check="Ventus",
        ),
    ))

    # 3. stelladb trekker 角色页
    results.append((
        "stelladb /trekker/143",
        probe_single(
            STELLADB_TREKKER_URL,
            "stelladb trekker Wraith 角色页",
            content_check="Wraith",
        ),
    ))

    # 4. 限流检测（只测 Google Docs，stelladb 是 CDN 不会限流）
    results.append((
        "Google Docs 限流检测",
        probe_rate_limit(GOOGLE_DOC_URL, "Google Docs TXT 限流"),
    ))

    # 汇总
    print(f"\n{'='*60}")
    print("验证汇总")
    print(f"{'='*60}")
    all_pass = True
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}  {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("[ALL PASS] 反爬验证全部通过，可以开始实施主逻辑。")
        return 0
    else:
        print("[FAILED] 有验证项未通过，请检查上方日志。")
        return 1


if __name__ == "__main__":
    sys.exit(main())