#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""generate_with_pinned_model 固定模型路径验证（stub orchestrator，无需 Runner 环境）。

失败模式说明（修复前）：generate_with_pinned_model 不是 async def，且
orchestrator.generate_response_async(...) 未加 await——调用处拿到的是未 await
的协程对象，随后 result.response 触发
"AttributeError: 'coroutine' object has no attribute 'response'"
（协程从未被 await）；
修复后（async def + await generate_response_async）本测试通过。
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from pathlib import Path

# 自定位插件根目录（对齐 test_dict.py 的写法）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# ---- stub 主程序模块 ----
# generate_with_pinned_model 函数内延迟导入 src.config.model_configs.TaskConfig，
# 独立测试环境没有主程序 src.* 包，注入假模块链避免 ModuleNotFoundError
_src = types.ModuleType("src")
_src_config = types.ModuleType("src.config")
_src_model_configs = types.ModuleType("src.config.model_configs")


@dataclass
class _StubTaskConfig:
    """与 src.config.model_configs.TaskConfig 字段形状一致的假配置。"""

    model_list: list
    max_tokens: int
    temperature: float
    slow_threshold: float
    selection_strategy: str


_src_model_configs.TaskConfig = _StubTaskConfig
_src.config = _src_config
_src_config.model_configs = _src_model_configs
sys.modules["src"] = _src
sys.modules["src.config"] = _src_config
sys.modules["src.config.model_configs"] = _src_model_configs

import model_resolver


class _FakeResult:
    """generate_response_async 的假返回值（字段形状与真实 result 一致）。"""

    response = "模拟生成的固定模型回答"
    reasoning = ""
    model_name = "fake-model"
    prompt_tokens = 1
    completion_tokens = 2
    total_tokens = 3


class _StubOrchestrator:
    """替代 _PinnedTaskLLMOrchestrator 的桩：记录调用并返回假结果。"""

    def __init__(self, task_config, request_type: str = "") -> None:
        self.task_config = task_config
        self.request_type = request_type
        self.calls: list = []

    async def generate_response_async(self, prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return _FakeResult()


def main() -> int:
    # 把桩类塞进缓存全局：_get_pinned_orchestrator_cls() 见非 None 直接返回，
    # 不再触发真实的 src.* 延迟导入
    model_resolver._pinned_orchestrator_cls = _StubOrchestrator

    result = asyncio.run(
        model_resolver.generate_with_pinned_model(
            "test prompt",
            resolved_model_name="fake-model",
            request_type="test",
        )
    )

    ok = True
    ok &= result.get("success") is True
    ok &= result.get("response") == "模拟生成的固定模型回答"
    ok &= result.get("model_name") == "fake-model"
    ok &= result.get("total_tokens") == 3
    print(
        f"固定模型路径 async/await -> success={result.get('success')} "
        f"response={result.get('response')!r} model={result.get('model_name')}: {bool(ok)}"
    )

    if not ok:
        print("[FAIL] generate_with_pinned_model 返回结果不符合预期")
        return 1
    print("=== 固定模型路径验证通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
