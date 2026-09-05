#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主模型解析器：把配置值解析为任务名或固定模型。

移植自 saberlights_smart-segmentation-plugin 的模型解析链：
    配置值支持三种形态——任务名 / 模型别名(models.name) / 模型标识(models.model_identifier)，
    留空回落默认任务（utils > replyer > planner 优先）。

解析优先级：
    1. 精确命中宿主任务名 → ("task", 任务名)
    2. 命中 models.name / model_identifier → ("model", 别名)
    3. 出现在某任务的 model_list 中 → ("task", 该任务)
    4. 未命中 → ("task", "")（Host 默认模型）

主程序内部模块（TaskConfig / LLMOrchestrator）采用函数内延迟导入：
本模块只应被 Runner 环境中的插件调用；独立 CLI 不经过此路径。
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# 延迟导入缓存（避免模块级 import src.* 影响独立 CLI）
_pinned_orchestrator_cls: Any = None

# 宿主 model_config.toml 读取缓存
_host_model_config_cache: Dict[str, Any] = {}
_host_model_config_cache_mtime: float | None = None


def find_host_model_config_path() -> Path:
    """定位宿主 model_config.toml。

    本文件位于 <主程序根>/plugins/<插件>/tools/ 下，
    parents[3] 即主程序根目录。
    """
    host_root = Path(__file__).resolve().parents[3]
    return host_root / "config" / "model_config.toml"


def load_host_model_config() -> Dict[str, Any]:
    """读取宿主模型配置（带 mtime 缓存），读不到返回空 dict。"""
    global _host_model_config_cache, _host_model_config_cache_mtime

    config_path = find_host_model_config_path()
    if not config_path.is_file():
        logger.warning("未找到宿主模型配置文件: %s", config_path)
        return {}

    try:
        mtime = config_path.stat().st_mtime
    except OSError as exc:
        logger.warning("宿主模型配置文件不可读: %s (%s)", config_path, exc)
        return {}

    if _host_model_config_cache_mtime == mtime and _host_model_config_cache:
        return _host_model_config_cache

    try:
        with config_path.open("rb") as fp:
            config_data = tomllib.load(fp)
    except Exception as exc:
        logger.warning("宿主模型配置解析失败: %s (%s)", config_path, exc)
        return {}

    _host_model_config_cache = config_data
    _host_model_config_cache_mtime = mtime
    return _host_model_config_cache


def _extract_available_task_names(host_model_config: Dict[str, Any]) -> List[str]:
    """从宿主配置的 model_task_config 中提取全部任务名。"""
    raw_task_config = host_model_config.get("model_task_config")
    if not isinstance(raw_task_config, dict):
        return []
    return [str(task_name).strip() for task_name in raw_task_config if str(task_name).strip()]


def _normalize_task_model_list(task_config: Any) -> List[str]:
    """归一化任务配置里的 model_list。"""
    raw_list = task_config.get("model_list") if isinstance(task_config, dict) else None
    if not isinstance(raw_list, list):
        return []
    return [str(item).strip() for item in raw_list if str(item).strip()]


def normalize_model_alias_candidates(configured_name: str, host_model_config: Dict[str, Any]) -> List[str]:
    """把任务名、模型别名和 model_identifier 归一成候选匹配值。"""
    normalized_name = str(configured_name or "").strip()
    if not normalized_name:
        return []

    candidate_names = [normalized_name]
    raw_models = host_model_config.get("models")
    if not isinstance(raw_models, list):
        return candidate_names

    for model_item in raw_models:
        if not isinstance(model_item, dict):
            continue
        model_alias = str(model_item.get("name", "") or "").strip()
        model_identifier = str(model_item.get("model_identifier", "") or "").strip()
        if normalized_name not in {model_alias, model_identifier}:
            continue
        if model_alias and model_alias not in candidate_names:
            candidate_names.append(model_alias)
        if model_identifier and model_identifier not in candidate_names:
            candidate_names.append(model_identifier)
    return candidate_names


def resolve_model_alias_from_host_model_config(configured_name: str, host_model_config: Dict[str, Any]) -> str:
    """把模型别名或 model_identifier 解析为宿主 models.name。"""
    normalized_name = str(configured_name or "").strip()
    if not normalized_name:
        return ""

    raw_models = host_model_config.get("models")
    if not isinstance(raw_models, list):
        return ""

    for model_item in raw_models:
        if not isinstance(model_item, dict):
            continue
        model_alias = str(model_item.get("name", "") or "").strip()
        model_identifier = str(model_item.get("model_identifier", "") or "").strip()
        if normalized_name in {model_alias, model_identifier}:
            return model_alias or normalized_name
    return ""


