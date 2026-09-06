#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F3 输出格式验证测试（agent 可执行 + 自愈式 strip_markdown）

测试项：
  Test 1: 验证 direct-send prompt 规则 9 禁止 markdown。Mock ctx.llm.generate 返回纯文本，
          检查 ctx.send.text 收到的纯文本不含 '**', '###', '`'。
  Test 2: Mock ctx.llm.generate 返回含 markdown 的文本（'**加粗**', '### 标题', '`代码`'），
          检查 ctx.send.text 及缓存命中时收到的文本是否已去除 markdown。
          若未过滤则报错促使自愈式补全 strip_markdown。
  Test 3: 以'赤霞攻略'为题，mock fetcher 返回含 Chaton 段的 infodoc + 索引文本，
          调用 handle_how，断言传给 ctx.llm.generate 的 prompt 含输出规则关键词：
          队伍阵容, 主控位, 秘纹, 纹章, 默认不给出。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import plugin as plug
import service
from maibot_sdk.context import PluginContext, PluginPaths


class MockConfig:
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
    def __init__(self, answer="这是模拟纯文本回答。"):
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


async def test_1_prompt_rule_and_plain_output():
    """Test 1: 规则 9 禁止 markdown 且纯文本正常发送"""
    p = plug.create_plugin()
    cache = Path(tempfile.mkdtemp(prefix="stellasora_f3_t1_"))
    ctx = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache, runtime_dir=cache),
    )
    p._set_context(ctx)
    p._plugin_config_instance = plug.StellaSoraConfig()
    p._plugin_config_instance.query.answer_cache_ttl = 0
    p._plugin_config_instance.query.dedup_window = 0
    await p.on_load()

    mock_llm = MockLLM(answer="夏花是风属性角色，队伍搭配以风系为主。推荐秘纹：风之眼。纹章推荐：穿透与暴击。")
    mock_send = MockSend()
    ctx.llm = mock_llm
    ctx.send = mock_send

    res = await p.handle_how(query="夏花", question="夏花攻略", group_id="g1", stream_id="s1")
    assert "已直接发送" in res["content"], f"直发失败: {res}"
    assert len(mock_llm.calls) == 1, f"LLM 调用次数异常: {len(mock_llm.calls)}"
    prompt = mock_llm.calls[0]["prompt"]
    assert "不要使用任何 markdown 格式" in prompt, "Prompt 缺少规则 9 反 markdown 指令"

    assert len(mock_send.sent) == 1, "未通过 ctx.send.text 发送消息"
    sent_text = mock_send.sent[0][1]
    assert "**" not in sent_text, f"输出含有 '**': {sent_text}"
    assert "###" not in sent_text, f"输出含有 '###': {sent_text}"
    assert "`" not in sent_text, f"输出含有 '`': {sent_text}"
    print("PASS: Test 1 (Prompt rule 9 + plain text output)")


async def test_2_markdown_leak_and_strip():
    """Test 2: 当 LLM 返回 markdown 格式时，验证是否被清洗剥离"""
    p = plug.create_plugin()
    cache = Path(tempfile.mkdtemp(prefix="stellasora_f3_t2_"))
    ctx = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache, runtime_dir=cache),
    )
    p._set_context(ctx)
    p._plugin_config_instance = plug.StellaSoraConfig()
    p._plugin_config_instance.query.answer_cache_ttl = 3600  # 测试缓存路径
    p._plugin_config_instance.query.dedup_window = 0
    await p.on_load()

    markdown_response = (
        "**夏花攻略**\n"
        "### 推荐阵容\n"
        "主控位：夏花，支援位：猫眼。\n"
        "- 秘纹推荐：`风之眼`、`狂风呼啸`\n"
        "| 纹章 | 词条 |\n"
        "| 三角形 | 风系穿透 |\n"
    )

    mock_llm = MockLLM(answer=markdown_response)
    mock_send = MockSend()
    ctx.llm = mock_llm
    ctx.send = mock_send

    # 1. 首次调用（新鲜结果路径）
    res1 = await p.handle_how(query="夏花", question="夏花怎么玩", group_id="g1", stream_id="s1")
    assert "已直接发送" in res1["content"], f"首次直发失败: {res1}"
    assert len(mock_send.sent) == 1, "首次未发送消息"
    sent_text_1 = mock_send.sent[0][1]

    # 断言 markdown 符号已被清洗
    assert "**" not in sent_text_1, f"新鲜结果含有 '**': {sent_text_1}"
    assert "###" not in sent_text_1, f"新鲜结果含有 '###': {sent_text_1}"
    assert "`" not in sent_text_1, f"新鲜结果含有 '`': {sent_text_1}"

    # 2. 二次调用（缓存命中路径）
    res2 = await p.handle_how(query="夏花", question="夏花怎么玩", group_id="g1", stream_id="s2")
    assert "已直接发送" in res2["content"], f"缓存命中直发失败: {res2}"
    assert len(mock_send.sent) == 2, "缓存命中未发送消息"
    sent_text_2 = mock_send.sent[1][1]

    assert "**" not in sent_text_2, f"缓存结果含有 '**': {sent_text_2}"
    assert "###" not in sent_text_2, f"缓存结果含有 '###': {sent_text_2}"
    assert "`" not in sent_text_2, f"缓存结果含有 '`': {sent_text_2}"

    print("PASS: Test 2 (Markdown stripping on fresh and cached send)")


