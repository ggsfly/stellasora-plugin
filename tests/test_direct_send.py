#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接发送模式端到端验证（mock ctx.llm / ctx.send）。"""
import asyncio
import sys
import tempfile
from pathlib import Path

# 自定位插件根目录（对齐 test_dict.py 的写法），替代旧的硬编码绝对路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import plugin as plug
from maibot_sdk.context import PluginContext, PluginPaths


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
    def __init__(self, fail=False, hard_fail=False, answer="这是模拟生成的攻略成品回答，含官方中文术语：泷闪 1/10/1/1。"):
        self.fail = fail
        self.hard_fail = hard_fail
        self.answer = answer
        self.calls = []

    async def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.hard_fail:
            raise RuntimeError("RPC hard failure")
        if self.fail:
            return {"success": False, "response": "", "error": "mock LLM down"}
        return {"success": True, "response": self.answer}


class MockSend:
    def __init__(self):
        self.sent = []

    async def text(self, text, stream_id, **kwargs):
        self.sent.append((stream_id, text))
        return True


async def main():
    p = plug.create_plugin()
    # 缓存目录用运行时临时目录（CacheManager.__init__ 会自动 mkdir），避免硬编码路径
    cache = Path(tempfile.mkdtemp(prefix="stellasora_test_cache_"))
    ctx = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache, runtime_dir=cache),
    )
    p._set_context(ctx)
    p._plugin_config_instance = plug.StellaSoraConfig()
    p._plugin_config_instance.query.answer_cache_ttl = 0  # 用例 1-9 关注 LLM 加工与错误分支，禁用成品缓存避免跨用例串扰
    await p.on_load()

    # 模拟宿主全局配置（人格与表达风格，与 replyer 同源）
    ctx.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": ["阿麦"]},
            "personality": {
                "personality": "是一个大二女大学生，现在正在上网和群友聊天。",
                "reply_style": "你的风格平淡简短，可以参考贴吧的回复风格。",
            },
            "experimental": {"emotion_trait": "neutral"},
        }
    )

    mock_llm = MockLLM()
    mock_send = MockSend()
    ctx.llm = mock_llm
    ctx.send = mock_send

    # 1. 直接发送 + 人格注入：prompt 应含人格块，成品含"已发送"引导
    r1 = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_001")
    persona_injected = bool(mock_llm.calls) and "你的名字是麦麦" in mock_llm.calls[0]["prompt"] and "表达风格" in mock_llm.calls[0]["prompt"]
    slot_ok = bool(mock_llm.calls) and mock_llm.calls[0].get("model") == "utils"
    ok1 = (
        "已直接发送" in r1["content"]
        and "reply 工具" in r1["content"]
        and "wait 工具" in r1["content"]
        and len(mock_send.sent) == 1
        and mock_send.sent[0][0] == "stream_001"
    )
    print(f"1 how直接发送 -> 引导调wait禁用reply: {ok1} | 人格+表达风格已注入prompt: {persona_injected} | 任务槽位=utils: {slot_ok}")

    # 2. 超长不压缩直接发全文（direct_send_max_length 已删除）
    async def gen_long(prompt, **kwargs):
        return {"success": True, "response": "很长的回答" * 1000}
    ctx.llm = type("L", (), {"generate": staticmethod(gen_long)})()
    r2 = await p.handle_what(query="千都世", group_id="g1", stream_id="stream_002")
    last_text = mock_send.sent[-1][1]
    print(f"2 超长直接发全文 -> 长度={len(last_text)} 无压缩: {'已直接发送' in r2['content']}")

    # 3. LLM 软失败 -> 统一"未找到"
    ctx.llm = MockLLM(fail=True)
    n = len(mock_send.sent)
    r3 = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_003")
    print(f"3 LLM软失败 -> '未找到相关攻略。': {r3['content'] == '未找到相关攻略。'} | 未发送: {len(mock_send.sent) == n}")

    # 4. LLM 硬异常 -> 统一"未找到"
    ctx.llm = MockLLM(hard_fail=True)
    r4 = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_004")
    print(f"4 LLM硬异常 -> '未找到相关攻略。': {r4['content'] == '未找到相关攻略。'}")

    # 5. stream 缺失 -> 统一"未找到"
    ctx.llm = MockLLM()
    r5 = await p.handle_what(query="猫眼", group_id="g1", stream_id="")
    print(f"5 stream缺失 -> {r5['content'] == '未找到相关攻略。'}")

    # 6. 鉴权优先
    p._plugin_config_instance.access_control.mode = "whitelist"
    r6 = await p.handle_how(query="夏花", group_id="g999", stream_id="stream_006")
    print(f"6 白名单拒绝 -> {'允许范围' in r6['content']}")

    # 7. 模型解析三态（新解析链，移植自 smart-segmentation-plugin）
    from model_resolver import resolve_generation_model

    kind_task, name_task = resolve_generation_model("planner")
    kind_model, name_model = resolve_generation_model("gpt-4o")  # 未知值 → 默认模型
    print(f"7 任务名解析 -> task/{name_task}: {kind_task == 'task' and name_task == 'planner'}")
    print(f"7' 未知值回落默认 -> task/'': {kind_model == 'task' and name_model == ''}")

    # 8. 人格注入开关：关闭后 prompt 不含人格块
    p._plugin_config_instance.access_control.mode = "off"
    ctx.llm = mock_llm  # 恢复引用（用例 5 曾替换为失败实例）
    mock_llm.calls.clear()
    mock_llm.fail = False
    p._plugin_config_instance.query.inject_persona = False
    await p.handle_how(query="夏花", group_id="g1", stream_id="stream_008")
    no_persona = bool(mock_llm.calls) and "你的名字是麦麦" not in mock_llm.calls[-1]["prompt"]
    mock_llm.calls.clear()
    p._plugin_config_instance.query.inject_persona = True
    await p.handle_how(query="夏花", group_id="g1", stream_id="stream_009")
    with_persona = bool(mock_llm.calls) and "你的名字是麦麦" in mock_llm.calls[-1]["prompt"]
    print(f"8 人格开关 -> 关闭后无人格: {no_persona} | 开启后有人格: {with_persona}")

    # 9. 人格按模式分支：直发带人格，回传（联合查询）恒为客观体
    mock_llm.calls.clear()
    p._plugin_config_instance.query.direct_send = True
    p._plugin_config_instance.query.inject_persona = True
    sent_before = len(ctx.send.sent)
    await p.handle_how(query="夏花", group_id="g1", stream_id="stream_010")
    direct_persona = bool(mock_llm.calls) and "你的名字是麦麦" in mock_llm.calls[-1]["prompt"]
    direct_sent = len(ctx.send.sent) == sent_before + 1
    mock_llm.calls.clear()
    # 联合查询(>=2角色名)强制回传 -> 即使人格开启也不注入, 用客观体指令
    ret = await p.handle_how(query="夏花", question="夏花 小禾 谁的纹章好", group_id="g1", stream_id="stream_011")
    return_no_persona = bool(mock_llm.calls) and "你的名字是麦麦" not in mock_llm.calls[-1]["prompt"]
    return_objective = bool(mock_llm.calls) and "客观" in mock_llm.calls[-1]["prompt"]
    wrapped = bool(ret) and "系统说明" in str(ret.get("content", ""))
    not_sent = len(ctx.send.sent) == sent_before + 1  # 回传模式不新增直发
    print(f"9 人格按模式 -> 直发带人格: {direct_persona} | 直发已发送: {direct_sent} | 联合查询回传无人格: {return_no_persona} | 客观体指令: {return_objective} | 返回带系统包装: {wrapped} | 回传未直发: {not_sent}")

    # ===== Fix A 去重守卫测试（用例 10-14）=====

    # 10. 同流同 query，第二次调用在 dedup_window 内 → 被拦截
    # 重置插件状态
    p2 = plug.create_plugin()
    cache2 = Path(tempfile.mkdtemp(prefix="stellasora_test_dedup_"))
    ctx2 = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache2, runtime_dir=cache2),
    )
    p2._set_context(ctx2)
    p2._plugin_config_instance = plug.StellaSoraConfig()
    p2._plugin_config_instance.query.answer_cache_ttl = 0  # 用例 10-14 关注去重守卫，禁用成品缓存
    await p2.on_load()
    ctx2.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": []},
            "personality": {"personality": "是人类。", "reply_style": ""},
            "experimental": {"emotion_trait": "neutral"},
        }
    )
    mock_llm10 = MockLLM()
    mock_send10 = MockSend()
    ctx2.llm = mock_llm10
    ctx2.send = mock_send10
    # 第一次调用（应成功直发）
    r10a = await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup")
    first_ok = "已直接发送" in r10a.get("content", "")
    # 第二次立即调用（同流同 query，应被拦截）
    r10b = await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup")
    blocked = "勿重复发送" in r10b.get("content", "")
    llm_once = len(mock_llm10.calls) == 1
    send_once = len(mock_send10.sent) == 1
    print(f"10 同流同query去重拦截 -> 第一次成功: {first_ok} | 第二次被拦截: {blocked} | LLM仅调一次: {llm_once} | send仅一次: {send_once}")

    # 11. 不同 query，同流 → 不拦截
    mock_llm11 = MockLLM()
    mock_send11 = MockSend()
    ctx2.llm = mock_llm11
    ctx2.send = mock_send11
    p2._recent_direct.clear()
    await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup11")
    r11b = await p2.handle_how(query="猫眼", group_id="g1", stream_id="stream_dedup11")
    not_blocked11 = "已直接发送" in r11b.get("content", "")
    send_twice11 = len(mock_send11.sent) == 2
    print(f"11 不同query不拦截 -> 第二次正常通过: {not_blocked11} | send两次: {send_twice11}")

    # 12. 回传模式（≥2 角色名，direct=False）→ 不拦截
    mock_llm12 = MockLLM()
    mock_send12 = MockSend()
    ctx2.llm = mock_llm12
    ctx2.send = mock_send12
    p2._recent_direct.clear()
    # 首次用单角色触发直发，写入 dedup
    await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup12")
    # 第二次：联合查询 → direct=False → 不走守卫
    r12b = await p2.handle_how(
        query="夏花", question="夏花 小禾 谁好", group_id="g1", stream_id="stream_dedup12"
    )
    relay_mode12 = "系统说明" in r12b.get("content", "")
    print(f"12 回传模式不拦截 -> 返回攻略包装: {relay_mode12}")

    # 13. dedup_window 已过期 → 第二次不拦截
    import time as _time
    mock_llm13 = MockLLM()
    mock_send13 = MockSend()
    ctx2.llm = mock_llm13
    ctx2.send = mock_send13
    p2._recent_direct.clear()
    # 手动写入一个已过期的记录（时间戳往前推 200 秒，超过默认 dedup_window=60）
    p2._recent_direct[("stream_dedup13", "夏花")] = _time.time() - 200
    r13 = await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup13")
    not_blocked13 = "已直接发送" in r13.get("content", "")
    print(f"13 窗口过期不拦截 -> 第二次正常通过: {not_blocked13}")

    # 14. 第一次 send 失败（返回 False）→ 不登记 → 第二次重新尝试 LLM
    class MockSendFail:
        """第一次 send 返回 False，第二次正常。"""
        def __init__(self):
            self.sent = []
            self.call_count = 0

        async def text(self, text, stream_id, **kwargs):
            self.call_count += 1
            if self.call_count == 1:
                return False  # 第一次失败
            self.sent.append((stream_id, text))
            return True

    mock_llm14 = MockLLM()
    mock_send14 = MockSendFail()
    ctx2.llm = mock_llm14
    ctx2.send = mock_send14
    p2._recent_direct.clear()
    # 第一次：send 失败 → 应返回未找到，不写 dedup
    r14a = await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup14")
    first_fail = r14a.get("content") == "未找到相关攻略。"
    dedup_empty = ("stream_dedup14", "夏花") not in p2._recent_direct
    # 第二次：send 恢复正常 → 应走完整流程（LLM 再调一次）
    r14b = await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup14")
    second_ok = "已直接发送" in r14b.get("content", "")
    llm_twice14 = len(mock_llm14.calls) == 2
    print(f"14 send失败不登记→第二次重试 -> 第一次失败: {first_fail} | 未登记dedup: {dedup_empty} | 第二次成功: {second_ok} | LLM调两次: {llm_twice14}")

    # ===== Fix B 直发成品缓存测试（用例 15-18）=====

    # 15. 同参数第二次调用命中缓存：LLM 仅调 1 次，send 调 2 次，两次 answer 相同
    p15 = plug.create_plugin()
    cache15 = Path(tempfile.mkdtemp(prefix="stellasora_test_cache15_"))
    ctx15 = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache15, runtime_dir=cache15),
    )
    p15._set_context(ctx15)
    p15._plugin_config_instance = plug.StellaSoraConfig()
    p15._plugin_config_instance.query.dedup_window = 0  # 禁用去重守卫以测试成品缓存
    await p15.on_load()
    ctx15.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": []},
            "personality": {"personality": "是人类。", "reply_style": ""},
            "experimental": {"emotion_trait": "neutral"},
        }
    )
    mock_llm15 = MockLLM(answer="夏花纹章推荐成品攻略")
    mock_send15 = MockSend()
    ctx15.llm = mock_llm15
    ctx15.send = mock_send15

    r15a = await p15.handle_how(query="夏花", group_id="g1", stream_id="stream_cache15")
    r15b = await p15.handle_how(query="夏花", group_id="g1", stream_id="stream_cache15")
    c15_llm_once = len(mock_llm15.calls) == 1
    c15_send_twice = len(mock_send15.sent) == 2
    c15_same_ans = (
        len(mock_send15.sent) == 2
        and mock_send15.sent[0][1] == mock_send15.sent[1][1] == "夏花纹章推荐成品攻略"
    )
    c15_ok = "已直接发送" in r15b.get("content", "")
    assert c15_llm_once and c15_send_twice and c15_same_ans and c15_ok
    print(f"15 直发缓存命中 -> LLM仅调一次: {c15_llm_once} | send调用两次: {c15_send_twice} | 两次内容相同: {c15_same_ans} | 返回成功: {c15_ok}")

    # 16. on_config_update 清空缓存后，再次调用重新触发 LLM 生成（调用计数变为 2）
    await p15.on_config_update(scope="query", config_data={}, version="1.2.3")
    r16 = await p15.handle_how(query="夏花", group_id="g1", stream_id="stream_cache15")
    c16_llm_twice = len(mock_llm15.calls) == 2
    c16_send_3 = len(mock_send15.sent) == 3
    assert c16_llm_twice and c16_send_3
    print(f"16 配置更新清空缓存 -> LLM重新生成(计数=2): {c16_llm_twice} | send计数=3: {c16_send_3}")

    # 17. answer_cache_ttl = 0 禁用缓存：每次调用均触发 LLM，answers 目录不写文件
    p17 = plug.create_plugin()
    cache17 = Path(tempfile.mkdtemp(prefix="stellasora_test_cache17_"))
    ctx17 = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache17, runtime_dir=cache17),
    )
    p17._set_context(ctx17)
    p17._plugin_config_instance = plug.StellaSoraConfig()
    p17._plugin_config_instance.query.dedup_window = 0
    p17._plugin_config_instance.query.answer_cache_ttl = 0  # 禁用成品缓存
    await p17.on_load()
    ctx17.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": []},
            "personality": {"personality": "是人类。", "reply_style": ""},
            "experimental": {"emotion_trait": "neutral"},
        }
    )
    mock_llm17 = MockLLM()
    mock_send17 = MockSend()
    ctx17.llm = mock_llm17
    ctx17.send = mock_send17

    await p17.handle_how(query="夏花", group_id="g1", stream_id="stream_cache17")
    await p17.handle_how(query="夏花", group_id="g1", stream_id="stream_cache17")
    c17_llm_twice = len(mock_llm17.calls) == 2
    answers_dir17 = Path(ctx17.paths.runtime_dir) / "webcache" / "answers"
    answers_files = list(answers_dir17.glob("*.json")) if answers_dir17.exists() else []
    c17_no_files = len(answers_files) == 0
    assert c17_llm_twice and c17_no_files
    print(f"17 TTL=0禁用缓存 -> LLM调用两次: {c17_llm_twice} | answers目录无文件: {c17_no_files}")

    # 18. presets=True 与 presets=False 在相同 query 下拥有不同缓存键，互不命中
    p18 = plug.create_plugin()
    cache18 = Path(tempfile.mkdtemp(prefix="stellasora_test_cache18_"))
    ctx18 = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache18, runtime_dir=cache18),
    )
    p18._set_context(ctx18)
    p18._plugin_config_instance = plug.StellaSoraConfig()
    p18._plugin_config_instance.query.dedup_window = 0
    await p18.on_load()
    ctx18.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": []},
            "personality": {"personality": "是人类。", "reply_style": ""},
            "experimental": {"emotion_trait": "neutral"},
        }
    )
    mock_llm18 = MockLLM()
    mock_send18 = MockSend()
    ctx18.llm = mock_llm18
    ctx18.send = mock_send18

    # 先不带 presets 查询
    await p18.handle_how(query="夏花", presets=False, group_id="g1", stream_id="stream_cache18")
    c18_first_call = len(mock_llm18.calls) == 1
    # 带 presets 查询（虽然 query 相同，但 presets 不同，应重新触发 LLM 生成）
    await p18.handle_how(query="夏花", presets=True, group_id="g1", stream_id="stream_cache18")
    c18_second_call = len(mock_llm18.calls) == 2
    # 再次带 presets=True 查询，应该命中 presets=True 的缓存
    await p18.handle_how(query="夏花", presets=True, group_id="g1", stream_id="stream_cache18")
    c18_third_cached = len(mock_llm18.calls) == 2
    c18_send_3 = len(mock_send18.sent) == 3
    assert c18_first_call and c18_second_call and c18_third_cached and c18_send_3
    print(f"18 presets独立缓存键 -> 无预设首次LLM: {c18_first_call} | 有预设二次LLM(互不命中): {c18_second_call} | 有预设三次命中缓存: {c18_third_cached} | send共3次: {c18_send_3}")

    # ===== Fix I 游戏知识文档注入测试（用例 19）=====
    # 19. 验证直发 prompt 注入游戏机制知识：
    #   (a) 默认 inject_knowledge=True：prompt 含知识标头与关键规范
    #   (b) inject_knowledge=False：prompt 不含知识标头
    #   (c) 知识文档缺失/返回空串时：不崩且 prompt 不含知识标头
    p19 = plug.create_plugin()
    cache19 = Path(tempfile.mkdtemp(prefix="stellasora_test_cache19_"))
    ctx19 = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache19, runtime_dir=cache19),
    )
    p19._set_context(ctx19)
    p19._plugin_config_instance = plug.StellaSoraConfig()
    p19._plugin_config_instance.query.dedup_window = 0
    p19._plugin_config_instance.query.answer_cache_ttl = 0
    await p19.on_load()
    ctx19.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": []},
            "personality": {"personality": "是人类。", "reply_style": ""},
            "experimental": {"emotion_trait": "neutral"},
        }
    )
    mock_llm19 = MockLLM()
    mock_send19 = MockSend()
    ctx19.llm = mock_llm19
    ctx19.send = mock_send19

    # (a) 默认 inject_knowledge=True：prompt 含标头与规范
    r19a = await p19.handle_how(query="夏花", group_id="g1", stream_id="stream_k19a")
    assert len(mock_llm19.calls) == 1
    prompt_19a = mock_llm19.calls[0]["prompt"]
    c19a_has_header = "【游戏机制知识（回答格式必须遵守）】" in prompt_19a
    c19a_has_content = "70级：词条" in prompt_19a and "绿<蓝<金<彩" in prompt_19a
    assert c19a_has_header and c19a_has_content

    # (b) inject_knowledge=False：prompt 不含标头
    p19._plugin_config_instance.query.inject_knowledge = False
    mock_llm19.calls.clear()
    r19b = await p19.handle_how(query="夏花", group_id="g1", stream_id="stream_k19b")
    assert len(mock_llm19.calls) == 1
    prompt_19b = mock_llm19.calls[0]["prompt"]
    c19b_no_header = "【游戏机制知识（回答格式必须遵守）】" not in prompt_19b
    assert c19b_no_header

    # (c) 知识文档为空/缺失时：不崩且不含标头
    p19._plugin_config_instance.query.inject_knowledge = True
    orig_loader = plug._load_game_knowledge
    try:
        plug._load_game_knowledge = lambda: ""
        mock_llm19.calls.clear()
        r19c = await p19.handle_how(query="夏花", group_id="g1", stream_id="stream_k19c")
        assert len(mock_llm19.calls) == 1
        prompt_19c = mock_llm19.calls[0]["prompt"]
        c19c_no_header = "【游戏机制知识（回答格式必须遵守）】" not in prompt_19c
        c19c_ok = "已直接发送" in r19c.get("content", "")
        assert c19c_no_header and c19c_ok
    finally:
        plug._load_game_knowledge = orig_loader

    print(f"19 游戏知识注入 -> (a)默认注入: {c19a_has_header and c19a_has_content} | (b)关闭不注入: {c19b_no_header} | (c)缺失降级不崩: {c19c_no_header and c19c_ok}")

    print()
    print("=== 直接发送模式端到端全部通过 ===")


asyncio.run(main())
