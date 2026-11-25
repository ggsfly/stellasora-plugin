#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stellasora_what 接线测试（1.5.3）：插件层契约，不含库/交付内部实现。

运行方式：
    python tests/test_what_runtime.py

覆盖：
  W0 声明       —— 工具元数据（query+intent、只声明业务参、默认关闭、platforms=qq）、
                   [what] 配置默认值、manifest 版本与两项组件能力
  W1 默认关闭   —— 执行层拦截、Hook 只剔 what（how 保留）、非QQ/名单双剔
  W2 热切换     —— on_load / on_config_update 同步 component.enable/disable（不受
                   how.source 提前 return 影响）、关闭时取消在飞同步
  W3 意图       —— 缺失/非法 mode/未实现 date_window 明确拒绝（不静默忽略）、
                   合法意图原样下行、会话字段只由宿主注入
  W4 五状态     —— 成功/未命中/读取失败（含建库中）/部分/送达未知；提示发送失败
                   不得当成成功；未知不重发；不伪造 reply 指引
  W5 线程       —— 查询与准备、资料同步 worker 都在事件循环之外执行
  W6 /st_update —— 两路独立受理、短探查优先闸门、重复触发不启第二份、旧三路报告
                   不变、失败/取消保留旧库

库与交付层用内存替身（sys.modules 注入，不落盘、不遮蔽任何工作区文件）；
真实 sqlite/网络路径由库/交付各自的测试与 Leader 的实机验收覆盖。
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_all as H  # noqa: E402  复用它已有的 ctx mock 脚手架（不另造一套）
import plugin as plug  # noqa: E402
import what_query  # noqa: E402

failures: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{name} -> {'PASS' if ok else 'FAIL'}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ----------------------------------------------------------------- 替身定义


class FakeGeneration:
    """Generation 契约（库侧 frozen dataclass）的最小替身。"""

    def __init__(
        self,
        *,
        name: str = "kb-20261125T134001.sqlite3",
        last_verified: str = "2026-11-25T21:40:00+08:00",
        built_at: str = "2026-11-25T21:38:00+08:00",
        articles: int = 722,
        age: float = 20.0,
    ) -> None:
        self.name = name
        self.db_path = f"/tmp/{name}"
        self.last_verified = last_verified
        self.built_at = built_at
        self.articles = articles
        self.listed = 745
        self.coverage = {}
        self._age = age

    def as_meta(self) -> Dict[str, Any]:
        return {
            "generation": self.name,
            "built_at": self.built_at,
            "last_verified": self.last_verified,
            "article_count": self.articles,
        }

    def age_hours(self, now: float | None = None) -> float:
        return self._age

    def is_stale(self, hours: int, now: float | None = None) -> bool:
        return self._age > hours


class FakeConn:
    """交付准备只读连接占位（替身交付不读它，但要占住契约位置）。"""

    def execute(self, *a, **k):  # pragma: no cover - 接线测试不真的执行 SQL
        raise AssertionError("接线测试不应直接执行 SQL")


class FakeCandidate:
    """可发候选占位（真实 Candidate 由 what_query 产出）。"""


class FakeQueryResult:
    def __init__(
        self,
        status: str,
        message: str = "",
        *,
        candidates: int = 0,
        subject_name: str | None = None,
    ) -> None:
        self.mode = "field"
        self.status = status
        self.message = message
        self.candidates = [FakeCandidate() for _ in range(candidates)]
        self.also_named: List[Dict[str, Any]] = []
        self.available_levels: List[int] = []
        self.available_groups: List[str] = []
        self.available_fields: List[str] = []
        self.field_notes: List[Dict[str, str]] = []
        self.block_evidence: List[Dict[str, Any]] = []
        self.rejected: List[Dict[str, Any]] = []
        self.media_policy: Dict[str, Any] = {}
        self.stats: Dict[str, Any] = {}
        self.subject_name = subject_name


class FakeDeliveryResult:
    """交付结果替身（字段与真实 DeliveryResult 同名同义）。

    真实交付层对「媒体全挂」的最终回执是 `nothing + success=False +
    images_failed>0 + packs_sent=0`（不是 failed）；`success` 缺省不注入时
    表示「旧替身未声明发送结论」，插件按无发送问题处理（兼容既有用例）。
    """

    def __init__(
        self,
        status: str,
        *,
        user_text: str = "",
        packs_sent: int = 0,
        images_sent: int = 0,
        images_failed: int = 0,
        skipped_reasons: List[str] | None = None,
        error: str = "",
        success: bool | None = None,
    ) -> None:
        self.status = status
        self.user_text = user_text
        self.packs_sent = packs_sent
        self.images_sent = images_sent
        self.images_failed = images_failed
        self.skipped_reasons = list(skipped_reasons or [])
        self.unsent_packs = 0
        self.error = error
        if success is not None:
            self.success = success


class FakeRefreshResult:
    def __init__(
        self,
        status: str = "published",
        *,
        stage: str = "publish",
        generation: str | None = "kb-20261126T010000.sqlite3",
        previous_generation: str | None = "kb-20261125T134001.sqlite3",
        old_generation_ready: bool = True,
        first_build: bool = False,
        last_verified: str | None = "2026-11-26T01:00:00+08:00",
        previous_last_verified: str | None = "2026-11-25T21:40:00+08:00",
        added: int = 0,
        refetched: int = 0,
        reused: int = 722,
        removed: int = 0,
        failed: int = 0,
        verified: int = 0,
        done: int = 0,
        total: int = 0,
        error: str = "",
        error_code: str = "",
        reasons: List[str] | None = None,
    ) -> None:
        self.status = status
        self.stage = stage
        self.generation = generation
        self.previous_generation = previous_generation
        self.old_generation_ready = old_generation_ready
        self.first_build = first_build
        self.last_verified = last_verified
        self.previous_last_verified = previous_last_verified
        self.added = added
        self.refetched = refetched
        self.reused = reused
        self.removed = removed
        self.failed = failed
        self.verified = verified
        self.done = done
        self.total = total
        self.error = error
        self.error_code = error_code
        self.reasons = list(reasons or [])