async def test_3_chixia_guide_keywords():
    """Test 3: '赤霞攻略' mock fetcher 返回含 Chaton 段 infodoc + 索引文本，
    断言 prompt 含输出规则关键词: 队伍阵容, 主控位, 秘纹, 纹章, 默认不给出
    """
    p = plug.create_plugin()
    cache = Path(tempfile.mkdtemp(prefix="stellasora_f3_t3_"))
    ctx = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache, runtime_dir=cache),
    )
    p._set_context(ctx)
    p._plugin_config_instance = plug.StellaSoraConfig()
    p._plugin_config_instance.query.answer_cache_ttl = 0
    p._plugin_config_instance.query.dedup_window = 0
    p._plugin_config_instance.query.inject_knowledge = True
    await p.on_load()

    mock_llm = MockLLM(answer="赤霞攻略纯文本输出")
    mock_send = MockSend()
    ctx.llm = mock_llm
    ctx.send = mock_send

    # Mock fetcher 方法
    mock_index = "Chaton (Ignis) | Rotation: E > Q > R | Chaton (Main Slot) | Flora (1st Supp. Slot)"
    mock_detailed = (
        "Chaton (Ignis) | Main Slot | Recommended Main Discs: DiscA (C1), DiscB\n"
        "Emblem: Triangle | Ignis PEN\n"
        "Priority Potentials: Mark of Flame +3\n"
    )

    orig_fetch_index = service.StelladbFetcher.fetch_infodoc_index
    orig_fetch_infodoc = service.StelladbFetcher.fetch_infodoc
    orig_fetch_trekker = service.StelladbFetcher.fetch_trekker

    try:
        service.StelladbFetcher.fetch_infodoc_index = lambda self: mock_index
        service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_detailed
        service.StelladbFetcher.fetch_trekker = lambda self, num_id: "Ignis character data with Ignis element"

        res = await p.handle_how(query="赤霞", question="赤霞攻略", group_id="g1", stream_id="s1")
        assert "已直接发送" in res["content"], f"直发失败: {res}"
        assert len(mock_llm.calls) == 1, "LLM 未调用"

        prompt = mock_llm.calls[0]["prompt"]
        keywords = ["队伍阵容", "主控位", "秘纹", "纹章", "默认不给出"]
        missing = [kw for kw in keywords if kw not in prompt]
        assert not missing, f"Prompt 缺少关键词: {missing}\nPrompt内容片断: {prompt[:500]}"
        print(f"PASS: Test 3 (Chixia guide prompt contains all keywords: {keywords})")
    finally:
        service.StelladbFetcher.fetch_infodoc_index = orig_fetch_index
        service.StelladbFetcher.fetch_infodoc = orig_fetch_infodoc
        service.StelladbFetcher.fetch_trekker = orig_fetch_trekker


async def main():
    print("=== Running F3 Output Format Tests ===")
    await test_1_prompt_rule_and_plain_output()
    await test_2_markdown_leak_and_strip()
    await test_3_chixia_guide_keywords()
    print("=== All F3 Output Format Tests PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