def resolve_generation_model(configured_name: str) -> Tuple[str, str]:
    """把配置值解析为宿主任务名或固定模型名。

    Returns:
        Tuple[str, str]: (解析类型, 目标名)。
        解析类型为 "task"（任务路由）或 "model"（固定模型执行）；
        目标名为空表示使用 Host 默认模型。
    """
    normalized_name = str(configured_name or "").strip()
    host_model_config = load_host_model_config()
    normalized_tasks = _extract_available_task_names(host_model_config)

    if not normalized_name:
        for preferred_task in ("utils", "replyer", "planner"):
            if preferred_task in normalized_tasks:
                return ("task", preferred_task)
        return ("task", "")

    if normalized_name in normalized_tasks:
        return ("task", normalized_name)

    direct_model_name = resolve_model_alias_from_host_model_config(normalized_name, host_model_config)
    if direct_model_name:
        logger.info("模型 `%s` 已解析为固定模型 `%s`", normalized_name, direct_model_name)
        return ("model", direct_model_name)

    candidate_names = normalize_model_alias_candidates(normalized_name, host_model_config)
    raw_task_config = host_model_config.get("model_task_config")
    if isinstance(raw_task_config, dict):
        matched_tasks: List[Tuple[str, str]] = []
        for task_name, task_config in raw_task_config.items():
            if not isinstance(task_config, dict):
                continue
            task_model_list = _normalize_task_model_list(task_config)
            for candidate_name in candidate_names:
                if candidate_name in task_model_list:
                    matched_tasks.append((str(task_name).strip(), candidate_name))
                    break

        if matched_tasks:
            resolved_task, matched_candidate = matched_tasks[0]
            if len(matched_tasks) > 1:
                logger.warning(
                    "模型/标识 `%s` 命中多个宿主任务 %s，将优先使用 `%s`",
                    normalized_name,
                    [task_name for task_name, _ in matched_tasks],
                    resolved_task,
                )
            logger.info("模型 `%s` 已映射到宿主任务 `%s` (匹配值: `%s`)", normalized_name, resolved_task, matched_candidate)
            return ("task", resolved_task)

    logger.warning("配置的模型/任务 `%s` 未命中宿主可用 task，将回退默认模型", normalized_name)
    return ("task", "")


def _get_pinned_orchestrator_cls() -> Any:
    """延迟导入并缓存固定模型的 Orchestrator 类（仅 Runner 环境可用）。"""
    global _pinned_orchestrator_cls
    if _pinned_orchestrator_cls is None:
        from src.config.model_configs import TaskConfig
        from src.llm_models.utils_model import LLMOrchestrator

        class _PinnedTaskLLMOrchestrator(LLMOrchestrator):
            """固定到单一模型执行的 Orchestrator（移植自 smart-segmentation-plugin）。"""

            def __init__(self, task_config: TaskConfig, request_type: str = "") -> None:
                self._pinned_task_config = task_config
                super().__init__(task_name="planner", request_type=request_type)

            def _get_task_config_or_raise(self) -> TaskConfig:
                return self._pinned_task_config

            def _refresh_task_config(self) -> TaskConfig:
                latest = self._pinned_task_config
                if latest is not self.model_for_task:
                    self.model_for_task = latest
                    self.model_usage = {
                        model: self.model_usage.get(model, (0, 0, 0)) for model in latest.model_list
                    }
                return self.model_for_task

        _pinned_orchestrator_cls = _PinnedTaskLLMOrchestrator
    return _pinned_orchestrator_cls


async def generate_with_pinned_model(
    prompt: str,
    *,
    resolved_model_name: str,
    request_type: str,
) -> Dict[str, Any]:
    """固定到指定模型直接生成（绕过任务路由）。"""
    pinned_cls = _get_pinned_orchestrator_cls()
    from src.config.model_configs import TaskConfig

    orchestrator = pinned_cls(
        TaskConfig(
            model_list=[resolved_model_name],
            max_tokens=4096,
            temperature=0.3,
            slow_threshold=30.0,
            selection_strategy="random",
        ),
        request_type=request_type,
    )
    result = await orchestrator.generate_response_async(
        prompt=prompt,
        temperature=0.3,
        max_tokens=4096,
    )
    return {
        "success": True,
        "response": result.response,
        "reasoning": result.reasoning,
        "model_name": result.model_name,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
    }