class FakeRuntime:
    """what_library / what_delivery 的内存替身集合（契约同兄弟模块公开面）。"""

    def __init__(self) -> None:
        self.library_state: Dict[str, Any] = {
            "error": None,
            "generation": FakeGeneration(),
            "refresh": None,
            "refresh_calls": [],
        }
        self.delivery_state: Dict[str, Any] = {
            "plan_calls": [],
            "deliver_calls": [],
            "mode": "sent",
            "result": None,
            "forward_raises": False,
            "prepare_raises": None,
        }
        self.query_calls: List[Dict[str, Any]] = []
        self.query_threads: List[str] = []
        self.run_query_impl = None
        self.library_mod = self._build_library_module()
        self.delivery_mod = self._build_delivery_module()

    # ---- what_library

    def _build_library_module(self) -> types.ModuleType:
        mod = types.ModuleType("what_library")
        state = self.library_state

        class WhatLibraryError(Exception):
            pass

        class WhatLibraryUnavailable(WhatLibraryError):
            def __init__(self, kind: str = "pending", reason: str = "no_generation", detail: str = "") -> None:
                super().__init__(detail or reason)
                self.kind = kind
                self.reason = reason
                self.detail = detail
                self.user_text = (
                    "资料库还没建好，请让有权限的操作员执行 /st_update"
                    if kind == "pending"
                    else "读取失败"
                )

        class WhatLibrary:
            def __init__(self, root: Path) -> None:
                self.root = Path(root)

            @contextlib.contextmanager
            def open_current(self):
                if state["error"] is not None:
                    raise state["error"]
                yield FakeConn(), state["generation"]

            def refresh(self, *, cancel=None, progress=None, full=False, should_stop=None):
                stop = should_stop or (cancel.is_set if cancel is not None else None)
                state["refresh_calls"].append(
                    {"thread": threading.current_thread().name, "stop": stop, "root": str(self.root)}
                )
                impl = state["refresh"]
                if impl is None:
                    return FakeRefreshResult()
                return impl(progress=progress, should_stop=stop)

        mod.WhatLibraryError = WhatLibraryError
        mod.WhatLibraryUnavailable = WhatLibraryUnavailable
        mod.WhatLibrary = WhatLibrary
        return mod

    # ---- what_delivery

    def _build_delivery_module(self) -> types.ModuleType:
        mod = types.ModuleType("what_delivery")
        state = self.delivery_state

        def prepare_delivery(conn, result, intent):
            state["plan_calls"].append({"result": result, "intent": intent, "thread": threading.current_thread().name})
            if state["prepare_raises"] is not None:
                raise state["prepare_raises"]
            return {"plan": True, "status": result.status}

        async def deliver_what(
            *,
            stream_id: str,
            send_forward,
            plan=None,
            conn=None,
            result=None,
            intent=None,
            load_image=None,
            budget_bytes=None,
            host_version=None,
            logger=None,
        ):
            state["deliver_calls"].append(
                {
                    "stream_id": stream_id,
                    "plan": plan,
                    "budget_bytes": budget_bytes,
                    "host_version": host_version,
                    "load_image": load_image,
                    "has_logger": logger is not None,
                }
            )
            mode = state["mode"]
            nodes = [{"nickname": "星塔旅人", "segments": [{"type": "text", "content": "候选说明"}]}]
            if mode in ("sent", "partial"):
                ok = await send_forward(nodes, stream_id)
                if not ok:
                    return FakeDeliveryResult("unknown", error="宿主返回未发送")
                if mode == "partial":
                    return state["result"] or FakeDeliveryResult(
                        "partial", packs_sent=1, images_sent=2, images_failed=1,
                        skipped_reasons=["单图超预算"],
                    )
                return state["result"] or FakeDeliveryResult("sent", packs_sent=1, images_sent=2)
            if mode == "unknown":
                # 真实交付层先尝试 forward，异常或回执 False 才判送达未知（不重发）
                ok = await send_forward(nodes, stream_id)
                if ok and not state["forward_raises"]:
                    return state["result"] or FakeDeliveryResult("unknown", error="forward 回执超时")
                return state["result"] or FakeDeliveryResult("unknown", error="宿主返回未发送")
            return state["result"] or FakeDeliveryResult("nothing", user_text="没查到")

        mod.prepare_delivery = prepare_delivery
        mod.deliver_what = deliver_what
        return mod

    # ---- what_query.run_query

    def install(self) -> None:
        sys.modules["what_library"] = self.library_mod
        sys.modules["what_delivery"] = self.delivery_mod
        self._orig_run_query = what_query.run_query

        def run_query(conn, intent):
            self.query_calls.append(intent)
            self.query_threads.append(threading.current_thread().name)
            if self.run_query_impl is not None:
                return self.run_query_impl(intent)
            return FakeQueryResult("ok", "命中", candidates=1)

        what_query.run_query = run_query

    def uninstall(self) -> None:
        sys.modules.pop("what_library", None)
        sys.modules.pop("what_delivery", None)
        what_query.run_query = self._orig_run_query


class FakeComponent:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.result: Any = {"success": True}

    async def enable_component(self, name, component_type, **kwargs):
        self.calls.append(("enable", name, component_type))
        return self.result

    async def disable_component(self, name, component_type, **kwargs):
        self.calls.append(("disable", name, component_type))
        return self.result


# ----------------------------------------------------------------- 用例


def new_plugin(runtime: FakeRuntime, *, what_enabled: bool = False):
    """插件 + mock ctx（复用 test_all 的脚手架，外加 component 能力替身）。"""
    p, ctx = H.make_plugin()
    ctx.component = FakeComponent()
    p.config.what.enabled = what_enabled
    return p, ctx


