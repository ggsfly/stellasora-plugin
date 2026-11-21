#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星塔旅人插件统一回归测试（1.5.0 精简版）。

运行方式：
    python tests/test_all.py              # 全量
    python tests/test_all.py G U V        # 只跑指定节

测试节（1.5.0 重写后 9 节）：
  G how 一图流端到端 —— GameKee 契约：命中/末图/泛查/别名分篇/未收录/索引不可用/下载失败/平台两层
  I 非阻塞探针      —— 图片下载 to_thread 不阻塞事件循环；异常干净传播
  J 工具参数描述    —— stellasora_how 1.5.0 description/参数契约
  K 手动更新指令    —— /st_update 强刷两榜+探查+重置计时、三路独立报告、提前闸门、不自 reload
  R 排行榜数据层    —— 瘦身提取/指针/TTL/降级/BoardUnavailable/强刷独立/HTML 分页纯函数
  S 排行榜命令链路  —— 图片节点合并转发/渲染失败短提示/notice/鉴权/平台
  T Hook 前置过滤   —— planner 工具剔除（1.5.0 仅 how）、fail-open、缓存
  U GameKee 解析匹配 —— content_json 结构/规范化/末图/队伍码边界/别名/分篇/属性泛查/畸形容错
  V 探查调度与图片  —— P2 single-flight/时间戳纪律/域名白名单/魔数/缓存
