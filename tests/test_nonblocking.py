#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fix D 非阻塞验证：query_what/query_how/count_character_names 已放入线程池，
runner 事件循环不再被同步 IO/CPU 重活阻塞。

测试 A：非阻塞探针
  - Monkeypatch plugin.query_what 为含 time.sleep(0.3) 的同步函数
  - 并发运行 asyncio.sleep(0.1) 探针任务
  - 若探针在 sleep(0.3) 结束前完成（漂移 < 0.15s），则证明同步操作在线程池，不阻塞循环

测试 B：异常行为验证
  - Monkeypatch query_what 抛出 RuntimeError("test error")
  - 验证 handle_what 要么返回 not_found dict，要么干净传播，均无挂起
  - 本插件实现中：asyncio.to_thread 抛出的异常会向上传播（不被 _direct_send 之前捕获），
    因此行为为「向上传播 (propagates)」
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

# 自定位插件根目录，与其他测试写法保持一致
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import plugin as plug
from maibot_sdk.context import PluginContext, PluginPaths


# ===== 复用 test_direct_send.py 的 Mock 脚手架（原样复制，保持风格一致） =====

class MockConfig:
    """模拟宿主全局配置读取（ctx.config.get 的 {success, value} 返回）。"""

    def __init__(self, data=None):
        self.data = data or {}

    async def get(self, key, default=None):
        node = self.data
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return {"success": False, "value": default}
            node = node[part]
        return {"success": True, "value": node}


class MockLLM:
    def __init__(self, answer="mock LLM 攻略成品"):
        self.answer = answer
        self.calls = []

    async def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return {"success": True, "response": self.answer}


class MockSend:
    def __init__(self):
        self.sent = []

    async def text(self, text, stream_id, **kwargs):
        self.sent.append((stream_id, text))
        return True


def _make_plugin():
    """初始化插件实例 + mock ctx（与 test_direct_send.py 完全一致的搭建方式）。"""
    p = plug.create_plugin()
    cache = Path(tempfile.mkdtemp(prefix="stellasora_nonblocking_"))
    ctx = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache, runtime_dir=cache),
    )
    p._set_context(ctx)
    p._plugin_config_instance = plug.StellaSoraConfig()
    ctx.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": []},
            "personality": {"personality": "是人类。", "reply_style": ""},
            "experimental": {"emotion_trait": "neutral"},
        }
    )
    ctx.llm = MockLLM()
    ctx.send = MockSend()
    return p, ctx


# ===== 测试 A：非阻塞探针 =====

async def _test_a() -> tuple[bool, float]:
    """返回 (pass, probe_drift_s)。"""
    p, ctx = _make_plugin()
    await p.on_load()

    # Monkeypatch：plugin 模块命名空间中的 query_what 替换为含 0.3s 阻塞的同步函数
    original_query_what = plug.query_what

    def slow_query_what(*args, **kwargs):
        time.sleep(0.3)          # 同步阻塞 0.3s
        return "mock攻略内容"

    plug.query_what = slow_query_what

    # Monkeypatch count_character_names 使其立即返回（不影响探针计时）
    original_count = plug.count_character_names
    plug.count_character_names = lambda q: 1

    probe_done_at: list[float] = []

    async def probe():
        """0.1s 探针：若事件循环未被阻塞，这个任务将在 ~0.1s 处完成。"""
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        probe_done_at.append(time.monotonic() - t0)

    t_start = time.monotonic()
    # 并发执行：handle_what（内含 0.3s 同步睡眠，已被 to_thread 放入线程池）+ 探针
    await asyncio.gather(
        p.handle_what(query="夏花", group_id="g1", stream_id="stream_a"),
        probe(),
    )
    total = time.monotonic() - t_start

    # 还原
    plug.query_what = original_query_what
    plug.count_character_names = original_count

    probe_elapsed = probe_done_at[0] if probe_done_at else float("inf")
    # 探针实际耗时应接近 0.1s；漂移 < 0.15s（总 < 0.25s）证明 0.3s 阻塞未占用事件循环
    drift = probe_elapsed - 0.1
    passed = drift < 0.15
    return passed, drift, total


# ===== 测试 B：异常行为验证 =====

async def _test_b() -> tuple[bool, str]:
    """返回 (pass, behavior_description)。

    行为说明：handle_what 调用 asyncio.to_thread(query_what, ...)，
    若 query_what 抛出异常，to_thread 会将其重新抛出到 await 处，
    handle_what 本身没有在 to_thread 调用处捕获，所以异常向上传播。
    因此预期行为：propagates_cleanly（调用方会看到 RuntimeError，无挂起）。
    """
    p, ctx = _make_plugin()
    await p.on_load()

    original_query_what = plug.query_what

    def raise_query_what(*args, **kwargs):
        raise RuntimeError("test error")

    plug.query_what = raise_query_what

    behavior = "unknown"
    passed = False
    try:
        result = await asyncio.wait_for(
            p.handle_what(query="夏花", group_id="g1", stream_id="stream_b"),
            timeout=5.0,  # 最多等 5s，若挂起则超时
        )
        # 若返回而未抛（被内部捕获为 not_found）
        if isinstance(result, dict) and "未找到" in result.get("content", ""):
            behavior = "caught_as_not_found"
            passed = True
        else:
            behavior = f"returned_unexpected: {result}"
            passed = False
    except RuntimeError as exc:
        # 干净传播，无挂起
        behavior = f"propagates_cleanly (RuntimeError: {exc})"
        passed = True
    except asyncio.TimeoutError:
        behavior = "HANG_TIMEOUT — 5s 未返回，线程池未解阻塞！"
        passed = False
    finally:
        plug.query_what = original_query_what

    return passed, behavior


# ===== 主入口 =====

async def main():
    print("=== test_nonblocking.py: Fix D asyncio.to_thread 非阻塞验证 ===")
    print()

    # ------- 测试 A -------
    pass_a, drift, total = await _test_a()
    drift_ms = drift * 1000
    total_ms = total * 1000
    status_a = "PASS" if pass_a else "FAIL"
    print(f"A 非阻塞探针: {status_a}")
    print(f"  探针漂移 = {drift_ms:.1f} ms（阈值 < 150ms）")
    print(f"  gather 总耗时 = {total_ms:.0f} ms")
    if not pass_a:
        print("  !! 漂移超阈值：query_what 可能仍在阻塞事件循环！")
    print()

    # ------- 测试 B -------
    pass_b, behavior = await _test_b()
    status_b = "PASS" if pass_b else "FAIL"
    print(f"B 异常行为验证: {status_b}")
    print(f"  行为: {behavior}")
    print()

    all_pass = pass_a and pass_b
    if all_pass:
        print("=== 全部通过 ===")
    else:
        print("=== 存在失败项，请检查上方输出 ===")
        sys.exit(1)

    # 输出机器可读摘要（供 evidence 收集）
    print()
    print(f"SUMMARY probe_drift_ms={drift_ms:.1f} exception_path={behavior.split('(')[0].strip()}")


asyncio.run(main())