async def section_declaration(runtime: FakeRuntime) -> None:
    print("--- W0 声明 ---")
    func = getattr(plug.StellaSoraPlugin, "handle_what", None)
    info = getattr(func, "__maibot_component_info__", None) if func else None
    check("W0a Tool 元数据存在且名字为 stellasora_what", info is not None and info.name == "stellasora_what")
    params = {p.name: p for p in (getattr(info, "parameters", None) or [])} if info else {}
    check("W0b 只声明 query+intent 两个业务参数",
          set(params) == {"query", "intent"}, f"params={sorted(params)}")
    check("W0c query 必填、intent 必填且为 OBJECT",
          params.get("query") is not None and params["query"].required is True
          and params["intent"].param_type.value == "object" and params["intent"].required is True)
    schema = params["intent"].to_parameter_schema() if "intent" in params else {}
    props = schema.get("properties", {})
    check("W0d intent 覆盖 mode/subject/shape/fields/levels/groups/conditions",
          {"mode", "subject", "shape", "fields", "levels", "groups", "conditions"} <= set(props),
          f"props={sorted(props)}")
    check("W0e subject 为 name+kind 且 kind 枚举，mode 枚举四模式",
          props.get("subject", {}).get("required") == ["name"]
          and "kind" in props["subject"].get("properties", {})
          and set(props["mode"].get("enum", [])) == {"field", "article", "media", "catalog"})
    check("W0f 不暴露会话字段（stream_id/platform/group_id/user_id）",
          not ({"stream_id", "platform", "group_id", "user_id"} & set(props)))
    md = getattr(info, "metadata", None) or {}
    plats = getattr(info, "platforms", None)
    check("W0g 默认关闭 + platforms=qq + visibility=visible",
          md.get("enabled") is False and plats in ("qq", ["qq"])
          and str(md.get("visibility", "")).lower() == "visible",
          f"metadata={md} platforms={plats}")
    check("W0h 工具预算随 metadata 上报", md.get("timeout_ms") == plug._TOOL_RPC_TIMEOUT_MS)

    cfg = plug.StellaSoraConfig()
    check("W0i [what] 默认 enabled=False、stale_warn_hours=168",
          cfg.what.enabled is False and cfg.what.stale_warn_hours == 168)
    check("W0j config_version 升到 1.5.0", cfg.plugin.config_version == "1.5.0")

    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    caps = set(manifest.get("capabilities") or [])
    check("W0k manifest 版本 1.5.3 且声明组件热切换两项能力",
          manifest.get("version") == "1.5.3"
          and {"component.enable", "component.disable"} <= caps,
          f"version={manifest.get('version')} caps={sorted(caps)}")
    check("W0l 兼容范围未变（host 1.3.15–1.4.99 / sdk 2.16.0–2.23.99）",
          manifest["host_application"] == {"min_version": "1.3.15", "max_version": "1.4.99"}
          and manifest["sdk"] == {"min_version": "2.16.0", "max_version": "2.23.99"})