"""
from __future__ import annotations

from datetime import datetime
import asyncio
import json
import logging
import re
import sys
import tempfile
import threading
import time
import urllib.error
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT.parents[1] / "data" / "plugins" / "ggsfly.stellasora-plugin"
DICT_PATH = DATA_DIR / "dict.json"
NAMES_PATH = DATA_DIR / "names.json"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import leaderboard as lbmod  # noqa: E402
import leaderboard_render as lbrender  # noqa: E402
import gamekee as gkmod  # noqa: E402
from maibot_sdk.context import PluginContext, PluginPaths  # noqa: E402

import plugin as plug  # noqa: E402

failures: list = []

# 存活插件实例注册表：make_plugin 登记，供 V 节等需要干净计时断言的场景
# 统一取消僵尸实例的 GameKee 后台探查任务（全量 >60s 时僵尸会醒来污染计数）
_LIVE_PLUGINS: list = []


def cancel_gamekee_tasks() -> int:
    """取消全部存活实例的 GameKee 后台探查任务，返回取消数（仅测试用）。"""
    cancelled = 0
    for inst in _LIVE_PLUGINS:
        t = getattr(inst, "_gamekee_probe_task", None)
        if t is not None and not t.done():
            t.cancel()
            cancelled += 1
    return cancelled


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录单个断言结果，FAIL 时累计到 failures。"""
    print(f"{name} -> {'PASS' if ok else 'FAIL'}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def hard_fail(msg: str) -> None:
    """致命失败：立即终止（仅用于数据损坏等无法继续的场景）。"""
    print(f"[FAIL] {msg}")
    sys.exit(1)


# ===== 共享 Mock 脚手架（原分属 4 个文件，合并后唯一定义） =====

class MockLLM:
    """模拟 ctx.llm.generate：可注入固定回答/软失败/硬异常。"""

    def __init__(self, answer="mock LLM 攻略成品", fail=False, hard_fail=False):
        self.answer = answer
        self.fail = fail
        self.hard_fail = hard_fail
        self.calls = []

    async def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.hard_fail:
            raise RuntimeError("RPC hard failure")
        if self.fail:
            return {"success": False, "response": "", "error": "mock LLM down"}
        return {"success": True, "response": self.answer}


class MockSend:
    """模拟 ctx.send：text 记录 (stream_id, text)，forward 记录 (stream_id, nodes)。

    两个通道分开记录——text/forward 混写会掩盖"该走转发却发了文本"的回归。
    """

    def __init__(self):
        self.sent = []
        self.forwarded = []

    async def text(self, text, stream_id, **kwargs):
        self.sent.append((stream_id, text))
        return True

    async def forward(self, messages, stream_id, **kwargs):
        self.forwarded.append((stream_id, list(messages)))
        return True


class MockChat:
    """模拟 ctx.chat：get_all_streams 返回裸 list（SDK 代理已按 "streams" 键解包），记录调用次数。"""

    def __init__(self, streams=None, fail: bool = False):
        self.calls = 0
        self.streams = streams if streams is not None else []
        self.fail = fail

    async def get_all_streams(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("rpc down")
        return self.streams


class _ListLogHandler(logging.Handler):
    """捕获指定 logger 的日志记录，供失败路径断言（caplog 等价物）。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _FakeHTTPResponse:
    """模拟 urlopen 返回的文件对象。"""

    def __init__(self, data: bytes, code: int = 200):
        self._data, self._code = data, code

    def getcode(self) -> int:
        return self._code

    def read(self, *args) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class _FakeOpener:
    """模拟 fetcher._opener（_build_opener 产物），在实例级网络边界拦截请求。"""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list = []

    def open(self, req, timeout=10):
        self.calls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return self._handler(req, timeout=timeout)


def make_plugin():
    """创建插件实例 + mock ctx（缓存/去重参数默认关闭以便隔离用例）。

    data_dir 与 runtime_dir 都指向临时目录（隔离纪律：测试不触碰真实宿主
    持久目录）。实例注册到模块级 _LIVE_PLUGINS：on_load 会启动 GameKee 后台
    探查任务（60s 延迟），长测试（全量 >60s）中僵尸实例醒来会污染调用计数
    桩——需要干净计时断言的节（V）开头统一 cancel_gamekee_tasks()。
    """
    p = plug.create_plugin()
    cache = Path(tempfile.mkdtemp(prefix="stellasora_test_all_"))
    data_tmp = Path(tempfile.mkdtemp(prefix="stellasora_test_data_"))
    _LIVE_PLUGINS.append(p)
    ctx = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=data_tmp, runtime_dir=cache),
    )
    p._set_context(ctx)
    p._plugin_config_instance = plug.StellaSoraConfig()
    ctx.llm = MockLLM()
    ctx.send = MockSend()
    ctx.chat = MockChat()
    return p, ctx


# ===== 节 G：直发端到端（核心用例精简版） =====

# ===== 节 G：how 一图流端到端（1.5.0 GameKee 契约：匹配→下载→统一合并转发） =====

async def run_direct_send() -> None:
    """1.5.0 G 节重写：handle_how 新契约（GameKee 图流直发，无 LLM 加工）。

    旧 LLM 直发断言（人格/task_name/缓存/去重 prompt 注入）随旧链路废弃；
    _direct_send 保留至步骤 5（what 链路仍在用），其单测（G20/G23-G25/G28）
    相应保留。所有 how 用例显式传 platform="qq"（P3 执行层平台校验）。
    """
    print("--- G how 一图流端到端 ---")

    # 共享桩索引：艾蕾（单图）、薇洛（盛夏）、科洛妮丝新春双分篇（fixture 覆盖别名/分篇）
    def make_index():
        return {
            "built_at": int(time.time()),
            "pages": {},
            "characters": [
                {"name": "艾蕾", "element": "water", "note": "技伤≈印记，整体相较千小妹提升30%。", "image_urls": ["//cdnimg-v2.gamekee.com/a/ele.png"]},
                {"name": "薇洛（盛夏）", "element": "water", "note": "", "image_urls": ["//cdnimg-v2.gamekee.com/a/v1.png", "//cdnimg-v2.gamekee.com/a/v_long.png"]},
                {"name": "科洛妮丝新春·印记篇", "element": "earth", "note": "禾一位高配升级版", "image_urls": ["//cdnimg-v2.gamekee.com/a/k1.png"]},
                {"name": "科洛妮丝新春·直伤篇", "element": "earth", "note": "玫瑰上限高吃操作", "image_urls": ["//cdnimg-v2.gamekee.com/a/k2.png"]},
            ],
        }

    WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16

    def install_stubs(plugin=None, *, index=None, download=None):
        """patch plug 模块级 gamekee_* 绑定（handle_how 经模块 globals 解析，
        实例属性无效——Python 名字解析规则）。plugin 参数保留兼容旧调用。"""
        if index is None:
            plug.gamekee_build_index = lambda proxy_url=None: make_index()
        elif callable(index):
            plug.gamekee_build_index = index
        else:
            plug.gamekee_build_index = lambda proxy_url=None: index
        if download is None:
            plug.gamekee_download_image = lambda src, cache, prefer_webp=True: WEBP
        else:
            plug.gamekee_download_image = download

    p, ctx = make_plugin()
    await p.on_load()
    _stop_bg(p)
    install_stubs(p)
    llm, send = ctx.llm, ctx.send
    qq_kwargs = {"platform": "qq"}

    # G1 命中单角色：统一合并转发单节点（图段+备注段）+ stop 指引
    r = await p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_g1", **qq_kwargs)
    check("G1 命中单角色统一合并转发（图段+备注段+stop指引）",
          "已直接发送" in str(r.get("content", "")) and "stop 工具" in str(r.get("content", ""))
          and len(send.forwarded) == 1 and send.forwarded[0][0] == "stream_g1",
          str(r.get("content", ""))[:60])
    node = send.forwarded[0][1][0]
    segs = node.get("segments", [])
    img_seg = next((s for s in segs if s.get("type") == "image"), None)
    txt_seg = next((s for s in segs if s.get("type") == "text"), None)
    check("G1b 节点结构（署名+image段含base64+text段含属性/名称/备注/来源）",
          node.get("nickname") == "星塔旅人"
          and img_seg is not None and len(img_seg.get("content", "")) > 0
          and txt_seg is not None and "【水】艾蕾" in txt_seg["content"]
          and "技伤≈印记" in txt_seg["content"]
          and "GameKee" in txt_seg["content"],
          str(txt_seg["content"][:40]) if txt_seg else "no text seg")
    check("G1c 无 LLM 参与（攻略图直发，无加工）", len(llm.calls) == 0)

    # G2 多图角色取末张（完整版长图）
    downloaded: list = []

    def record_download(src, cache, prefer_webp=True):
        downloaded.append(src)
        return WEBP

    install_stubs(p, download=record_download)
    await p.handle_how(query="薇洛攻略", group_id="g1", stream_id="stream_g2", **qq_kwargs)
    check("G2 多图角色取末张完整长图", downloaded == ["//cdnimg-v2.gamekee.com/a/v_long.png"], str(downloaded))

    # G3 属性泛查：每角色一节点（水系=艾蕾+薇洛）
    install_stubs(p)
    n_before = len(send.forwarded)
    r3 = await p.handle_how(query="水系攻略", group_id="g1", stream_id="stream_g3", **qq_kwargs)
    nodes3 = send.forwarded[n_before][1]
    check("G3 属性泛查每角色一节点（2节点+返回张数）",
          len(nodes3) == 2 and "（2 张）" in str(r3.get("content", "")),
          f"nodes={len(nodes3)}")
    # 每个节点都含 image 段
    check("G3b 每节点均含图片段",
          all(any(s.get("type") == "image" for s in n.get("segments", [])) for n in nodes3))

    # G4 别名+分篇：春科→科洛妮丝新春全部分篇；点名直伤篇只发该篇
    p._plugin_config_instance.overrides.aliases = [plug.AliasEntry(alias="春科", official="科洛妮丝新春")]
    n_before = len(send.forwarded)
    await p.handle_how(query="春科攻略", group_id="g1", stream_id="stream_g4", **qq_kwargs)
    names4 = [_node_text_of(send.forwarded[n_before][1])]
    check("G4 别名命中全部分篇（2节点）", len(send.forwarded[n_before][1]) == 2,
          f"nodes={len(send.forwarded[n_before][1])}")
    n_before = len(send.forwarded)
    await p.handle_how(query="春科直伤篇", group_id="g1", stream_id="stream_g4b", **qq_kwargs)
    check("G4b 分篇点名只发该篇（1节点且文本含直伤篇）",
          len(send.forwarded[n_before][1]) == 1
          and "直伤篇" in _node_text_of(send.forwarded[n_before][1]),
          _node_text_of(send.forwarded[n_before][1])[:40])

    # G5 未收录：明确提示不发送图片
    n_sent, n_fwd = len(send.sent), len(send.forwarded)
    r5 = await p.handle_how(query="不存在的角色XYZ", group_id="g1", stream_id="stream_g5", **qq_kwargs)
    check("G5 未收录明确提示（不发送图片+stop指引）",
          len(send.sent) == n_sent + 1 and "没有" in send.sent[-1][1]
          and len(send.forwarded) == n_fwd
          and "已向聊天发送" in str(r5.get("content", ""))
          and "stop 工具" in str(r5.get("content", "")))

    # G6 索引不可用（无快照+探查失败）：明确提示
    def boom_build(proxy_url=None):
        raise plug.GameKeeError("probe down")

    p._gamekee_index = None
    p._gamekee_state.clear()
    install_stubs(p, index=boom_build)
    n_sent = len(send.sent)
    r6 = await p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_g6", **qq_kwargs)
    check("G6 索引不可用明确提示（不发送图片+stop指引）",
          len(send.sent) == n_sent + 1 and "不可用" in send.sent[-1][1]
          and "已向聊天发送" in str(r6.get("content", "")))
    # 恢复索引快照（后续用例依赖）
    p._gamekee_index = make_index()
    p._gamekee_state["last_verified"] = p._now()
    install_stubs(p)

    # G7 部分下载失败：跳过失败节点仍发送成功部分
    def flaky_download(src, cache, prefer_webp=True):
        if "v1.png" in src or "v_long.png" in src:
            raise plug.GameKeeError("image boom")
        return WEBP

    install_stubs(p, download=flaky_download)
    n_before = len(send.forwarded)
    r7 = await p.handle_how(query="水系攻略", group_id="g1", stream_id="stream_g7", **qq_kwargs)
    check("G7 部分下载失败跳过节点仍发送（1节点=仅艾蕾）",
          "已直接发送" in str(r7.get("content", "")) and "（1 张）" in str(r7.get("content", ""))
          and len(send.forwarded[n_before][1]) == 1,
          str(r7.get("content", ""))[:50])

    # G8 全部下载失败：明确提示不发送
    def all_fail(src, cache, prefer_webp=True):
        raise plug.GameKeeError("down")

    install_stubs(p, download=all_fail)
    n_sent = len(send.sent)
    r8 = await p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_g8", **qq_kwargs)
    check("G8 全部下载失败明确提示（不发送+stop指引）",
          len(send.sent) == n_sent + 1 and "下载失败" in send.sent[-1][1]
          and "已向聊天发送" in str(r8.get("content", "")))
    install_stubs(p)

    # G9 平台校验（P3 执行层）：非 QQ/未知平台保守拒绝且零网络
    dl_calls_before_g9 = len(downloaded) if isinstance(downloaded, list) else 0
    r9a = await p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_g9a", platform="telegram")
    r9b = await p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_g9b", user_id="u_unknown")
    check("G9 非QQ/未知平台保守拒绝（不发送不下载）",
          "不支持" in str(r9a.get("content", "")) and "不支持" in str(r9b.get("content", ""))
          and len(downloaded) == dl_calls_before_g9,
          f"dl_delta={len(downloaded) - dl_calls_before_g9}")

    # G10 黑白名单仍然生效（平台先于名单？否——名单拒绝优先级独立，两层都要过）
    p._plugin_config_instance.access_control.mode = "whitelist"
    p._plugin_config_instance.access_control.whitelist = ["g1"]
    r10 = await p.handle_how(query="艾蕾攻略", group_id="g999", stream_id="stream_g10", **qq_kwargs)
    check("G10 白名单拒绝", "允许范围" in str(r10.get("content", "")))
    p._plugin_config_instance.access_control.mode = "off"

    # G11 索引 TTL 内的第二次查询零探查（fresh 复用）
    fresh_calls = [0]

    def counting_build(proxy_url=None):
        fresh_calls[0] += 1
        return make_index()

    install_stubs(p, index=counting_build)
    p._gamekee_state["last_verified"] = p._now()
    await p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_g11", **qq_kwargs)
    check("G11 TTL 内二次查询零探查（fresh 复用索引）", fresh_calls[0] == 0, f"build calls={fresh_calls[0]}")

    # G12 缺 query 参数：明确返回缺少查询词（不发送）
    n_sent, n_fwd = len(send.sent), len(send.forwarded)
    r12 = await p.handle_how(query="", group_id="g1", stream_id="stream_g12", **qq_kwargs)
    check("G12 缺 query 明确返回（不发送）",
          "缺少查询词" in str(r12.get("content", ""))
          and len(send.sent) == n_sent and len(send.forwarded) == n_fwd)



def _node_text_of(nodes) -> str:
    """转发节点数组 → 全部文本段拼接（不含图片段）。"""
    return "\n".join(
        seg.get("content", "") for node in nodes for seg in node.get("segments", [])
        if seg.get("type") == "text"
    )


def _stop_bg(plugin) -> None:
    """停掉实例的 GameKee 后台探查任务（单测只测手动/懒刷新入口）。"""
    t = getattr(plugin, "_gamekee_probe_task", None)
    if t is not None and not t.done():
        t.cancel()



# ===== 节 H：输出格式（verbatim trust + infodoc 输出规则） =====

# ===== 节 I：非阻塞探针 =====

async def run_nonblocking() -> None:
    """I: 同步重活不阻塞事件循环——handle_how 的图片下载（to_thread）路径。"""
    print("--- I 非阻塞探针 ---")
    p, ctx = make_plugin()
    await p.on_load()
    _stop_bg(p)
    _i_index = {"built_at": int(time.time()), "pages": {"water": {"updated_at": 1}}, "characters": [{"name": "艾蕾", "element": "water", "note": "", "image_urls": ["//cdnimg-v2.gamekee.com/a/ele.png"]}]}
    plug.gamekee_build_index = lambda proxy_url=None: _i_index
    plug.gamekee_probe_updates = lambda proxy_url=None, timeout=20, known_updates=None: {
        "pages": {k: {"updated_at": 1, "published_at": 1} for k in ("water", "fire", "light", "dark", "wind", "earth")},
        "changed": True,
    }

    def slow_download(src, cache, prefer_webp=True):
        time.sleep(0.3)
        return b"RIFF\x00\x00\x00\x00WEBP\x00" * 8

    plug.gamekee_download_image = slow_download
    ctx.chat = MockChat(streams=[{"session_id": "sess_i", "group_id": "g1", "user_id": "u1", "platform": "qq"}])
    try:
        probe_done: list = []

        async def probe():
            t0 = time.monotonic()
            await asyncio.sleep(0.1)
            probe_done.append(time.monotonic() - t0)

        task = asyncio.create_task(p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_i", session_id="sess_i"))
        probe_task = asyncio.create_task(probe())
        r = await asyncio.wait_for(task, 15)
        await asyncio.wait_for(probe_task, 15)
        drift = probe_done[0] - 0.1
        check("I1 同步重活不阻塞事件循环（探针漂移<0.15s）", drift < 0.15, f"漂移 {drift:.3f}s")
        check("I1b how 图片直发成功", "已直接发送" in str(r.get("content", "")))

        # I2 异常干净传播：下载抛错 → 明确提示不挂起
        def raise_download(src, cache, prefer_webp=True):
            raise plug.GameKeeError("download hard down")

        plug.gamekee_download_image = raise_download
        r2 = await asyncio.wait_for(
            p.handle_how(query="艾蕾攻略", group_id="g1", stream_id="stream_i2", session_id="sess_i"), 15)
        check("I2 下载异常干净传播（提示+不挂起）", "下载失败" in str(r2.get("content", "")))
    finally:
        await p.on_unload()


# ===== 节 J：工具参数描述（群聊回退 bug 回归） =====

def run_tool_query_desc() -> None:
    print("--- J 工具参数描述 ---")
    attr = "__maibot_component_info__"
    tool_infos: dict = {}
    tool_descs: dict = {}
    tool_required: dict = {}
    for name in dir(plug.StellaSoraPlugin):
        func = getattr(plug.StellaSoraPlugin, name, None)
        info = getattr(func, attr, None) if func is not None else None
        if info is not None and getattr(info, "name", None):
            params = getattr(info, "parameters", None) or []
            tool_infos[info.name] = {
                param.name: param.description for param in params if hasattr(param, "name")
            }
            tool_descs[info.name] = getattr(info, "description", "")
            tool_required[info.name] = {
                param.name: getattr(param, "required", False)
                for param in params if hasattr(param, "name")
            }

    how_params = tool_infos.get("stellasora_how", {})
    how_desc = tool_descs.get("stellasora_how", "")
    how_required = tool_required.get("stellasora_how", {})
    desc = how_params.get("query", "")
    # J1 query 参数契约（1.5.0）：单一 query 参数必传，覆盖问题原文/角色名/属性泛查/别名
    check("J1 how.query 单参数原文契约（别名+属性泛查宣称）",
          how_required.get("query") is True and "属性泛查" in desc and "别名" in desc
          and "问题原文" in desc, desc)
    # J2 工具 description 契约（1.5.0）：一图流/攻略图/属性示例保留；question/presets 已移除。
    # 划界词实际为「角色客观资料……不在本工具范围」（不含字面 stellasora_what——
    # planner 靠 tool_search 词面与 description 语义路由，划界语义由"不在本工具范围"承担）
    check("J2 how.description 一图流契约+划界+旧参数移除",
          all(w in how_desc for w in ("一图流", "攻略图", "风队"))
          and "不在本工具范围" in how_desc
          and "question" not in how_required and "presets" not in how_required,
          f"desc={how_desc}, required={how_required}")
    # J3 query 参数：不宣称 LLM 加工/预设码（旧契约清除）
    check("J3 how.query 清除旧契约宣称（预设码/兜底归一）",
          "预设码" not in desc and "兜底归一" not in desc, desc)


# ===== 节 K：手动更新指令（st_update） =====

# -*- coding: utf-8 -*-
# K 节 1.5.0 重写内容（供替换脚本读取）
import asyncio
import threading
import time


async def run_manual_update() -> None:
    """K: /st_update 1.5.0 契约——操作员鉴权、提前闸门、强刷两榜+探查+重置计时、
    三路结果独立报告、不自 reload、不碰字典/GameKee 索引外的任何旧链路。"""
    print("--- K 手动更新指令 ---")
    p, ctx = make_plugin()
    await p.on_load()
    _stop_bg(p)
    ctx.chat = MockChat(streams=[{"session_id": "sess_k", "group_id": "g1", "user_id": "u1", "platform": "qq"}])

    func = getattr(plug.StellaSoraPlugin, "handle_update", None)
    check("K1 handle_update 方法存在", func is not None)
    attr = "__maibot_component_info__"
    info = getattr(func, attr, None) if func is not None else None
    check("K2 Command 装饰器元数据存在", info is not None)
    if info is not None:
        check("K3 指令名匹配 st_update", getattr(info, "name", "") == "st_update")
        check("K4 正则 pattern 匹配 ^/st_update", getattr(info, "command_pattern", "") == r"^/st_update")
        md = getattr(info, "metadata", None) or {}
        check("K4b 操作员级别权限声明 permission=operator",
              md.get("permission") == "operator", f"metadata={md}")

    p.config.access_control.mode = "whitelist"
    p.config.access_control.whitelist = ["allowed_group"]
    denied_res = await p.handle_update(stream_id="s1", group_id="denied_group", user_id="denied_user", platform="qq")
    check("K5 未授权调用被拦截（返回 False）", denied_res[0] is False)
    check("K6 拦截返回码为 1 且包含权限提示", denied_res[2] == 1 and "权限" in denied_res[1])
    check("K7 未授权不向聊天流发送消息", len(ctx.send.sent) == 0)
    r_plat = await p.handle_update(stream_id="s1", group_id="allowed_group", platform="telegram")
    check("K7b 非QQ平台保守拒绝且不发送", r_plat[0] is False and len(ctx.send.sent) == 0)
    p.config.access_control.mode = "off"

    probe_calls = []

    def fake_index(proxy_url=None):
        probe_calls.append(1)
        return {"built_at": int(time.time()), "pages": {}, "characters": [{"name": "艾蕾", "element": "water", "note": "", "image_urls": ["//x/y.png"]}]}

    board_results_stub = {
        "bb": {"status": "ok", "detail": "bb13 Top100"},
        "fe": {"status": "not_found", "detail": "fe8 当期未开榜"},
    }
    board_calls = []

    def fake_refresh(leaderboard_dir, proxy_url=None):
        board_calls.append(1)
        return board_results_stub

    orig_build = plug.gamekee_build_index
    orig_refresh = plug.refresh_board_for_update
    plug.refresh_board_for_update = fake_refresh
    try:
        plug.gamekee_build_index = fake_index
        await p._run_gamekee_probe(trigger="setup")
        fresh_verified = p._gamekee_state["last_verified"]
        success, msg, code = await p.handle_update(stream_id="stream_k", group_id="g1", platform="qq")
        check("K8 后台化：handler 立即返回受理 True", success is True)
        check("K9 返回码为 2（拦截触发消息不进 planner）", code == 2)
        await asyncio.wait_for(p._update_task, 30)
        check("K10 强刷绕过 TTL（探查 2 次=setup+manual，两榜各 1 次）",
              len(probe_calls) == 2 and len(board_calls) == 1,
              f"probe={len(probe_calls)} board={len(board_calls)}")
        update_msgs = [t[1] for t in ctx.send.sent if "数据更新完成" in t[1]]
        check("K11 三路独立报告（bb成功/fe未开榜/探查结果各自一行）",
              len(update_msgs) == 1
              and "联合讨伐：强刷成功" in update_msgs[0]
              and "终焉绝响：fe8 当期未开榜" in update_msgs[0]
              and "攻略源：索引已更新（1 小节）" in update_msgs[0]
              and "每日探查计时已重置" in update_msgs[0],
              update_msgs[0][:120] if update_msgs else "no msg")
        check("K12 手动成功重置每日探查计时", p._gamekee_state["last_verified"] > fresh_verified)
    finally:
        plug.gamekee_build_index = orig_build
        plug.refresh_board_for_update = orig_refresh

    board_results_fail = {
        "bb": {"status": "error", "detail": "拉取失败"},
        "fe": {"status": "ok", "detail": "fe8 Top100"},
    }

    def fake_index2(proxy_url=None):
        return {"built_at": int(time.time()), "pages": {}, "characters": []}

    def fake_refresh2(leaderboard_dir, proxy_url=None):
        return board_results_fail

    plug.gamekee_build_index = fake_index2
    plug.refresh_board_for_update = fake_refresh2
    try:
        n_sent = len(ctx.send.sent)
        success, msg, code = await p.handle_update(stream_id="stream_k2", group_id="g1", platform="qq")
        await asyncio.wait_for(p._update_task, 30)
        update_msgs = [t[1] for t in ctx.send.sent[n_sent:] if "数据更新完成" in t[1]]
        check("K14 一榜失败不吞另一榜与探查（bb失败/fe成功/探查成功同报）",
              len(update_msgs) == 1
              and "联合讨伐：失败" in update_msgs[0]
              and "终焉绝响：强刷成功" in update_msgs[0]
              and "攻略源：索引已更新" in update_msgs[0],
              update_msgs[0][:120] if update_msgs else "no msg")
    finally:
        plug.gamekee_build_index = orig_build
        plug.refresh_board_for_update = orig_refresh

    def boom_build(proxy_url=None):
        raise plug.GameKeeError("probe down")

    verified_before = p._gamekee_state.get("last_verified", 0)
    plug.gamekee_build_index = boom_build
    plug.refresh_board_for_update = fake_refresh
    try:
        n_sent = len(ctx.send.sent)
        success, msg, code = await p.handle_update(stream_id="stream_k3", group_id="g1", platform="qq")
        await asyncio.wait_for(p._update_task, 30)
        update_msgs = [t[1] for t in ctx.send.sent[n_sent:] if "数据更新完成" in t[1]]
        check("K15 探查失败报告失败且不重置计时（verified 不动+两榜照常）",
              len(update_msgs) == 1
              and "探查失败" in update_msgs[0] and "未重置" in update_msgs[0]
              and "联合讨伐：强刷成功" in update_msgs[0]
              and p._gamekee_state["last_verified"] == verified_before,
              update_msgs[0][:120] if update_msgs else "no msg")
    finally:
        plug.gamekee_build_index = orig_build
        plug.refresh_board_for_update = orig_refresh

    gate_release = threading.Event()

    def slow_refresh(leaderboard_dir, proxy_url=None):
        gate_release.wait(5)
        return board_results_stub

    plug.gamekee_build_index = fake_index2
    plug.refresh_board_for_update = slow_refresh
    try:
        res1 = await p.handle_update(stream_id="stream_k25a", group_id="g1", platform="qq")
        in_flight = p._update_task
        res2 = await p.handle_update(stream_id="stream_k25b", group_id="g1", platform="qq")
        check("K25 在飞连点被拒（False/码1/进行中文案）且任务句柄未被替换",
              res1[0] is True and res2[0] is False and res2[2] == 1
              and "已有更新任务进行中" in res2[1] and p._update_task is in_flight)
        gate_release.set()
        await asyncio.wait_for(p._update_task, 30)
    finally:
        plug.refresh_board_for_update = orig_refresh
        plug.gamekee_build_index = orig_build

    await p.on_unload()


# ===== 节 R：排行榜数据层（tools/leaderboard.py 迁出模块） =====

def run_section_r() -> None:
    """R: 榜单数据层——瘦身提取、指针跟随、TTL、空窗/降级、BoardUnavailable、强刷（零网络）。"""
    print("--- R 排行榜数据层 ---")

    slim = {
        "regions": {
            "cn": {
                "total": 1234,
                "last_refresh": "1789777540",
                "top": [
                    {"rank": 1, "id": "101", "name": "阿尔法", "score": 3139255},
                    {"rank": 2, "id": "102", "name": "Beta", "score": 2880000},
                ],
            },
            "en": {"total": 5, "last_refresh": "1", "top": [{"rank": 1, "id": "9", "name": "X", "score": 1}]},
        }
    }

    # R1 瘦身提取
    raw = {
        "region": {
            "cn": {
                "Total": 1234,
                "LastRefreshTime": "1789777540",
                "Rank": [
                    {"Id": 101, "NickName": "阿尔法", "Score": 3139255, "Rank": 1, "Teams": [{"Potentials": [1]}]},
                    {"Id": "102", "NickName": " Beta ", "Score": None, "Rank": 2},
                ],
                "UsageByFloor": {"all": [{"id": 101}]},
            }
        },
        "top_level_noise": 1,
    }
    slim2 = lbmod.extract_board_top(raw)
    check("R1a 提取仅保留 rank/id/name/score",
          slim2 and slim2["regions"]["cn"]["top"][0] == {"rank": 1, "id": "101", "name": "阿尔法", "score": 3139255},
          str(slim2 and slim2["regions"]["cn"]["top"][0]))
    check("R1b 丢弃 Usage/队伍明细", set(slim2["regions"]["cn"].keys()) == {"total", "last_refresh", "top"})
    check("R1c Score 缺省 0、名字 strip",
          slim2["regions"]["cn"]["top"][1]["score"] == 0 and slim2["regions"]["cn"]["top"][1]["name"] == "Beta")
    check("R1d 结构不符返回 None",
          lbmod.extract_board_top({"no": 1}) is None and lbmod.extract_board_top(None) is None
          and lbmod.extract_board_top({"region": {}}) is None)

    with tempfile.TemporaryDirectory(prefix="stellasora_lb_") as td:
        lb_dir = Path(td) / "leaderboard"
        lb_dir.mkdir(parents=True)
        top_file = lb_dir / "bb12_top.json"

        def write_top(ts: float) -> None:
            top_file.write_text(
                json.dumps({"url": "x", "name": "board_bb12", "timestamp": ts, "data": slim},
                           ensure_ascii=False),
                encoding="utf-8")

        orig_season = lbmod.fetch_leaderboard_season
        orig_meta = lbmod.fetch_leaderboard_meta
        orig_board = lbmod.fetch_leaderboard_board
        try:
            # R2 指针跟随 + 新鲜缓存零网络 + 未开榜
            write_top(time.time())
            calls = []

            def fake_season(d, proxy_url=None, force_update=False):
                return {"BB_SEASON": "bb12", "FE_SEASON": "fe7"}

            def fake_meta(d, proxy_url=None, force_update=False):
                return {"bb12": {}}

            def fake_board(d, sid, proxy_url=None, force_update=False):
                calls.append((sid, force_update))
                return (None, "not_found")

            lbmod.fetch_leaderboard_season = fake_season
            lbmod.fetch_leaderboard_meta = fake_meta
            lbmod.fetch_leaderboard_board = fake_board
            cn_bb, header_bb, age_bb = lbmod.load_board_for_render("bb", "bb12", 10, lb_dir)
            check("R2a 新鲜缓存零网络返回国服数据",
                  cn_bb["top"][0]["name"] == "阿尔法" and header_bb == "【联合讨伐 S12】" and age_bb < 60)
            try:
                lbmod.load_board_for_render("fe", "fe7", 10, lb_dir)
                check("R2b fe 未开榜抛 BoardUnavailable", False)
            except lbmod.BoardUnavailable as exc:
                check("R2b fe 未开榜抛 BoardUnavailable", "当期未开榜" in str(exc), str(exc)[:50])

            # R3 过期触发重拉
            write_top(time.time() - 7200)
            calls.clear()

            def fake_board_ok(d, sid, proxy_url=None, force_update=False):
                calls.append((sid, force_update))
                return (slim, "ok")

            lbmod.fetch_leaderboard_board = fake_board_ok
            cn3, _, age3 = lbmod.load_board_for_render("bb", "bb12", 10, lb_dir)
            check("R3 过期缓存触发重拉且数据新鲜",
                  calls == [("bb12", True)] and age3 == 0.0, str(calls))

            # R4 拉取失败降级陈旧缓存
            def fake_board_err(d, sid, proxy_url=None, force_update=False):
                return (None, "error")

            lbmod.fetch_leaderboard_board = fake_board_err
            cn4, _, age4 = lbmod.load_board_for_render("bb", "bb12", 10, lb_dir)
            check("R4 拉取失败降级陈旧缓存（age=2h）", age4 > 7000, f"age={age4}")

            # R5 指针缺失按 meta 降级
            def fake_season_nobb(d, proxy_url=None, force_update=False):
                return {"FE_SEASON": "fe7"}

            lbmod.fetch_leaderboard_season = fake_season_nobb
            prefix5, sid5 = lbmod.resolve_current_board("bb", lb_dir)
            check("R5 BB_SEASON 缺失按 meta 最大 bbNN 降级", (prefix5, sid5) == ("bb", "bb12"), f"{prefix5}/{sid5}")

            # R6 指针缺失且 meta 无 bb → BoardUnavailable
            def fake_meta_empty(d, proxy_url=None, force_update=False):
                return {}

            lbmod.fetch_leaderboard_meta = fake_meta_empty
            def fake_season_nofe(d, proxy_url=None, force_update=False):
                return {"BB_SEASON": "bb13"}

            lbmod.fetch_leaderboard_season = fake_season_nofe
            prefix6, sid6 = lbmod.resolve_current_board("fe", lb_dir)
            check("R6 fe 指针缺失返回空 sid（不猜）", (prefix6, sid6) == ("fe", ""), f"{prefix6}/{sid6}")
            try:
                lbmod.load_board_for_render("fe", sid6, 10, lb_dir)
                check("R6b fe 空 sid 加载数据抛 BoardUnavailable", False)
            except lbmod.BoardUnavailable as exc:
                check("R6b fe 空 sid 加载数据抛 BoardUnavailable", "指针缺失" in str(exc), str(exc)[:40])

            # R7 强刷两榜独立（refresh_board_for_update）
            def fake_season_full(d, proxy_url=None, force_update=False):
                return {"BB_SEASON": "bb13", "FE_SEASON": "fe8"}

            lbmod.fetch_leaderboard_season = fake_season_full
            board_map = {"bb13": (slim, "ok"), "fe8": (None, "not_found")}

            def fake_board_map(d, sid, proxy_url=None, force_update=False):
                return board_map.get(sid, (None, "error"))

            lbmod.fetch_leaderboard_board = fake_board_map
            results = lbmod.refresh_board_for_update(lb_dir)
            check("R7 强刷两榜独立报告（bb ok/fe not_found）",
                  results["bb"]["status"] == "ok" and results["fe"]["status"] == "not_found",
                  str(results))
        finally:
            lbmod.fetch_leaderboard_season = orig_season
            lbmod.fetch_leaderboard_meta = orig_meta
            lbmod.fetch_leaderboard_board = orig_board

    # R8 build_board_pages 纯函数：分页/转义/分数完整
    cn_big = {"total": 60, "top": [
        {"rank": i + 1, "id": str(i), "name": f"玩{i}<Script>" if i == 0 else f"玩{i}", "score": 1234567 + i} for i in range(60)
    ]}
    pages = lbrender.build_board_pages("bb", "bb13", cn_big, "刚刚")
    check("R8a 60人分3页（每页25）", len(pages) == 3, f"pages={len(pages)}")
    check("R8b 昵称 HTML 转义", "&lt;Script&gt;" in pages[0]["html"])
    check("R8c 分数千分位完整（无万缩写）", "1,234,567" in pages[0]["html"] and "1,234,626" in pages[2]["html"])
    check("R8d 页标签", pages[0]["label"].startswith("第 1–") and pages[2]["label"].startswith("第 51–"),
          f"{pages[0]['label']}/{pages[2]['label']}")
    # R8e 分数右对齐 tabular-nums（渲染纪律在模板里）
    check("R8e 模板含 tabular-nums 与右对齐", "tabular-nums" in pages[0]["html"] and 'class="score"' in pages[0]["html"])


async def run_section_s() -> None:
    """S: 排行榜命令插件链路（1.5.0 图片契约）——鉴权/平台透传、图片节点、
    渲染失败短提示、notice 文本、拒绝不发送。"""
    print("--- S 排行榜命令插件链路 ---")
    p, ctx = make_plugin()
    await p.on_load()
    _stop_bg(p)

    # 1. 命令注册元数据
    for cmd_name, handler in (("st_bb", "handle_lb_bb"), ("st_fe", "handle_lb_fe")):
        func = getattr(plug.StellaSoraPlugin, handler, None)
        info = getattr(func, "__maibot_component_info__", None) if func else None
        check(f"S1 {handler} Command 元数据存在且指令名 {cmd_name}",
              info is not None and getattr(info, "name", "") == cmd_name)

    slim = {"regions": {"cn": {"total": 2, "last_refresh": "1", "top": [
        {"rank": 1, "id": "1", "name": "甲", "score": 1000},
        {"rank": 2, "id": "2", "name": "乙", "score": 900}]}}}
    fake_png = "iVBORw0KGgoAAAANSUhEUg=="  # 最小合法 PNG base64 头

    class MockRender:
        def __init__(self, fail: bool = False):
            self.fail = fail
            self.calls: list = []

        async def html2png(self, html, **kwargs):
            self.calls.append(kwargs)
            if self.fail:
                return {"success": False, "error": "browser down"}
            return {"image_base64": fake_png, "mime_type": "image/png", "width": 100, "height": 40, "render_ms": 5}

    orig_resolve = plug.resolve_current_board
    orig_load = plug.load_board_for_render
    orig_build = plug.build_board_pages
    orig_render = plug.render_board_pages
    try:
        # 2. 正常路径：数据→分页→渲染→合并转发图片节点（平台+名单透传）
        plug.resolve_current_board = lambda mode, lb_dir=None, proxy_url=None: ("bb", "bb13")
        plug.load_board_for_render = lambda prefix, sid, ttl, lb_dir, proxy_url=None: (slim["regions"]["cn"], "【联合讨伐 S13】", 0.0)
        render = MockRender()
        plug.build_board_pages = lambda *a, **kw: __import__("leaderboard_render").build_board_pages(*a, **kw)
        ctx.render = render
        p.config.access_control.mode = "whitelist"
        p.config.access_control.whitelist = ["id_ok"]
        res = await p.handle_lb_bb(stream_id="stream_s1", group_id="id_ok", user_id="u1", platform="qq")
        check("S2 白名单+QQ 命中放行", res[0] is True)
        check("S3 榜单图片经 ctx.send.forward 送达（1页1节点含image段）",
              len(ctx.send.forwarded) == 1 and ctx.send.forwarded[0][0] == "stream_s1"
              and any(s.get("type") == "image" for s in ctx.send.forwarded[0][1][0]["segments"]),
              f"fwd={len(ctx.send.forwarded)}")
        check("S3b 渲染调用带 render_timeout_ms", render.calls and render.calls[0].get("render_timeout_ms") == 30000)
        check("S4 返回值为执行摘要而非全文", res[1] == "星塔旅人排行榜已发送" and res[2] == 2)

        # 3. 渲染失败：短提示不回退文本榜
        ctx.send.forwarded.clear()
        render_fail = MockRender(fail=True)
        ctx.render = render_fail
        n_sent = len(ctx.send.sent)
        res_fail = await p.handle_lb_fe(stream_id="stream_s2", group_id="id_ok", platform="qq")
        check("S5 渲染失败短提示（不回退文本榜）",
              res_fail[0] is True and len(ctx.send.sent) == n_sent + 1
              and "渲染失败" in ctx.send.sent[-1][1] and len(ctx.send.forwarded) == 0,
              str(res_fail[1]))

        # 4. 未开榜走 notice 文本（BoardUnavailable）
        plug.load_board_for_render = lambda prefix, sid, ttl, lb_dir, proxy_url=None: (_ for _ in ()).throw(
            __import__("leaderboard").BoardUnavailable("【终焉绝响 S8】 当期未开榜（上游尚未发布榜单数据）。"))
        n_sent = len(ctx.send.sent)
        res_nf = await p.handle_lb_fe(stream_id="stream_s3", group_id="id_ok", platform="qq")
        check("S6 未开榜 notice 文本发送",
              res_nf[0] is True and len(ctx.send.sent) == n_sent + 1
              and "当期未开榜" in ctx.send.sent[-1][1])

        # 5. 拒绝路径不发送（白名单未命中 + 非QQ）
        n_sent, n_fwd = len(ctx.send.sent), len(ctx.send.forwarded)
        res_deny = await p.handle_lb_fe(stream_id="stream_s4", group_id="other_group", platform="qq")
        check("S7 白名单未命中拒绝", res_deny[0] is False and res_deny[2] == 1)
        check("S8 拒绝路径不发送任何消息",
              len(ctx.send.sent) == n_sent and len(ctx.send.forwarded) == n_fwd)
        res_plat = await p.handle_lb_fe(stream_id="stream_s5", group_id="id_ok", platform="telegram")
        check("S9 非QQ平台拒绝且不发送",
              "不支持" in res_plat[1] and len(ctx.send.sent) == n_sent and len(ctx.send.forwarded) == n_fwd)

        # 6. 私聊 user_id 命中放行并发送图片（恢复 S2 的正常数据 mock——S6 抛异常桩未还原）
        plug.load_board_for_render = lambda prefix, sid, ttl, lb_dir, proxy_url=None: (
            slim["regions"]["cn"], "【联合讨伐 S13】", 0.0)
        ctx.send.forwarded.clear()
        ctx.render = MockRender()
        res = await p.handle_lb_fe(stream_id="stream_s6", user_id="id_ok", platform="qq")
        check("S10 私聊 user_id 命中放行并发送图片",
              res[0] is True and len(ctx.send.forwarded) == 1 and ctx.send.forwarded[0][0] == "stream_s6",
              f"res={res} fwd={len(ctx.send.forwarded)}")
    finally:
        plug.resolve_current_board = orig_resolve
        plug.load_board_for_render = orig_load
        plug.build_board_pages = orig_build
        plug.render_board_pages = orig_render
        p.config.access_control.mode = "off"



# ===== 节 T：Hook 前置过滤（planner 工具暴露治理） =====

def _hook_kwargs(tool_defs, session_id: str) -> dict:
    """构造 maisaka.planner.before_request 的完整 8 键 payload。

    与宿主 hook_dispatcher 一致：hook_name 由 dispatcher 注入在首位。
    """
    return {
        "hook_name": "maisaka.planner.before_request",
        "items": [],
        "item_schema_version": 1,
        "tool_definitions": tool_defs,
        "selected_history_count": 2,
        "built_message_count": 5,
        "selection_reason": "test",
        "session_id": session_id,
    }


def _tool_def(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "x", "parameters": {}}}


async def run_hook_gate() -> None:
    print("--- T Hook 前置过滤 ---")
    p, ctx = make_plugin()
    await p.on_load()

    def plugin_defs() -> list:
        return [_tool_def(n) for n in sorted(plug._TOOL_NAMES)]

    # T1 无本插件工具：原样 continue（不携带 modified_kwargs）
    r = await p._filter_planner_tools(**_hook_kwargs([_tool_def("other_tool")], "sess_t1"))
    check("T1 无本插件工具原样放行", r == {"action": "continue"})

    # T2 mode=off + QQ 平台：不过滤（预填身份+平台缓存，模拟 Hook 链路已反查）
    p._session_identity_cache["sess_t2"] = ("g_t2", "u_t2")
    p._session_platform_cache["sess_t2"] = "qq"
    r = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_t2"))
    check("T2 mode=off 且 QQ 平台工具保留", r == {"action": "continue"})
    # T2b 非平台缓存（未知平台）保守剔除
    r2b = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_t2_noplat"))
    kept2b = [d.get("function", {}).get("name") for d in r2b.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("T2b 未知平台保守剔除（P3 两层）", r2b.get("action") == "continue" and kept2b == [],
          f"kept={kept2b}")

    # T3-T5：预填身份缓存（绕过 get_all_streams 路径，独立覆盖在 T6/T7）
    p._session_identity_cache.update({
        "sess_g_ok": ("g_ok", "u1"),
        "sess_g_no": ("g_no", "u2"),
        "sess_blk": ("g_blk", "u3"),
    })
    p.config.access_control.mode = "whitelist"
    p.config.access_control.whitelist = ["g_ok"]
    p._session_platform_cache["sess_g_ok"] = "qq"
    r = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_g_ok"))
    check("T3 白名单命中且 QQ 平台工具保留", r == {"action": "continue"})
    # T3b 非 QQ 平台（过名单）剔除
    p._session_platform_cache["sess_g_ok_noplat"] = "telegram"
    r3b = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_g_ok_noplat"))
    kept3b = [d.get("function", {}).get("name") for d in r3b.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("T3b 非QQ平台剔除（P3 两层）", r3b.get("action") == "continue" and kept3b == [],
          f"kept={kept3b}")

    # T4 白名单未命中：剔除本插件三工具，foreign 工具与畸形元素保留，modified_kwargs 完整回传
    defs = plugin_defs() + [_tool_def("foreign_tool"), {"broken": True}]
    r = await p._filter_planner_tools(**_hook_kwargs(defs, "sess_g_no"))
    kept_names = [d.get("function", {}).get("name") for d in r.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("T4 未命中剔除本插件工具（foreign+畸形保留）且 8 键完整回传",
          r.get("action") == "continue" and kept_names == ["foreign_tool", None]
          and len(r["modified_kwargs"]["tool_definitions"]) == 2
          and set(r["modified_kwargs"]) == {"hook_name", "items", "item_schema_version",
                                            "tool_definitions", "selected_history_count",
                                            "built_message_count", "selection_reason", "session_id"},
          f"kept={kept_names}")

    # T5 黑名单命中：剔除
    p.config.access_control.mode = "blacklist"
    p.config.access_control.blacklist = ["g_blk"]
    r = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_blk"))
    kept_names = [d.get("function", {}).get("name") for d in r.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("T5 黑名单命中剔除本插件工具", r.get("action") == "continue" and kept_names == [],
          f"kept={kept_names}")
    p.config.access_control.mode = "off"

    # T6 身份/平台查询失败：保守剔除（P3 口径——未知宁可漏不可错；执行层同口径兜底）
    p.config.access_control.mode = "whitelist"
    p.config.access_control.whitelist = ["g_ok"]
    ctx.chat = MockChat(fail=True)
    r = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_unknown"))
    kept6 = [d.get("function", {}).get("name") for d in r.get("modified_kwargs", {}).get("tool_definitions", [])]
    check("T6 反查失败保守剔除（未知平台/身份）", r.get("action") == "continue" and kept6 == [],
          f"kept={kept6}")

    # T7 缓存命中零 RPC + 溢出清空：monkeypatch 上界为 3，未缓存会话触发一次查询后写缓存
    mock_chat = MockChat(streams=[{"session_id": "sess_new", "group_id": "g9", "user_id": "u9"}])
    ctx.chat = mock_chat
    orig_cap = plug.MAX_SESSION_CACHE_ENTRIES
    plug.MAX_SESSION_CACHE_ENTRIES = 3
    try:
        p._session_identity_cache.update({"j1": ("a", ""), "j2": ("b", ""), "j3": ("c", "")})
        r = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_new"))
        r2 = await p._filter_planner_tools(**_hook_kwargs(plugin_defs(), "sess_new"))
        check("T7 首查写缓存剔除生效（溢出先清空）+ 二次命中零 RPC",
              r.get("action") == "continue"
              and [d.get("function", {}).get("name") for d in r["modified_kwargs"]["tool_definitions"]] == []
              and mock_chat.calls == 1 and p._session_identity_cache.get("sess_new") == ("g9", "u9")
              and r2 == r)
    finally:
        plug.MAX_SESSION_CACHE_ENTRIES = orig_cap
    p.config.access_control.mode = "off"

    # T8 声明契约：仅剩 how 工具且 platforms=qq（lookup/what 已随 1.5.0 移除）
    metas = {}
    plats = {}
    for cname in dir(plug.StellaSoraPlugin):
        func = getattr(plug.StellaSoraPlugin, cname, None)
        info = getattr(func, "__maibot_component_info__", None) if func else None
        if info is not None and getattr(info, "name", None):
            metas[info.name] = getattr(info, "metadata", None) or {}
            plats[info.name] = getattr(info, "platforms", None)
    tool_plats = {n: plats[n] for n in plug._TOOL_NAMES if n in plats}
    check("T8 工具组件仅剩 how 且 platforms=qq（lookup/what 已移除）",
          set(tool_plats) == {"stellasora_how"} and tool_plats["stellasora_how"] in ("qq", ["qq"]),
          f"tool_plats={tool_plats}")
def _u_nodes_single(name: str, note: str, src: str) -> list:
    """单图小节（不含队伍码区——队伍码边界由 U5 独立小节覆盖）。"""
    return [
        {"type": "header1", "children": [{"text": f"{name}属性角色一图流攻略"}]},
        {"type": "header2", "children": [{"text": f"{name}一图流攻略"}]},
        {"type": "paragraph", "children": [{"text": note}]},
        {"type": "paragraph", "children": [
            {"text": ""},
            {"type": "image", "src": src, "style": {"width": "1080px", "height": "1440px"}},
        ]},
    ]


def _u_nodes_album(name: str, srcs: list) -> list:
    """多图小节：header2 + image-album（末张=完整长图）。"""
    return [
        {"type": "header2", "children": [{"text": f"{name}一图流攻略"}]},
        {"type": "image-album", "data": srcs, "limit": [False] * len(srcs)},
        {"type": "paragraph", "children": [{"text": "左右滑动可以查看下一张图！！"}]},
    ]


def _u_nodes_parted(name: str, part: str, src: str) -> list:
    """分篇小节：标题含中文冒号后缀。"""
    return [
        {"type": "header2", "children": [{"text": f"{name}攻略一图流：{part}"}]},
        {"type": "paragraph", "children": [{"text": "备注"}]},
        {"type": "image-album", "data": [src]},
    ]


def run_section_u() -> None:
    """U: GameKee 解析与匹配——规范化/末图/队伍码边界/别名/分篇/属性泛查/畸形容错暴露。"""
    print("--- U GameKee 解析与匹配 ---")
    nodes = _u_nodes_single("艾蕾", "技伤≈印记。", "//cdnimg-v2.gamekee.com/a/ele.png")
    entries = gkmod.parse_content_json(nodes, "water")
    check("U1 单图解析：名称规范化+备注+图链",
          len(entries) == 1 and entries[0]["name"] == "艾蕾" and entries[0]["note"] == "技伤≈印记。"
          and entries[0]["image_urls"] == ["//cdnimg-v2.gamekee.com/a/ele.png"]
          and entries[0]["element"] == "water", str(entries))

    album_srcs = ["//cdnimg-v2.gamekee.com/a/1.png", "//cdnimg-v2.gamekee.com/a/2.png", "//cdnimg-v2.gamekee.com/a/long.png"]
    entries2 = gkmod.parse_content_json(_u_nodes_album("薇洛（盛夏）", album_srcs), "water")
    check("U2 album 全链保留且末张可取",
          len(entries2) == 1 and entries2[0]["image_urls"] == album_srcs
          and entries2[0]["image_urls"][-1].endswith("long.png"), str(entries2))

    mixed = _u_nodes_single("千都世", "有薇洛很强。", "//cdnimg-v2.gamekee.com/a/q.png")
    mixed += _u_nodes_album("多娜", ["//cdnimg-v2.gamekee.com/a/m1.png", "//cdnimg-v2.gamekee.com/a/m2.png"])
    entries3 = gkmod.parse_content_json(mixed, "water")
    check("U3 多小节互不串图",
          [e["name"] for e in entries3] == ["千都世", "多娜"]
          and entries3[0]["image_urls"] == ["//cdnimg-v2.gamekee.com/a/q.png"]
          and entries3[1]["image_urls"] == ["//cdnimg-v2.gamekee.com/a/m1.png", "//cdnimg-v2.gamekee.com/a/m2.png"],
          str([e["name"] for e in entries3]))

    check("U4 角色名规范化（括号/分篇）",
          gkmod.normalize_character_name("尘沙（源初）一图流攻略") == "尘沙（源初）"
          and gkmod.normalize_character_name("科洛妮丝新春攻略一图流：印记篇") == "科洛妮丝新春·印记篇"
          and gkmod.normalize_character_name("千都世攻略一图流") == "千都世")

    # 队伍码边界：header1"队伍码"后的 header2（队伍名）不生成小节
    entries4 = gkmod.parse_content_json(_u_nodes_single("艾蕾", "n", "//x/y.png"), "water")
    check("U5 队伍码区止步（队伍名不进索引）",
          len(entries4) == 1 and entries4[0]["name"] == "艾蕾")

    # 属性泛查 + 别名 + 分篇（用 build_index 的测试注入路径，单页即可）
    index = gkmod.build_index(pages={
        "water": (0, "水系阵容推荐", _u_nodes_single("艾蕾", "n1", "//a/ele.png") + _u_nodes_album("薇洛（盛夏）", ["//a/v1.png", "//a/v2.png"])),
        "earth": (0, "地系阵容推荐", _u_nodes_parted("科洛妮丝新春", "印记篇", "//a/k1.png") + _u_nodes_parted("科洛妮丝新春", "直伤篇", "//a/k2.png")),
    })
    aliases = {"春科": "科洛妮丝新春", "土": "地"}
    h1, r1 = gkmod.match_guides(index, "艾蕾攻略", aliases=aliases)
    check("U6 角色命中单节", len(h1) == 1 and h1[0]["name"] == "艾蕾", f"{len(h1)}/{r1}")
    h2, r2 = gkmod.match_guides(index, "水系攻略", aliases=aliases)
    check("U7 属性泛查命中该属性全部小节", len(h2) == 2 and {e["name"] for e in h2} == {"艾蕾", "薇洛（盛夏）"}, f"{len(h2)}")
    h3, r3 = gkmod.match_guides(index, "春科攻略", aliases=aliases)
    check("U8 别名命中全部分篇（未点名分篇）",
          len(h3) == 2 and {e["name"] for e in h3} == {"科洛妮丝新春·印记篇", "科洛妮丝新春·直伤篇"}, f"{len(h3)}/{r3}")
    h4, r4 = gkmod.match_guides(index, "春科直伤篇", aliases=aliases)
    check("U9 分篇点名只发该篇", len(h4) == 1 and h4[0]["name"] == "科洛妮丝新春·直伤篇", f"{len(h4)}")
    h5, r5 = gkmod.match_guides(index, "土印记队", aliases=aliases)
    check("U10 别名→属性泛查", len(h5) == 2 and all(e["element"] == "earth" for e in h5), f"{len(h5)}/{r5}")
    h6, r6 = gkmod.match_guides(index, "不存在的角色XYZ", aliases=aliases)
    check("U11 未收录明确 no_match", h6 == [] and r6 == "no_match", f"{r6}")
    h7, r7 = gkmod.match_guides(index, "", aliases=aliases)
    check("U12 空 query 返回 empty_query", h7 == [] and r7 == "empty_query")

    # 图片 URL 规则
    u1 = gkmod.image_url("//cdnimg-v2.gamekee.com/a/x.png")
    u2 = gkmod.image_url("//cdnimg-v2.gamekee.com/a/x.png", prefer_webp=False)
    check("U13 image_url 补协议+webp 参数",
          u1 == "https://cdnimg-v2.gamekee.com/a/x.png" + gkmod._WEBP_QUERY
          and u2 == "https://cdnimg-v2.gamekee.com/a/x.png")

    # 畸形 content_json：解析器对空节点/无图小节健壮（返回空列表而非崩溃）
    broken = [{"type": "header2", "children": [{"text": "无图角色"}]}, {"type": "paragraph"}, None]
    entries5 = gkmod.parse_content_json(broken, "fire")
    check("U14 无图小节不进索引（明确空）", entries5 == [], str(entries5))


# ===== 节 V：GameKee 探查调度（P2 single-flight/时间戳纪律）与图片下载契约 =====

class _VFakeIndex:
    """GameKee 探查桩（1.5.0 双层：probe_updates 轻量 + build_index 重建）。

    changed=True 时 probe 报有变化 → 真实走 build；fail 时 probe 直接抛错。
    calls = probe 调用数；build_calls = 重建调用数。
    """

    def __init__(self, fail: bool = False, delay: float = 0.0, changed: bool = True):
        self.fail = fail
        self.delay = delay
        self.changed = changed
        self.calls = 0
        self.build_calls = 0
        self._index = {"built_at": int(time.time()), "pages": {}, "characters": [{"name": "艾蕾", "element": "water", "note": "n", "image_urls": ["//x/y.png"]}]}

    def probe(self, proxy_url=None, timeout=20, known_updates=None):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise gkmod.GameKeeError("network down")
        return {"pages": {k: {"updated_at": 1, "published_at": 1} for k in ("water", "fire", "light", "dark", "wind", "earth")}, "changed": self.changed}

    def __call__(self, proxy_url=None):
        self.build_calls += 1
        return self._index

    def install(self, module):
        module.gamekee_probe_updates = self.probe
        module.gamekee_build_index = self


async def run_section_v() -> None:
    """V: P2 探查调度纪律 + 图片下载契约（域名白名单/魔数校验/缓存）。"""
    print("--- V GameKee 探查调度与图片 ---")
    # 先杀掉全部僵尸实例的后台探查任务（前置节实例 60s 后醒来会用本节桩调用，
    # 污染 V7/V8/V9 的调用计数断言），再创建本节实例并停掉它的后台任务
    cancel_gamekee_tasks()
    await asyncio.sleep(0)  # 让 cancel 生效一轮
    p, ctx = make_plugin()
    await p.on_load()
    # 停掉后台探查排程（本节只测手动/懒刷新入口的调度纪律；
    # 后台任务 due=0 → 60s 后会醒来插入额外构建，污染调用计数断言）
    if p._gamekee_probe_task and not p._gamekee_probe_task.done():
        p._gamekee_probe_task.cancel()
        await asyncio.gather(p._gamekee_probe_task, return_exceptions=True)
        p._gamekee_probe_task = None

    # V1 首次无状态：due=0（启动延迟探查路径）
    check("V1 首次无状态 due=0", p._gamekee_due_delay() == 0.0)

    fake = _VFakeIndex()
    orig_build = plug.gamekee_build_index
    orig_probe = plug.gamekee_probe_updates
    fake.install(plug)
    try:
        # V2 探查成功：三时间戳 + 索引发布 + state 落盘
        idx = await p._run_gamekee_probe(trigger="test")
        st = p._gamekee_state
        check("V2 探查成功发布索引+落盘",
              idx.get("characters") and p._gamekee_index is idx
              and st.get("last_verified", 0) > 0 and st.get("last_probe_attempt", 0) > 0
              and p._gamekee_state_path().exists()
              and json.loads(p._gamekee_state_path().read_text(encoding="utf-8")).get("last_verified") == st["last_verified"])
        check("V2b changed=True 走完整重建（probe1/build1）",
              fake.calls == 1 and fake.build_calls == 1, f"probe={fake.calls} build={fake.build_calls}")
        # V3 成功后 24h 正常调度
        d = p._gamekee_due_delay()
        check("V3 成功后 due=24h", 86300 < d <= 86400, f"{d:.0f}s")

        # V4 失败：verified 不动、attempt 推进、快照保留
        verified_before = st["last_verified"]
        fail_fake = _VFakeIndex(fail=True)
        fail_fake.install(plug)
        try:
            try:
                await p._run_gamekee_probe(trigger="fail")
                check("V4 失败必须抛 GameKeeProbeFailure", False)
            except plug.GameKeeProbeFailure:
                pass
            check("V4b 失败纪律（verified不动/attempt推进/快照保留）",
                  p._gamekee_state["last_verified"] == verified_before
                  and p._gamekee_state["last_probe_attempt"] > verified_before
                  and p._gamekee_index is not None)
            # V5 失败后 30min 重试调度（不连拉不断停摆）
            d2 = p._gamekee_due_delay()
            expect = p._gamekee_state["last_probe_attempt"] + p._GAMEKEE_RETRY_INTERVAL - p._now()
            check("V5 失败重试延迟=30min", abs(d2 - max(0, expect)) < 1.5, f"{d2:.0f}s")
        finally:
            fake.install(plug)

        # V6 懒刷新：fresh 零探查；stale+失败带旧快照；无快照上抛
        fail_fake.install(plug)
        p._gamekee_state["last_verified"] = p._now()
        idx_fresh = await p.ensure_gamekee_index()
        calls_at_fresh = fail_fake.calls
        check("V6a fresh 不触发探查", idx_fresh is not None and fail_fake.calls == calls_at_fresh)
        p._gamekee_state["last_verified"] = p._now() - 10 * 3600
        idx_stale = await p.ensure_gamekee_index()
        check("V6b stale+失败带旧快照降级", idx_stale is not None)
        p._gamekee_index = None
        try:
            await p.ensure_gamekee_index()
            check("V6c 无快照失败必须上抛", False)
        except plug.GameKeeProbeFailure:
            check("V6c 无快照失败必须上抛", True)
        finally:
            fake.install(plug)

        # V7 single-flight 成功合并：并发 3 入口 1 次构建。
        # 分步启动消除调度竞争：先启动 a 并等它进入在飞批次（batch 已设），
        # 再放 b/c——否则系统负载下 b 的首轮调度可能晚于 a 完成，误判为独立调用。
        slow = _VFakeIndex(delay=0.3)
        slow.install(plug)
        p._gamekee_index = None
        p._gamekee_state.clear()
        task_a = asyncio.create_task(p._run_gamekee_probe("a"))
        for _ in range(200):
            if p._gamekee_batch is not None:
                break
            await asyncio.sleep(0.005)
        if p._gamekee_batch is None:
            check("V7-prep a 已进入在飞批次", False,
                  f"batch=None calls={slow.calls} state={p._gamekee_state}")
        else:
            check("V7-prep a 已进入在飞批次", True)
        results = await asyncio.gather(task_a, p._run_gamekee_probe("b"), p._run_gamekee_probe("c"), return_exceptions=True)
        ok = [r for r in results if not isinstance(r, Exception)]
        check("V7 并发合并请求（1次探查+1次构建服务3入口）",
              len(ok) == 3 and slow.calls == 1 and slow.build_calls == 1,
              f"ok={len(ok)} probe={slow.calls} build={slow.build_calls}")

        # V8 single-flight 失败合并：等待者拿失败结果不重拉（分步启动同 V7）
        slow_fail = _VFakeIndex(fail=True, delay=0.2)
        slow_fail.install(plug)
        p._gamekee_index = None
        p._gamekee_state.clear()
        task_a2 = asyncio.create_task(p._run_gamekee_probe("a"))
        for _ in range(200):
            if p._gamekee_batch is not None:
                break
            await asyncio.sleep(0.005)
        results2 = await asyncio.gather(task_a2, p._run_gamekee_probe("b"), return_exceptions=True)
        errs = [r for r in results2 if isinstance(r, plug.GameKeeProbeFailure)]
        check("V8 失败合并（等待者不重拉）",
              len(errs) == 2 and slow_fail.calls == 1 and slow_fail.build_calls == 0,
              f"errs={len(errs)} probe={slow_fail.calls} build={slow_fail.build_calls}")

        # V9 顺序两入口：各自真实探查+构建（不复用批次）
        seq = _VFakeIndex()
        seq.install(plug)
        p._gamekee_index = None
        p._gamekee_state.clear()
        check("V9-prep 批次已清空且后台任务不在飞",
              p._gamekee_batch is None
              and (p._gamekee_probe_task is None or p._gamekee_probe_task.done()),
              f"batch={p._gamekee_batch} task={p._gamekee_probe_task}")
        await p._run_gamekee_probe("m1")
        await p._run_gamekee_probe("m2")
        check("V9 顺序两入口各真实探查（probe2/build2）",
              seq.calls == 2 and seq.build_calls == 2,
              f"probe={seq.calls} build={seq.build_calls}")
    finally:
        plug.gamekee_build_index = orig_build

        # V14 六页无变化：只推进 last_verified 不重建（probe 轻量路径）
        nochg = _VFakeIndex(changed=False)
        nochg.install(plug)
        p._gamekee_index = {"built_at": 1, "pages": {}, "characters": [{"name": "旧", "element": "water", "note": "", "image_urls": ["//x/old.png"]}]}
        p._gamekee_state.clear()
        idx14 = await p._run_gamekee_probe("nochange")
        check("V14 无变化仅推进时间戳不重建（索引对象不变+build0）",
              idx14 is p._gamekee_index and nochg.calls == 1 and nochg.build_calls == 0
              and p._gamekee_state["last_verified"] > 0,
              f"probe={nochg.calls} build={nochg.build_calls}")

    # V10 卸载清理探查任务
    await p.on_unload()
    check("V10 卸载清空探查任务", p._gamekee_probe_task is None)

    # V11 图片下载契约：域名白名单
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "imgs"
        try:
            gkmod.download_image("//evil.example.com/x.png", cache)
            check("V11 非白名单域名拒绝", False)
        except gkmod.GameKeeError as exc:
            check("V11 非白名单域名拒绝", "白名单" in str(exc), str(exc)[:60])

        # V12 魔数校验：HTML 拦截页拒绝（裸 GET 567 的产物形态）
        html_resp = _FakeHTTPResponse(b"<!doctype html><html>blocked</html>", code=200)
        fake_opener = _FakeOpener(lambda req, timeout=10: html_resp)
        orig_opener = gkmod._build_opener
        gkmod._build_opener = lambda proxy: fake_opener
        try:
            try:
                gkmod.download_image("//cdnimg-v2.gamekee.com/a/x.png", cache)
                check("V12 HTML 拦截页拒绝", False)
            except gkmod.GameKeeError as exc:
                check("V12 HTML 拦截页拒绝", "非图片" in str(exc), str(exc)[:60])
        finally:
            gkmod._build_opener = orig_opener

        # V13 缓存写入与命中（mock 网络返回合法 webp）
        webp_bytes = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8
        webp_resp = _FakeHTTPResponse(webp_bytes, code=200)
        gkmod._build_opener = lambda proxy: _FakeOpener(lambda req, timeout=10: webp_resp)
        try:
            data1 = gkmod.download_image("//cdnimg-v2.gamekee.com/a/ok.png", cache, prefer_webp=True)
            data2 = gkmod.download_image("//cdnimg-v2.gamekee.com/a/ok.png", cache, prefer_webp=True)
            check("V13 图片缓存命中（字节一致）",
                  data1 == webp_bytes and data2 == webp_bytes and len(list(cache.iterdir())) == 1)
        finally:
            gkmod._build_opener = orig_opener


# ===== 汇总入口 =====

SECTIONS = {
    "G": ("how 一图流端到端", run_direct_send),
    "I": ("非阻塞探针", run_nonblocking),
    "J": ("工具参数描述", run_tool_query_desc),
    "K": ("手动更新指令", run_manual_update),
    "R": ("排行榜数据层", run_section_r),
    "S": ("排行榜命令插件链路", run_section_s),
    "T": ("Hook 前置过滤", run_hook_gate),
    "U": ("GameKee 解析与匹配", run_section_u),
    "V": ("GameKee 探查调度与图片下载", run_section_v),
}

# 执行顺序：B 最先（json.load 计数依赖首次触达），异步节统一在事件循环中跑
# （原 C 并入 D、原 Q 挂 N 节末尾）
ORDER = ["G", "I", "J", "K", "R", "S", "T", "U", "V"]
ASYNC_SECTIONS = {"G", "I", "K", "S", "T", "V"}


async def run_async_sections(keys: list) -> None:
    for key in keys:
        if key in ASYNC_SECTIONS:
            name, func = SECTIONS[key]
            print(f"\n=== 节 {key} {name} ===")
            await func()


def main() -> int:
    keys = [k.upper() for k in sys.argv[1:]] or ORDER
    invalid = [k for k in keys if k not in SECTIONS]
    if invalid:
        print(f"未知测试节: {invalid}，可选: {ORDER}")
        return 2

    sync_keys = [k for k in ORDER if k in keys and k not in ASYNC_SECTIONS]
    async_keys = [k for k in ORDER if k in keys and k in ASYNC_SECTIONS]

    for key in sync_keys:
        name, func = SECTIONS[key]
        print(f"\n=== 节 {key} {name} ===")
        func()

    if async_keys:
        asyncio.run(run_async_sections(async_keys))

    print()
    if failures:
        print(f"=== FAIL（{len(failures)} 项：{', '.join(failures)}）===")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