async def section_default_off(runtime: FakeRuntime) -> None:
    print("--- W1 默认关闭 / 双层过滤 ---")
    p, ctx = new_plugin(runtime, what_enabled=False)
    p.config.access_control.mode = "off"
    res = await p.handle_what(query="优叶生日", intent={"mode": "field",
                                                        "subject": {"name": "优叶", "kind": "traveler"},
                                                        "fields": ["birthday"]},
                              group_id="g1", stream_id="s1", platform="qq")
    check("W1a 关闭时执行层拦截（success=False、零库调用、零发送）",
          res.get("success") is False and not runtime.query_calls
          and not ctx.send.forwarded and not ctx.send.sent,
          f"res={res}")

    def defs(*names: str) -> List[Dict[str, Any]]:
        return [H._tool_def(n) for n in names]

    p._session_identity_cache["s_off"] = ("g1", "u1")
    p._session_platform_cache["s_off"] = "qq"
    r = await p._filter_planner_tools(**H._hook_kwargs(defs("stellasora_how", "stellasora_what"), "s_off"))
    kept = [d["function"]["name"] for d in r.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("W1b 授权 QQ 且 what 关闭：只剔 what，how 保留", kept == ["stellasora_how"], f"kept={kept}")

    p._session_identity_cache["s_tg"] = ("g1", "u1")
    p._session_platform_cache["s_tg"] = "telegram"
    r2 = await p._filter_planner_tools(**H._hook_kwargs(defs("stellasora_how", "stellasora_what"), "s_tg"))
    kept2 = [d["function"]["name"] for d in r2.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("W1c 非QQ平台：两工具一起剔除", kept2 == [], f"kept={kept2}")

    p.config.what.enabled = True
    p._session_identity_cache["s_ok"] = ("g1", "u1")
    p._session_platform_cache["s_ok"] = "qq"
    r3 = await p._filter_planner_tools(**H._hook_kwargs(defs("stellasora_how", "stellasora_what"), "s_ok"))
    check("W1d 打开后授权 QQ 不再剔除", r3 == {"action": "continue"})
    p.config.access_control.mode = "blacklist"
    p.config.access_control.blacklist = ["g1"]
    r4 = await p._filter_planner_tools(**H._hook_kwargs(defs("stellasora_how", "stellasora_what"), "s_ok"))
    kept4 = [d["function"]["name"] for d in r4.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("W1e 黑名单命中：两工具一起剔除（与执行层同口径）", kept4 == [], f"kept={kept4}")
    p.config.access_control.mode = "off"

    res2 = await p.handle_what(query="优叶生日", intent={"mode": "field",
                                                         "subject": {"name": "优叶"}},
                               group_id="g1", platform="telegram", stream_id="s_tg")
    check("W1f 非QQ 平台执行层保守拒绝（不发送、不查库）",
          res2.get("content") == "该平台暂不支持星塔旅人资料查询。"
          and not runtime.query_calls and not ctx.send.sent and not ctx.send.forwarded)
    await p.on_unload()


async def section_hot_switch(runtime: FakeRuntime) -> None:
    print("--- W2 热切换 ---")
    p, ctx = new_plugin(runtime, what_enabled=False)
    await p.on_load()
    H._stop_bg(p)
    full = f"{ctx.plugin_id}.stellasora_what"
    check("W2a on_load 按配置关闭组件（完整名 + TOOL）",
          ctx.component.calls and ctx.component.calls[-1] == ("disable", full, "TOOL"),
          f"calls={ctx.component.calls}")

    p.config.what.enabled = True
    calls_before = len(ctx.component.calls)
    await p.on_config_update(scope="self", config_data={}, version="1.5.0")
    check("W2b how.source 未变也会同步（不被提前 return 吞掉）",
          len(ctx.component.calls) == calls_before + 1
          and ctx.component.calls[-1] == ("enable", full, "TOOL"),
          f"calls={ctx.component.calls[calls_before:]}")

    p.config.what.enabled = False
    await p.on_config_update(scope="self", config_data={}, version="1.5.0")
    check("W2c 关闭再次同步为 disable", ctx.component.calls[-1] == ("disable", full, "TOOL"))

    ctx.component.result = {"success": False, "error": "组件名不唯一"}
    p.config.what.enabled = True
    await p.on_config_update(scope="self", config_data={}, version="1.5.0")
    check("W2d 同步失败只告警不抛出、不缓存错误状态",
          p._what_component_state is None, f"state={p._what_component_state}")
    await p.on_unload()


async def section_intent(runtime: FakeRuntime) -> None:
    print("--- W3 结构化意图 ---")
    p, ctx = new_plugin(runtime, what_enabled=True)
    await p.on_load()
    H._stop_bg(p)
    base_kwargs = {"group_id": "g1", "stream_id": "stream_w1", "platform": "qq"}

    res = await p.handle_what(query="优叶生日", intent=None, **base_kwargs)
    check("W3a 缺 intent：success=False + 引导文案，不查库不发送",
          res.get("success") is False and "intent" in str(res.get("error", ""))
          and not runtime.query_calls and not ctx.send.sent)

    res = await p.handle_what(query="优叶生日", intent={"mode": "guess", "subject": {"name": "优叶"}}, **base_kwargs)
    check("W3b 非法 mode 拒绝", res.get("success") is False and "mode" in str(res.get("error", ""))
          and not runtime.query_calls)

    res = await p.handle_what(
        query="最近公告", intent={"mode": "article", "date_window": {"start": "2026-11-01"}}, **base_kwargs
    )
    check("W3c 未实现 date_window 明确拒绝（不静默忽略）",
          res.get("success") is False and "date_window" in str(res.get("error", ""))
          and not runtime.query_calls)

    res = await p.handle_what(query="", intent={"mode": "field"}, **base_kwargs)
    check("W3d 空 query：success=False + 可引导示例，不发送",
          res.get("success") is False and "query" in str(res.get("error", ""))
          and not ctx.send.sent)

    res = await p.handle_what(
        query="优叶90级攻击",
        intent={"mode": "field", "subject": {"name": "优叶", "kind": "traveler"},
                "fields": ["stat.attack"], "levels": [90], "shape": "single"},
        **base_kwargs,
    )
    sent_intent = runtime.query_calls[-1] if runtime.query_calls else {}
    check("W3e 合法意图原样下行（字段/等级/shape 不被改写）",
          sent_intent.get("mode") == "field"
          and sent_intent.get("fields") == ["stat.attack"]
          and sent_intent.get("levels") == [90]
          and sent_intent.get("shape") == "single",
          f"intent={sent_intent}")

    res = await p.handle_what(
        query="水系伤害", intent={"mode": "article", "literal": "水系伤害"}, **base_kwargs
    )
    sent_intent = runtime.query_calls[-1] if runtime.query_calls else {}
    check("W3f article 自带检索文本时不覆盖为 query 原文",
          sent_intent.get("literal") == "水系伤害" and "query" not in sent_intent,
          f"intent={sent_intent}")

    res = await p.handle_what(query="水系伤害", intent={"mode": "article"}, **base_kwargs)
    sent_intent = runtime.query_calls[-1] if runtime.query_calls else {}
    check("W3g article 无主体无检索文本时用 query 参数原文检索",
          sent_intent.get("query") == "水系伤害", f"intent={sent_intent}")

    res = await p.handle_what(
        query="优叶生日", intent={"mode": "field", "subject": {"name": "优叶"}},
        group_id="g1", user_id="u1", platform="qq", stream_id="stream_w1",
    )
    forwarded = ctx.send.forwarded[-1] if ctx.send.forwarded else None
    check("W3h 发送目标用宿主注入的 stream_id（不接受模型自报）",
          forwarded is not None and forwarded[0] == "stream_w1")
    await p.on_unload()


async def section_states(runtime: FakeRuntime) -> None:
    print("--- W4 五状态与提示回执 ---")
    p, ctx = new_plugin(runtime, what_enabled=True)
    await p.on_load()
    H._stop_bg(p)
    kwargs = {"group_id": "g1", "stream_id": "stream_w4", "platform": "qq"}
    intent = {"mode": "field", "subject": {"name": "优叶", "kind": "traveler"}, "fields": ["birthday"]}

    # 成功交付
    runtime.delivery_state["mode"] = "sent"
    res = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    check("W4a 成功交付：success=True + stop=True + 一次 forward",
          res.get("success") is True and res.get("stop_after_execution") is True
          and len(ctx.send.forwarded) == 1 and not ctx.send.sent)
    check("W4b 成功回执给出资料截至且不让 planner 再 reply",
          "资料截至" in str(res.get("content", "")) and "reply" in str(res.get("content", ""))
          and "stop" not in str(res.get("content", "")).lower())

    # 未命中
    runtime.delivery_state["mode"] = "nothing"
    runtime.query_calls.clear()
    runtime.run_query_impl = lambda intent: FakeQueryResult("not_found", "资料库未收录「不存在的角色」")
    n_sent, n_fwd = len(ctx.send.sent), len(ctx.send.forwarded)
    res = await p.handle_what(query="不存在的角色生日", intent=intent, **kwargs)
    check("W4c 未命中：直发「没查到」+ success=True + stop=True",
          len(ctx.send.sent) == n_sent + 1 and ctx.send.sent[-1][1] == plug._WORD_MISS
          and len(ctx.send.forwarded) == n_fwd
          and res.get("success") is True and res.get("stop_after_execution") is True,
          f"sent={ctx.send.sent[-1:]}")
    runtime.run_query_impl = None

    # 提示发送失败：不得停轮、不得当成功
    runtime.delivery_state["mode"] = "nothing"
    runtime.run_query_impl = lambda intent: FakeQueryResult("not_found", "未收录")
    orig_text = ctx.send.text
    ctx.send.text = lambda text, stream_id, **kw: _false_text()
    res = await p.handle_what(query="不存在", intent=intent, **kwargs)
    ctx.send.text = orig_text
    check("W4d 提示没发出去：success=False 且不停轮（不伪造成功）",
          res.get("success") is False and res.get("stop_after_execution") is None
          and res.get("content") == plug._WORD_MISS)
    runtime.run_query_impl = None

    # 建库中 / 首次未建库 / 库损坏
    unavailable_cls = runtime.library_mod.WhatLibraryUnavailable
    runtime.library_state["error"] = unavailable_cls("pending", "staging_only", "staging 有库但无指针")
    n_sent = len(ctx.send.sent)
    res = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    check("W4e 残留 staging + 无在飞：提示「请先执行 /st_update」，reason 只留工具回执",
          len(ctx.send.sent) == n_sent + 1 and ctx.send.sent[-1][1] == plug._WORD_NO_LIBRARY
          and res.get("success") is False and "staging_only" in str(res.get("error", ""))
          and plug._WORD_BUILDING not in str(res.get("content", "")),
          f"sent={ctx.send.sent[-1:]} err={str(res.get('error'))[:40]}")

    runtime.library_state["error"] = unavailable_cls("pending", "no_generation", "无已发布代")
    n_sent = len(ctx.send.sent)
    res = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    check("W4f 无库：提示「请先执行 /st_update」而不是「没查到」",
          len(ctx.send.sent) == n_sent + 1 and ctx.send.sent[-1][1] == plug._WORD_NO_LIBRARY
          and res.get("success") is False)

    runtime.library_state["error"] = unavailable_cls("corrupt", "pointer_invalid", "指针损坏")
    n_sent = len(ctx.send.sent)
    res = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    check("W4g 库损坏：提示「读取失败」，不与未建库混同",
          len(ctx.send.sent) == n_sent + 1 and ctx.send.sent[-1][1] == plug._WORD_READ_FAIL
          and res.get("success") is False)
    runtime.library_state["error"] = None

    # 真实在飞（插件自己的 worker 在线程里跑）但尚无 staging：pending 显示「建库中」
    build_release = threading.Event()
    build_started = threading.Event()

    def holding_refresh(progress=None, should_stop=None):
        build_started.set()
        build_release.wait(8)
        return FakeRefreshResult(status="published", added=1)

    runtime.library_state["refresh"] = holding_refresh
    build_gate = asyncio.Event()
    build_gate.set()
    p._what_update_stop = threading.Event()
    p._what_update_task = asyncio.create_task(p._run_what_update("stream_w4_build", build_gate))
    await asyncio.to_thread(build_started.wait, 5)
    runtime.library_state["error"] = unavailable_cls("pending", "no_generation", "首次建库中，尚无 staging")
    n_sent = len(ctx.send.sent)
    res_build = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    check("W4p 真实在飞（尚无 staging）：提示「建库中」+ success=False + 不查库",
          len(ctx.send.sent) == n_sent + 1 and ctx.send.sent[-1][1] == plug._WORD_BUILDING
          and res_build.get("success") is False and p._what_update_active() is True
          and "no_generation" in str(res_build.get("error", "")),
          f"sent={ctx.send.sent[-1:]} active={p._what_update_active()}")

    # 取消信号已置位（任务尚未结束）不算活跃；任务完成后的残留 staging 也不卡在「建库中」
    p._what_update_stop.set()
    runtime.library_state["error"] = unavailable_cls("pending", "staging_only", "staging 有库但无指针")
    n_sent = len(ctx.send.sent)
    res_stop = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    build_release.set()
    await asyncio.wait_for(p._what_update_task, 20)
    runtime.library_state["error"] = unavailable_cls("pending", "staging_only", "staging 有库但无指针")
    n_sent2 = len(ctx.send.sent)
    res_done = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    runtime.library_state["error"] = None
    runtime.library_state["refresh"] = None
    # 本用例用真实 worker 跑过一次同步：清掉调用记录，W6 仍以「空调用表」为起点
    runtime.library_state["refresh_calls"].clear()
    check("W4q 取消已置位/任务已完成：都不算「建库中」，仍提示「请先执行 /st_update」",
          res_stop.get("success") is False and res_stop.get("content") == plug._WORD_NO_LIBRARY
          and res_done.get("success") is False and res_done.get("content") == plug._WORD_NO_LIBRARY
          and len(ctx.send.sent) >= n_sent2 + 1 and p._what_update_active() is False,
          f"stop={res_stop.get('content')} done={res_done.get('content')} active={p._what_update_active()}")

    # 部分交付：不停轮
    runtime.delivery_state["mode"] = "partial"
    n_sent = len(ctx.send.sent)
    res = await p.handle_what(query="优叶立绘", intent={**intent, "mode": "media"}, **kwargs)
    check("W4h 部分交付：success=True + 不停轮 + 「部分发出」",
          res.get("success") is True and res.get("stop_after_execution") is None
          and len(ctx.send.sent) == n_sent + 1 and ctx.send.sent[-1][1] == plug._WORD_PARTIAL,
          f"sent={ctx.send.sent[-1:]}")

    # 送达未知：不重发
    runtime.delivery_state["mode"] = "unknown"
    n_sent, n_fwd = len(ctx.send.sent), len(ctx.send.forwarded)
    res = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    check("W4i 送达未知：success=False + 不设停轮 + 「送达不确定」+ forward 只发一次",
          res.get("success") is False and res.get("stop_after_execution") is None
          and len(ctx.send.sent) == n_sent + 1 and ctx.send.sent[-1][1] == plug._WORD_UNKNOWN
          and len(ctx.send.forwarded) == n_fwd + 1,
          f"fwd_delta={len(ctx.send.forwarded) - n_fwd}")

    # 交付层抛异常：按送达未知处理，不重发
    runtime.delivery_state["mode"] = "unknown"
    runtime.delivery_state["forward_raises"] = True
    n_sent, n_fwd = len(ctx.send.sent), len(ctx.send.forwarded)
    res = await p.handle_what(query="优叶生日", intent=intent, **kwargs)
    check("W4j 交付层异常：success=False、「送达不确定」、不重发",
          res.get("success") is False and len(ctx.send.sent) == n_sent + 1
          and ctx.send.sent[-1][1] == plug._WORD_UNKNOWN
          and len(ctx.send.forwarded) == n_fwd + 1)
    runtime.delivery_state["forward_raises"] = False

    # 意图不成立（库侧返回 invalid_intent）：只回 planner，不打扰群
    runtime.delivery_state["mode"] = "sent"
    runtime.query_calls.clear()
    runtime.run_query_impl = lambda intent: FakeQueryResult("invalid_intent", "intent.subject.name 为空")
    n_sent, n_fwd = len(ctx.send.sent), len(ctx.send.forwarded)
    res = await p.handle_what(query="看下资料", intent={"mode": "field"}, **kwargs)
    check("W4k 意图不成立：只回 planner（success=False，不发群、不 forward）",
          res.get("success") is False and "intent 不成立" in str(res.get("error", ""))
          and len(ctx.send.sent) == n_sent and len(ctx.send.forwarded) == n_fwd)
    runtime.run_query_impl = None

    # 未判/装饰媒体不外发：交付层负责筛选，插件不自己拼节点
    runtime.delivery_state["mode"] = "sent"
    n_fwd = len(ctx.send.forwarded)
    res = await p.handle_what(query="优叶", intent={**intent, "mode": "media"}, **kwargs)
    plan_intent = runtime.delivery_state["plan_calls"][-1]["intent"]
    check("W4l 交付只拿计划：插件不自造节点、debug 字段不外发",
          len(ctx.send.forwarded) == n_fwd + 1
          and plan_intent.get("mode") == "media"
          and "field_notes" not in str(ctx.send.forwarded[-1][1]),
          f"plan_intent={plan_intent}")

    # 提示回执只认「明确成功」：None / 失败字典不得算已发出（否则「没查到」未确认也停轮）
    runtime.delivery_state["mode"] = "nothing"
    runtime.delivery_state["result"] = None
    runtime.run_query_impl = lambda intent: FakeQueryResult("not_found", "未收录")

    def _text_returns(outcome):
        async def _impl(text, stream_id, **kw):
            ctx.send.sent.append((stream_id, text))
            return outcome

        return _impl

    outcomes: Dict[str, Any] = {}
    for label, outcome in (("None", None), ("失败字典", {"success": False}), ("SDK True", {"sent": True})):
        n0 = len(ctx.send.sent)
        ctx.send.text = _text_returns(outcome)
        r = await p.handle_what(query="不存在", intent=intent, **kwargs)
        outcomes[label] = (r, len(ctx.send.sent) - n0)
    ctx.send.text = orig_text
    runtime.run_query_impl = None
    r_none, k_none = outcomes["None"]
    r_fail, k_fail = outcomes["失败字典"]
    r_ok, k_ok = outcomes["SDK True"]
    check("W4m 提示回执只认明确成功：None/失败字典不停轮不假成功，SDK {sent: True} 才算已发出",
          r_none.get("success") is False and r_none.get("stop_after_execution") is None
          and r_fail.get("success") is False and r_fail.get("stop_after_execution") is None
          and k_none == 1 and k_fail == 1
          and r_ok.get("success") is True and r_ok.get("stop_after_execution") is True
          and k_ok == 1,
          f"none={r_none.get('success')}/{r_none.get('stop_after_execution')} "
          f"fail={r_fail.get('success')}/{r_fail.get('stop_after_execution')} "
          f"ok={r_ok.get('success')}/{r_ok.get('stop_after_execution')}")

    # 明确发送失败（有图片一张都没发出去）：WHAT 直发「发送失败」，真实失败且不停轮；
    # 计划无效（images_failed=0）不是「发不出去」，仍只回 planner 不打扰群
    runtime.delivery_state["mode"] = "failed"
    runtime.delivery_state["result"] = FakeDeliveryResult(
        "failed", images_failed=2, error="立绘取图全部失败",
    )
    n_sent, n_fwd = len(ctx.send.sent), len(ctx.send.forwarded)
    res = await p.handle_what(query="优叶立绘", intent={**intent, "mode": "media"}, **kwargs)
    runtime.delivery_state["result"] = FakeDeliveryResult("failed", error="计划无效（empty）")
    n_sent2 = len(ctx.send.sent)
    res_plan = await p.handle_what(query="优叶立绘", intent={**intent, "mode": "media"}, **kwargs)
    runtime.delivery_state["result"] = None
    check("W4n 图片全失败：WHAT 直发「发送失败」+ success=False + 不停轮；计划无效仍只回 planner",
          res.get("success") is False and res.get("stop_after_execution") is None
          and len(ctx.send.sent) == n_sent + 1
          and ctx.send.sent[-1][1] == plug._WORD_SEND_FAIL
          and len(ctx.send.forwarded) == n_fwd
          and res_plan.get("success") is False and len(ctx.send.sent) == n_sent2
          and "发送失败" not in str(res_plan.get("content", "")),
          f"failed_sent={ctx.send.sent[n_sent:]} plan={res_plan}")

    # 跨层值精确复验：交付层对「媒体全挂」的最终回执是 nothing + success=False +
    # images_failed>0 + packs_sent=0（用户看不到任何图片）。插件不能走「没查到」长句
    # 并停轮，必须发「发送失败」、success=False、不停轮。
    runtime.delivery_state["mode"] = "failed"
    runtime.delivery_state["result"] = FakeDeliveryResult(
        "nothing", success=False, packs_sent=0, images_sent=0, images_failed=3,
        user_text="资料读取失败", error="图片全部发送失败",
    )
    n_sent, n_fwd = len(ctx.send.sent), len(ctx.send.forwarded)
    res = await p.handle_what(query="优叶立绘", intent={**intent, "mode": "media"}, **kwargs)
    runtime.delivery_state["result"] = None
    check("W4o 媒体全挂（nothing+success=False+images_failed>0）：发「发送失败」，success=False 不停轮，不发长句",
          res.get("success") is False and res.get("stop_after_execution") is None
          and len(ctx.send.sent) == n_sent + 1
          and ctx.send.sent[-1][1] == plug._WORD_SEND_FAIL
          and "没查到" not in ctx.send.sent[-1][1]
          and len(ctx.send.forwarded) == n_fwd,
          f"sent={ctx.send.sent[n_sent:]} res={res}")

    await p.on_unload()


async def _false_text():
    return False


async def section_offload(runtime: FakeRuntime) -> None:
    print("--- W5 线程纪律 ---")
    p, ctx = new_plugin(runtime, what_enabled=True)
    await p.on_load()
    H._stop_bg(p)
    runtime.query_threads.clear()
    kwargs = {"group_id": "g1", "stream_id": "stream_w5", "platform": "qq"}
    runtime.delivery_state["mode"] = "sent"
    await p.handle_what(query="优叶生日", intent={"mode": "field", "subject": {"name": "优叶"}}, **kwargs)
    check("W5a 开库+查询+交付准备在事件循环之外执行",
          bool(runtime.query_threads) and runtime.query_threads[-1] != "MainThread",
          f"thread={runtime.query_threads[-1:]}")
    check("W5b 交付准备与查询同一次 to_thread 内（同线程名）",
          len(runtime.delivery_state["plan_calls"]) >= 1
          and runtime.delivery_state["plan_calls"][-1]["thread"] == runtime.query_threads[-1],
          f"plan_thread={runtime.delivery_state['plan_calls'][-1]['thread']}")
    await p.on_unload()


async def section_update(runtime: FakeRuntime) -> None:
    print("--- W6 /st_update 编排 ---")
    orig_build = plug.gamekee_build_index
    orig_refresh = plug.refresh_board_for_update
    orig_probe_updates = plug.gamekee_probe_updates

    def default_probe_updates(proxy_url=None, timeout=20, known_updates=None):
        """默认探查替身：六页「无变化」，绝不触网。

        本节的真实网络面只有两处：短探查 `gamekee_probe_updates` 与完整重建
        `gamekee_build_index`（后者已在下方整体替换）。未在实例上覆写
        `_run_gamekee_probe` 的分支（W6a/W6i/W6j）走的就是插件默认实现，
        不注入替身就会真的去 GET GameKee 详情页。要测「慢探查」的分支照常
        在实例上覆写 `p._run_gamekee_probe`，互不影响。
        """
        return {"pages": {}, "changed": False}

    try:
        plug.gamekee_probe_updates = default_probe_updates
        # ---- what 关闭：行为与 1.5.2 完全一致（不建第四路、连点仍拒）
        p, ctx = new_plugin(runtime, what_enabled=False)
        await p.on_load()
        H._stop_bg(p)
        gate = threading.Event()

        def slow_board(leaderboard_dir, proxy_url=None):
            gate.wait(5)
            return {"bb": {"status": "ok", "detail": "bb13 Top100"},
                    "fe": {"status": "ok", "detail": "fe8 Top100"}}

        plug.gamekee_build_index = lambda proxy_url=None: {"characters": []}
        plug.refresh_board_for_update = slow_board
        res1 = await p.handle_update(stream_id="u_off", group_id="g1", platform="qq")
        task = p._update_task
        res2 = await p.handle_update(stream_id="u_off2", group_id="g1", platform="qq")
        check("W6a what 关闭：不建第四路且连点仍被拒（原语义不变）",
              res1[0] is True and p._what_update_task is None
              and res2[0] is False and res2[2] == 1 and "已有更新任务进行中" in res2[1]
              and p._update_task is task, f"res2={res2}")
        gate.set()
        await asyncio.wait_for(p._update_task, 30)
        await p.on_unload()

        # ---- what 开启：两路独立，短探查优先
        p, ctx = new_plugin(runtime, what_enabled=True)
        await p.on_load()
        H._stop_bg(p)
        probe_gate = threading.Event()
        release_what = threading.Event()
        order: List[str] = []

        async def slow_probe(trigger="manual_update"):
            order.append("probe_start")
            await asyncio.wait_for(asyncio.to_thread(probe_gate.wait, 4), 6)
            order.append("probe_end")
            return {"characters": [{"name": "艾蕾"}]}

        def fake_refresh(progress=None, should_stop=None):
            order.append("refresh_start")
            if progress:
                progress({"stage": "detail", "done": 3, "total": 722})
            release_what.wait(5)
            return FakeRefreshResult(status="published", added=12, refetched=3, removed=1)

        p._run_gamekee_probe = slow_probe
        runtime.library_state["refresh"] = fake_refresh
        plug.refresh_board_for_update = lambda leaderboard_dir, proxy_url=None: {
            "bb": {"status": "ok", "detail": "bb13 Top100"},
            "fe": {"status": "not_found", "detail": "fe8 当期未开榜"},
        }
        res = await p.handle_update(stream_id="u_on", group_id="g1", platform="qq")
        check("W6b what 开启：受理并同时登记两路任务",
              res[0] is True and res[2] == 2 and p._update_task is not None
              and p._what_update_task is not None,
              f"res={res}")
        for _ in range(200):
            if order:
                break
            await asyncio.sleep(0.005)
        check("W6c 长批等短探查：探查未放行前不发任何资料请求",
          order == ["probe_start"] and not runtime.library_state["refresh_calls"],
          f"order={order}")

        # 重复触发：原三路在飞、资料同步也在飞 → 整单拒绝
        res_dup = await p.handle_update(stream_id="u_on2", group_id="g1", platform="qq")
        check("W6d 两路都在飞时重复触发被拒（不启第二份）",
              res_dup[0] is False and res_dup[2] == 1
              and runtime.library_state["refresh_calls"] == [] , f"res_dup={res_dup}")

        probe_gate.set()
        for _ in range(400):
            if runtime.library_state["refresh_calls"]:
                break
            await asyncio.sleep(0.01)
        check("W6e 短探查结束后才启动资料同步（预算优先给短探查）",
              order[:3] == ["probe_start", "probe_end", "refresh_start"], f"order={order[:3]}")

        await asyncio.wait_for(p._update_task, 30)
        msgs = [t[1] for t in ctx.send.sent]
        routes_msg = [m for m in msgs if "数据更新完成" in m]
        check("W6f 原三路仍先独立报告（不等资料同步）",
              len(routes_msg) == 1 and "攻略源：" in routes_msg[0]
              and "联合讨伐：强刷成功" in routes_msg[0] and "终焉绝响：fe8 当期未开榜" in routes_msg[0],
              routes_msg[0][:100] if routes_msg else "no routes msg")
        release_what.set()
        await asyncio.wait_for(p._what_update_task, 30)
        what_msg = [t[1] for t in ctx.send.sent if "资料库同步完成" in t[1]]
        check("W6g 资料同步单独终报（增量/发布代/资料截至三要素）",
              len(what_msg) == 1 and "新增 12" in what_msg[0] and "重抓 3" in what_msg[0]
              and "移除 1" in what_msg[0] and "资料截至" in what_msg[0],
              what_msg[0] if what_msg else "no what msg")
        await p.on_unload()

        # ---- 原三路在飞 + 资料同步空闲：只启动资料同步（不重开两榜）
        # ---- 三路空闲 + 资料同步在飞：照常受理三路，并回报资料同步进度（不启第二份）
        p, ctx = new_plugin(runtime, what_enabled=False)
        await p.on_load()
        H._stop_bg(p)
        board_gate = threading.Event()
        what_hold = threading.Event()

        def slow_refresh(progress=None, should_stop=None):
            if progress:
                progress({"stage": "detail", "done": 3, "total": 722})
            what_hold.wait(8)
            return FakeRefreshResult(status="published", added=4)

        runtime.library_state["refresh"] = slow_refresh

        def blocking_board(leaderboard_dir, proxy_url=None):
            board_gate.wait(6)
            return {"bb": {"status": "ok", "detail": "bb13"}, "fe": {"status": "ok", "detail": "fe8"}}

        async def instant_probe(trigger="manual_update"):
            return {"characters": []}

        p._run_gamekee_probe = instant_probe
        plug.refresh_board_for_update = blocking_board
        first = await p.handle_update(stream_id="u_r1", group_id="g1", platform="qq")
        # 这一轮只有三路在飞，再把 what 打开
        p.config.what.enabled = True
        await p.on_config_update(scope="self", config_data={}, version="1.5.0")
        routes_task = p._update_task
        second = await p.handle_update(stream_id="u_r2", group_id="g1", platform="qq")
        check("W6h 三路在飞 + 资料同步空闲：受理并只启动资料同步（三路句柄不变）",
              first[0] is True and second[0] is True and second[2] == 2
              and p._update_task is routes_task and p._what_update_task is not None
              and "资料库同步" in ctx.send.sent[-1][1], f"second={second}")

        board_gate.set()
        await asyncio.wait_for(p._update_task, 30)
        what_task = p._what_update_task
        third = await p.handle_update(stream_id="u_r3", group_id="g1", platform="qq")
        check("W6m 三路空闲 + 资料同步在飞：照常受理并回报进度（不启第二份）",
              third[0] is True and third[2] == 2 and p._what_update_task is what_task
              and "资料库同步进行中（详情 3/722）" in ctx.send.sent[-1][1],
              f"third={third} msg={ctx.send.sent[-1][1]}")
        what_hold.set()
        await asyncio.wait_for(p._what_update_task, 30)
        await asyncio.wait_for(p._update_task, 30)
        await p.on_unload()

        # ---- 失败保留旧库 + 取消不晚写
        p, ctx = new_plugin(runtime, what_enabled=True)
        await p.on_load()
        H._stop_bg(p)
        runtime.library_state["refresh"] = lambda progress=None, should_stop=None: FakeRefreshResult(
            status="failed", stage="detail", done=37, total=722,
            error_code="network_error",
            reasons=[{"code": "http_status", "detail": "detail 504 @710001"}],
            old_generation_ready=True, first_build=False,
        )
        await p.handle_update(stream_id="u_fail", group_id="g1", platform="qq")
        await asyncio.wait_for(p._update_task, 30)
        await asyncio.wait_for(p._what_update_task, 30)
        fail_msg = [t[1] for t in ctx.send.sent if "资料库同步失败" in t[1]]
        check("W6i 同步失败：只发阶段/已完成数/旧库状态，错误码与逐条原因不进 QQ",
              len(fail_msg) == 1 and "详情阶段" in fail_msg[0] and "37/722" in fail_msg[0]
              and "旧库仍为上次发布版本" in fail_msg[0]
              and "network_error" not in fail_msg[0] and "504" not in fail_msg[0],
              fail_msg[0] if fail_msg else "no fail msg")
        await p.on_unload()

        p, ctx = new_plugin(runtime, what_enabled=True)
        await p.on_load()
        H._stop_bg(p)
        seen_stop: List[bool] = []
        started = threading.Event()
        hold = threading.Event()

        def cancellable_refresh(progress=None, should_stop=None):
            started.set()
            hold.wait(1.5)
            seen_stop.append(bool(should_stop and should_stop()))
            return FakeRefreshResult(status="cancelled", generation=None, old_generation_ready=True)

        runtime.library_state["refresh"] = cancellable_refresh
        await p.handle_update(stream_id="u_cancel", group_id="g1", platform="qq")
        await asyncio.to_thread(started.wait, 3)
        p.config.what.enabled = False
        await p.on_config_update(scope="self", config_data={}, version="1.5.0")
        check("W6j 关闭开关即取消在飞同步（信号已置位、任务句柄清空）",
              p._what_update_stop.is_set() and p._what_update_task is None,
              f"task={p._what_update_task}")
        hold.set()
        for _ in range(300):
            if seen_stop:
                break
            await asyncio.sleep(0.01)
        check("W6k worker 看到取消信号后收敛（不推进指针）",
              seen_stop == [True], f"seen_stop={seen_stop}")
        await p.on_unload()

        # 卸载同样置取消信号
        p, ctx = new_plugin(runtime, what_enabled=True)
        await p.on_load()
        H._stop_bg(p)
        await p.on_unload()
        check("W6l 卸载置取消信号（阻止晚写指针）", p._what_update_stop.is_set())
    finally:
        plug.gamekee_build_index = orig_build
        plug.refresh_board_for_update = orig_refresh
        plug.gamekee_probe_updates = orig_probe_updates


async def section_contract_smoke(runtime: FakeRuntime) -> None:
    print("--- W7 真实模块契约冒烟（未落地则跳过） ---")
    real_lib = _load_real_module("what_library")
    real_dl = _load_real_module("what_delivery")
    if real_lib is not None:
        check("W7a what_library 暴露 WhatLibrary(open_current/refresh) 与两类异常",
              hasattr(real_lib, "WhatLibrary") and hasattr(real_lib, "WhatLibraryError")
              and hasattr(real_lib, "WhatLibraryUnavailable")
              and hasattr(real_lib.WhatLibrary, "open_current")
              and hasattr(real_lib.WhatLibrary, "refresh"))
        gen = real_lib.Generation(name="kb-x.sqlite3", db_path=Path("/tmp/kb-x.sqlite3"))
        check("W7b Generation 提供 as_meta/is_stale（插件时效提示依赖）",
              gen.as_meta().get("generation") == "kb-x.sqlite3"
              and hasattr(gen, "is_stale") and hasattr(gen, "last_verified"))
        unavailable = real_lib.WhatLibraryUnavailable("pending", "还没发布", reason="staging_only")
        check("W7c WhatLibraryUnavailable(kind,detail,reason=) 与插件分流一致",
              unavailable.kind == "pending" and unavailable.reason == "staging_only"
              and isinstance(unavailable.detail, str))
    else:
        print("W7a-what_library 尚未落地 -> SKIP（接线用替身覆盖）")
    if real_dl is not None:
        sig = inspect.signature(real_dl.deliver_what)
        check("W7d what_delivery 暴露 prepare_delivery/deliver_what，且 deliver_what 接受插件传入的参数",
              hasattr(real_dl, "prepare_delivery")
              and {"stream_id", "send_forward", "plan", "load_image", "budget_bytes",
                   "host_version", "logger"} <= set(sig.parameters))
        check("W7e 交付状态枚举含五态（sent/nothing/partial/unknown/failed）",
              {real_dl.STATUS_SENT, real_dl.STATUS_NOTHING, real_dl.STATUS_PARTIAL,
               real_dl.STATUS_UNKNOWN, real_dl.STATUS_FAILED}
              == {"sent", "nothing", "partial", "unknown", "failed"})
        check("W7f 固定落款与插件约定一致",
              real_dl.SOURCE_LINE == "来源：https://www.gamekee.com/stellasora")
    else:
        print("W7d-what_delivery 尚未落地 -> SKIP（接线用替身覆盖）")


def _load_real_module(name: str):
    """按文件加载 tools/ 下的真实模块（绕过 sys.modules 里的替身）。

    先登记 sys.modules：`from __future__ import annotations` + dataclass 的模块级
    类型解析要靠 `cls.__module__` 回查模块字典，否则 frozen dataclass 直接报错。
    """
    path = ROOT / "tools" / f"{name}.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"_smoke_{name}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 依赖缺失时按未落地处理
        print(f"[warn] 加载真实 {name} 失败: {exc}")
        sys.modules.pop(spec.name, None)
        return None
    return module


async def main_async() -> None:
    runtime = FakeRuntime()
    runtime.install()
    try:
        await section_declaration(runtime)
        await section_default_off(runtime)
        await section_hot_switch(runtime)
        await section_intent(runtime)
        await section_states(runtime)
        await section_offload(runtime)
        await section_update(runtime)
        await section_contract_smoke(runtime)
    finally:
        runtime.uninstall()


def main() -> int:
    asyncio.run(main_async())
    print()
    if failures:
        print(f"=== FAIL（{len(failures)} 项：{', '.join(failures)}）===")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
