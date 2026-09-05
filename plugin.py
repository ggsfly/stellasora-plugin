"""星塔旅人（Stella Sora）攻略查询插件

将三个查询工具包装为 MaiBot Tool，由 planner 自动路由：
  - stellasora_what    "是什么"：角色属性/技能/素材/礼物
  - stellasora_how     "怎么玩"：配队/纹章/秘纹/技能升级优先度（预设码按需）
  - lookup_game_term   查词：游戏术语 → 官方中文名 + 游戏内 ID

安全：白名单/黑名单模式可配置（config.toml），群聊按群号、私聊按用户号鉴权。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParamType, ToolParameterInfo

MAX_DEDUP_ENTRIES = 2000  # 去重记录硬上界，防多群场景内存线性增长（【双审 SH-4】）

# 让插件可以导入 tools/ 下的模块
_TOOLS_DIR = Path(__file__).resolve().parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from cache import CacheManager  # noqa: E402
from model_resolver import generate_with_pinned_model, resolve_generation_model  # noqa: E402
from service import (  # noqa: E402
    check_permission,
    configure_overrides,
    count_character_names,
    lookup_term,
    query_how,
    query_what,
)

_GAME_KNOWLEDGE_CACHE: Optional[str] = None


def _load_game_knowledge() -> str:
    """加载 docs/game_knowledge.md 游戏机制知识文档（模块级缓存）。

    知识文档是增强项，缺失或读取失败返回空串，保证流程不崩。
    """
    global _GAME_KNOWLEDGE_CACHE
    if _GAME_KNOWLEDGE_CACHE is None:
        try:
            doc_path = Path(__file__).resolve().parent / "docs" / "game_knowledge.md"
            if doc_path.is_file():
                _GAME_KNOWLEDGE_CACHE = doc_path.read_text(encoding="utf-8")
            else:
                _GAME_KNOWLEDGE_CACHE = ""
        except Exception:
            _GAME_KNOWLEDGE_CACHE = ""
    return _GAME_KNOWLEDGE_CACHE


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（Runner 强制要求 plugin.config_version）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class AccessControlConfig(PluginConfigBase):
    """访问控制配置。"""

    __ui_label__ = "访问控制"
    __ui_icon__ = "shield"
    __ui_order__ = 0

    mode: str = Field(
        default="off",
        description="鉴权模式：whitelist=仅白名单可用；blacklist=黑名单内禁用；off=不限制",
    )
    whitelist: list[str] = Field(
        default_factory=list,
        description="白名单（群号或用户号，每行一个）",
    )
    blacklist: list[str] = Field(
        default_factory=list,
        description="黑名单（群号或用户号，每行一个）",
    )


class QueryConfig(PluginConfigBase):
    """查询行为配置。"""

    __ui_label__ = "查询设置"
    __ui_icon__ = "search"
    __ui_order__ = 1

    default_max_length: int = Field(
        default=40000,
        description="工具返回文本的最大长度（超出按行边界截断并标注，防止撑爆 LLM 上下文；"
        "how 路径含元素队 infodoc 全文，完整攻略需较大预算）",
    )

    direct_send: bool = Field(
        default=True,
        description="直接发送模式：插件内部用 LLM 把攻略加工成中文成品后直接发送到聊天，"
        "工具只向 planner 返回'已发送'。关闭则退回旧行为（攻略原文返回给 planner 翻译）",
    )

    dedup_window: int = Field(
        default=60,
        description="同流同主题直发去重窗口（秒）：同一 stream_id + query 在此时间内重复调用直接拦截，"
        "防止 what+how 双直发刷屏（Fix A，【Metis 修订 #7/#8】）",
    )

    answer_cache_ttl: int = Field(
        default=86400,
        description="直发成品缓存时长（秒），0=禁用",
    )

    llm_model: str = Field(
        default="utils",
        description="直接发送模式使用的模型；推荐使用 utils（快速响应 2-4s，术语翻译已在代码中完成）；"
        "填写任务名（utils/replyer/planner 等）、模型名或模型标识，留空使用默认模型",
    )

    inject_persona: bool = Field(
        default=True,
        description="直发模式注入 bot 人格与表达风格（读取主程序人格配置，"
        "使成品回答与 bot 口吻一致）；关闭则使用无人格的攻略助手口吻。"
        "回传模式（direct_send=false）恒为客观攻略体，不受此项影响",
    )

    inject_knowledge: bool = Field(
        default=True,
        description="直发模式注入 docs/game_knowledge.md 游戏机制知识（纹章推荐输出格式等）；关闭则不注入",
    )


class OverridesConfig(PluginConfigBase):
    """自定义覆盖与别名配置。"""

    __ui_label__ = "自定义覆盖"
    __ui_icon__ = "edit"
    __ui_order__ = 2

    aliases: dict[str, str] = Field(
        default_factory=lambda: {"土": "地", "花玲": "花铃"},
        description="别名/俗称/上游笔误映射：将俗称或输入词映射为官方中文名、英文名或词条ID。"
        "例如：{\"土\": \"地\", \"花玲\": \"花铃\"}",
    )

    replacements: dict[str, str] = Field(
        default_factory=lambda: {"Finale Echoing": "终焉绝响"},
        description="攻略文本替换规则：直接将攻略资料中的英文短语或笔误替换为指定中文文本。"
        "例如：{\"Finale Echoing\": \"终焉绝响\"}",
    )


class StellaSoraConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    access_control: AccessControlConfig = Field(default_factory=AccessControlConfig)
    query: QueryConfig = Field(default_factory=QueryConfig)
    overrides: OverridesConfig = Field(default_factory=OverridesConfig)


class StellaSoraPlugin(MaiBotPlugin):
    """星塔旅人攻略查询插件。"""

    config_model = StellaSoraConfig

    def __init__(self) -> None:
        super().__init__()
        self._cache_dir: Path | None = None
        self._answer_cache: CacheManager | None = None
        self._recent_direct: dict[tuple[str, str], float] = {}  # (stream_id, query) → 直发成功时间戳

    # ===== 生命周期 =====

    def _apply_overrides_config(self) -> None:
        """将 config 中的 [overrides] 自定义别名与替换应用到运行时服务层。"""
        try:
            overrides = getattr(self.config, "overrides", None)
            aliases = overrides.aliases if overrides else {}
            replacements = overrides.replacements if overrides else {}
            configure_overrides(aliases=aliases, replacements=replacements)
        except Exception as exc:
            self.ctx.logger.warning("应用 overrides 配置失败: %s", exc)

    async def on_load(self) -> None:
        """插件加载：准备运行时缓存目录并同步 overrides 配置。

        字典缓存放 runtime_dir（非持久，可随时重建）；
        字典本体在插件包内 data/（只读）。
        """
        self._cache_dir = Path(self.ctx.paths.runtime_dir) / "webcache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._apply_overrides_config()
        self.ctx.logger.info("星塔旅人插件已加载，缓存目录: %s", self._cache_dir)

    async def on_unload(self) -> None:
        """插件卸载：清空运行时缓存引用（文件留给磁盘回收）。"""
        self._cache_dir = None
        self._answer_cache = None
        self.ctx.logger.info("星塔旅人插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        """配置热重载：黑白名单、overrides 与查询参数即时生效，无需重启。"""
        self.ctx.logger.info(
            "配置已更新: scope=%s version=%s（黑白名单与自定义覆盖即时生效）", scope, version
        )
        self._apply_overrides_config()
        # 清空直发成品缓存（防旧配置答案残留）
        answers_dir = self._cache_dir_ready() / "answers"
        if answers_dir.exists():
            for p in answers_dir.glob("*.json"):
                try:
                    p.unlink()
                except Exception:
                    pass
        if self._answer_cache is not None:
            self._answer_cache._memory_cache.clear()
        self.ctx.logger.info("直发成品缓存已清空")

    # ===== 内部工具 =====

    def _cache_dir_ready(self) -> Path:
        if self._cache_dir is None:
            self._cache_dir = Path(self.ctx.paths.runtime_dir) / "webcache"
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def _get_answer_cache(self) -> CacheManager:
        """获取直发成品缓存管理器（懒创建并复用，同步当前 TTL 配置）。

        线程说明：fetcher 的 CacheManager 在 Todo 4 之后被线程池并发共享，
        GIL 下 dict 读写原子、最坏后果是重复抓取/损坏文件跳过重抓，良性；
        答案缓存仅在事件循环侧访问，无此问题。
        """
        ttl = int(self.config.query.answer_cache_ttl)
        expected_dir = self._cache_dir_ready() / "answers"
        if self._answer_cache is None or self._answer_cache.cache_dir != expected_dir:
            self._answer_cache = CacheManager(expected_dir, ttl_seconds=ttl)
        else:
            self._answer_cache.ttl_seconds = ttl
        return self._answer_cache

    def _resolve_stream_id(self, kwargs: dict) -> str:
        """从工具调用 kwargs 提取 stream_id（复用 _direct_send 现有逻辑）。"""
        return str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")

    def _denied(self, **kwargs) -> bool:
        """黑白名单鉴权。群聊看 group_id，私聊看 user_id。"""
        cfg = self.config.access_control
        return not check_permission(
            cfg.mode,
            [str(x).strip() for x in (cfg.whitelist or [])],
            [str(x).strip() for x in (cfg.blacklist or [])],
            group_id=str(kwargs.get("group_id") or ""),
            user_id=str(kwargs.get("user_id") or ""),
        )

    # ===== 直接发送模式 =====

    # 说明：游戏机制知识与输出格式规范的单一事实源为 docs/game_knowledge.md（{knowledge_block}），本 Prompt 仅保留回答行为约束；本 Prompt 为插件内嵌模板，非 prompts/ 目录模板，不受多语言同步约束。
    _DIRECT_SEND_PROMPT = (
        "{persona_block}"
        "{knowledge_block}"
        "你是星塔旅人（Stella Sora）游戏攻略助手。用户在 QQ 群里问了下面这个问题，"
        "下面还附有一份由攻略站抓取的、已替换为官方中文译名的原始攻略资料。\n"
        "你的任务：\n"
        "1. 只依据资料回答用户问的问题，资料里与问题无关的条目不要列出\n"
        "2. 按上面的人格与表达风格用简体中文自然口语输出，面向 QQ 群聊场景，直接给出答案本身\n"
        "3. 数值、等级必须与资料完全一致\n"
        "4. 回复长度由问题决定：问题含「完整/全部/所有/详细」，或询问某角色/玩法的资料、"
        "攻略全貌时，必须分区块全量列举资料内容（属性、技能、潜能、天赋、消耗、喜好等各区块都要），"
        "不限字数；否则只回答所问要点，不超过 300 字"
        "；知识文档标注\"必须完整给出\"的内容不受 300 字上限约束\n"
        "5. 不要输出「根据攻略」「查到如下」之类的元描述，不要加开场白和结束语\n"
        "6. 预设码保持原样\n"
        "7. 输出中不留英文：资料里残余的英文单词和句子（地名、专有名词、描述等）"
        "一律译为自然的简体中文；译名拿不准时用中文意译，不要原样保留英文\n"
        "8. 【资料不足】的判定只看资料与问题是否**完全无关**：只要资料里有任何与问题"
        "相关的内容（哪怕缺少细节分支、数值收益），都必须直接输出这些内容作为答案，"
        "**绝不允许**在答案前面加上【资料不足】前缀；"
        "缺少的细节（如分支好感度）用一句话注明即可，不要影响正常回答\n\n"
        "【用户问题】\n{question}\n\n"
        "【攻略资料】\n{material}"
    )

    async def _config_get_value(self, key: str, default: Any) -> Any:
        """读取宿主全局配置值（Host 返回 {success, value} 结构，解包 value）。"""
        result = await self.ctx.config.get(key, default)
        if isinstance(result, dict) and "value" in result:
            return result.get("value") if result.get("success") else default
        return result

    async def _build_persona_block(self) -> str:
        """构建与 replyer 同源的人格与表达风格块。

        读取路径与主程序 _build_personality_prompt / _select_reply_style 一致：
        personality.personality / bot.nickname / bot.alias_names / personality.reply_style /
        experimental.emotion_trait
        """
        try:
            bot_name = str(await self._config_get_value("bot.nickname", "") or "").strip()
            alias_names = await self._config_get_value("bot.alias_names", []) or []
            personality = str(
                await self._config_get_value("personality.personality", "") or ""
            ).strip()
            reply_style = str(
                await self._config_get_value("personality.reply_style", "") or ""
            ).strip()
            emotion_trait = str(
                await self._config_get_value("experimental.emotion_trait", "") or ""
            ).strip()

            if not bot_name and not personality:
                return ""

            bot_aliases = (
                f"，也有人叫你{','.join(str(a) for a in alias_names)}" if alias_names else ""
            )
            lines = [f"【你的身份与人格】", f"你的名字是{bot_name or '麦麦'}{bot_aliases}。"]
            lines.append(personality or "是人类。")
            if reply_style:
                lines.append(f"【表达风格】\n{reply_style}")
            if emotion_trait:
                # 与主程序 PERSONALITY_EMOTION_SUFFIXES 保持一致的本地副本
                # （不直接 import src.*，保证插件在 Runner 沙箱内的独立性）
                emotion_suffixes = {
                    "rational_calm": "你在对话中保持理性冷静，情绪波动小，即使遇到有趣的事也只会淡淡回应。",
                    "neutral": "你情绪平稳，表达自然，偶尔流露出真实情绪。",
                    "sentimental": "你情感丰富细腻，容易共情，表达中带着真实的喜怒哀乐。",
                }
                suffix = emotion_suffixes.get(emotion_trait)
                if suffix:
                    lines.append(suffix)
            return "\n".join(lines) + "\n\n"
        except Exception as exc:
            self.ctx.logger.warning("构建人格块失败，将使用无人格模式: %s", exc)
            return ""

    async def _direct_send(
        self,
        *,
        tool_name: str,
        question: str,
        material: str,
        direct: bool = True,
        presets: bool = False,
        query: str = "",
        **kwargs,
    ) -> dict:
        """攻略加工：LLM 把攻略资料加工成中文成品。

        direct=True（默认）：加工后 ctx.send 直发聊天，返回"已发送"。
          加工 prompt 注入 bot 人格——成品即最终回复，需要与 bot 口吻一致。
        direct=False：加工后返回 LLM 成品给 planner，由 replyer 组织回复。
          加工 prompt **不注入人格**——中间产物保持客观攻略体，人格由
          replyer 统一注入；否则人格化语气会在 replyer 历史渲染（无归属
          纯文本）中被误归属为用户发言（bot 与用户同名时必现）。

        失败语义：任何失败（LLM 失败/目标缺失）返回"未找到相关攻略。"。
        """
        not_found = {"name": tool_name, "content": "未找到相关攻略。"}
        question = (question or "").strip()
        if not question:
            self.ctx.logger.warning("直接发送模式缺少用户问题，返回未找到")
            return not_found

        stream_id = (
            str(kwargs.get("stream_id") or "") or str(kwargs.get("chat_id") or "")
        ).strip()
        if direct and not stream_id:
            self.ctx.logger.warning("直接发送模式缺少 stream_id，无法确定发送目标")
            return not_found

        # 直发成品缓存查取（Fix B，【Metis 修订 #1/#12】）：
        # 仅对 direct=True 生效（回传模式为中间产物，不缓存）；
        # TTL <= 0 时显式跳过 get/set
        cache_key = ""
        if direct and self.config.query.answer_cache_ttl > 0:
            inject_persona = self.config.query.inject_persona
            cache_key = f"{tool_name}|{query}|{question}|{presets}|{self.config.query.llm_model}|{self.config.plugin.config_version}|{inject_persona}|{self.config.query.inject_knowledge}"
            cache = self._get_answer_cache()
            cached_answer = cache.get(cache_key)
            if cached_answer:
                self.ctx.logger.info("直接发送缓存命中: key=%s", cache_key)
                try:
                    sent = await self.ctx.send.text(cached_answer, stream_id)
                except Exception as exc:
                    self.ctx.logger.exception("直接发送模式消息发送异常")
                    _ = exc
                    return not_found
                if not sent:
                    self.ctx.logger.error("直接发送模式消息发送失败: stream=%s", stream_id)
                    return not_found
                # 发送成功即登记去重守卫（【双审 SH-1】保证重复可拦截）
                if query and stream_id:
                    self._recent_direct[(stream_id, query)] = time.time()
                    self.ctx.logger.info("直发去重登记: key=%s", (stream_id, query))
                self.ctx.logger.info(
                    "直接发送完成(缓存): %s -> stream=%s, 长度=%d",
                    tool_name,
                    stream_id,
                    len(cached_answer),
                )
                return {
                    "name": tool_name,
                    "content": (
                        "攻略内容已直接发送到聊天，用户已经可以看到完整答案。"
                        "你不需要也不应该再调用 reply 工具——reply 的回复内容会与已发送的攻略重复。"
                        "请立即调用 wait 工具（seconds=5）结束本轮即可。"
                    ),
                }
            else:
                self.ctx.logger.info("直接发送缓存未命中: key=%s", cache_key)

        # 人格注入只发生在直发模式：回传模式的成品是中间产物，
        # 客观攻略体避免 replyer 归属混乱（人格由 replyer 统一负责）
        if self.config.query.inject_persona and direct:
            persona_block = await self._build_persona_block()
        elif not direct:
            persona_block = (
                "【输出要求】以下是转交给主回复流程的攻略材料，"
                "请用客观、清晰、条理分明的攻略体输出，"
                "不要使用任何人格语气或傲娇卖萌措辞。\n\n"
            )
        else:
            persona_block = ""
        knowledge_content = (
            _load_game_knowledge() if self.config.query.inject_knowledge else ""
        )
        knowledge_block = (
            f"【游戏机制知识（回答格式必须遵守）】\n{knowledge_content}\n\n"
            if knowledge_content
            else ""
        )
        prompt = self._DIRECT_SEND_PROMPT.format(
            persona_block=persona_block,
            knowledge_block=knowledge_block,
            question=question,
            material=material,  # service._fit_lines 已按 max_length 截断，此处不再硬切片
        )
        request_type = f"plugin.{self.ctx.plugin_id}"
        try:
            # 任务名/模型名/模型标识 → 解析为任务路由或固定模型（移植自 smart-segmentation-plugin）
            target_kind, target_name = resolve_generation_model(self.config.query.llm_model)
            self.ctx.logger.info(
                "直接发送模型解析: configured=%r kind=%r target=%r",
                self.config.query.llm_model,
                target_kind,
                target_name or "<default>",
            )
            if target_kind == "model" and target_name:
                llm_result = await generate_with_pinned_model(
                    prompt,
                    resolved_model_name=target_name,
                    request_type=request_type,
                )
            else:
                gen_kwargs: dict[str, Any] = {"prompt": prompt}
                if target_name:
                    gen_kwargs["model"] = target_name
                llm_result = await self.ctx.llm.generate(**gen_kwargs)
        except Exception as exc:
            self.ctx.logger.exception("直接发送模式 LLM 调用异常")
            _ = exc
            return not_found

        answer = str((llm_result or {}).get("response") or "").strip()
        if not (llm_result or {}).get("success") or not answer:
            self.ctx.logger.error(
                "直接发送模式 LLM 加工失败: %s",
                str((llm_result or {}).get("error") or "LLM 未返回内容"),
            )
            return not_found

        # 回传模式（direct=False）：LLM 成品返回给 planner，由 replyer 带人格回复。
        # 必须让归属标记跟随正文进入 reply_reference：主程序会把 reply_reference
        # 渲染成 user 角色消息（无来源标记），裸正文会被当成"用户自己粘贴的内容"
        # （bot 与用户同名时必现）。标记行随正文走，replyer 端才能识别归属。
        if not direct:
            wrapped = (
                "[系统说明：以下是攻略插件查询到的攻略资料，是你（bot）查询所得，"
                "不是用户说的话。调用 reply 工具时，请把下方【攻略资料】整块"
                "（含首行标记）原样放入 reply_reference 参数，不要删改标记行，"
                "由回复流程基于它组织语言]\n"
                "【攻略资料·bot查询所得，非用户发言】\n"
                + answer
            )
            self.ctx.logger.info(
                "攻略已加工回传: %s, 长度=%d", tool_name, len(wrapped)
            )
            return {"name": tool_name, "content": wrapped}

        try:
            sent = await self.ctx.send.text(answer, stream_id)
        except Exception as exc:
            self.ctx.logger.exception("直接发送模式消息发送异常")
            _ = exc
            return not_found
        if not sent:
            self.ctx.logger.error("直接发送模式消息发送失败: stream=%s", stream_id)
            return not_found

        # 去重登记：只有真正直发成功后才写入（Fix A，【Metis 修订 #7】）
        if query and stream_id:
            self._recent_direct[(stream_id, query)] = time.time()
            self.ctx.logger.info("直发去重登记: key=%s", (stream_id, query))

        # 写入直发成品缓存（Fix B）
        if direct and self.config.query.answer_cache_ttl > 0 and cache_key:
            cache = self._get_answer_cache()
            cache.set(cache_key, answer)

        self.ctx.logger.info(
            "直接发送完成: %s -> stream=%s, 长度=%d", tool_name, stream_id, len(answer)
        )
        return {
            "name": tool_name,
            "content": (
                "攻略内容已直接发送到聊天，用户已经可以看到完整答案。"
                "你不需要也不应该再调用 reply 工具——reply 的回复内容会与已发送的攻略重复。"
                "请立即调用 wait 工具（seconds=5）结束本轮即可。"
            ),
        }

    # ===== Tool 组件 =====

    @Tool(
        "lookup_game_term",
        description="查询星塔旅人游戏专有名词的中英文对照和游戏内ID。"
                    "输入：中文名或英文名（单个词，不要传整句）。"
                    "输出：{id, en, cn, cat} 或未找到。"
                    "适用：遇到不认识的游戏术语时，先调用此工具获取ID和英文名，再调用其他工具。",
        parameters=[
            ToolParameterInfo(
                name="term",
                param_type=ToolParamType.STRING,
                description="中文名或英文名（单个词，不要传整句）",
                required=True,
            ),
        ],
    )
    async def handle_lookup(self, term: str = "", **kwargs):
        if self._denied(**kwargs):
            return {"name": "lookup_game_term", "content": "当前聊天不在星塔旅人插件的允许范围内。"}
        self._apply_overrides_config()
        term = (term or "").strip()
        if not term:
            return {"name": "lookup_game_term", "content": "缺少查询词。"}
        result = lookup_term(term)
        return {
            "name": "lookup_game_term",
            "content": json.dumps(result, ensure_ascii=False),
        }

    @Tool(
        "stellasora_what",
        description="查询星塔旅人游戏中角色、装备、技能的基本信息（是什么、属性、描述、培养素材、礼物偏好）。"
                    "输入：角色/装备的中文名或英文名。"
                    "输出：开启直接发送时攻略已直发聊天，返回后调 wait 结束本轮；"
                    "关闭直接发送时返回攻略正文，用 reply 组织回复。"
                    "适用：用户问'XX是谁''XX技能是什么''XX培养素材''XX属性'，"
                    "以及'XX的完整资料/档案/介绍/约会/礼物'时。"
                    "同一对象在同一轮只允许调用本组工具中的一个：已调用本工具并收到\u2018已发送\u2019后，不要再调用另一个，直接调 wait。",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="角色/装备的中文名或英文名",
                required=True,
            ),
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="用户的原始问题原文（如'夏花的完整资料'），用于生成贴合问题的回答；无法提取时可不传",
                required=False,
            ),
        ],
    )
    async def handle_what(self, query: str = "", question: str = "", **kwargs):
        if self._denied(**kwargs):
            return {"name": "stellasora_what", "content": "当前聊天不在星塔旅人插件的允许范围内。"}
        self._apply_overrides_config()
        query = (query or "").strip()
        if not query:
            return {"name": "stellasora_what", "content": "缺少查询词。"}
        self.ctx.logger.info("what 查询: %s (group=%s user=%s)", query, kwargs.get("group_id", ""), kwargs.get("user_id", ""))
        # Fix D：抓取+字典+替换是同步重活（urllib 网络 + 正则 CPU），放入线程池执行，
        # 避免 runner 事件循环被阻塞（async handler 直接 await 在 runner 循环上）
        text = await asyncio.to_thread(
            query_what,
            query,
            self._cache_dir_ready(),
            max_length=int(self.config.query.default_max_length),
        )
        # 未找到时不走 LLM 加工，直接返回给 planner 自行处理（approach B）
        if "未在字典中找到" in text:
            return {"name": "stellasora_what", "content": "未在星塔旅人游戏中找到该角色或装备。"}
        # 命中时 LLM 加工；direct_send=true 直发聊天，false 回传给 replyer（approach A）
        # 联合查询检测：用户原话命中 ≥2 个角色名时强制回传，planner 汇总后单条回复避免刷屏
        # count_character_names 内含正则匹配，同样为同步 CPU 重活，放入线程池
        effective_question = (question or "").strip() or query
        direct = self.config.query.direct_send and (
            await asyncio.to_thread(count_character_names, effective_question)
        ) < 2
        # 去重守卫：同流同主题在 dedup_window 内直接拦截（Fix A，【Metis 修订 #7/#8】）
        stream_id = self._resolve_stream_id(kwargs)
        now = time.time()
        # 清理过期键（含硬上界保护，防多群内存增长，【双审 SH-4】）
        if len(self._recent_direct) > MAX_DEDUP_ENTRIES:
            self._recent_direct.clear()
        else:
            expired = [k for k, ts in self._recent_direct.items() if now - ts > 10 * self.config.query.dedup_window]
            for k in expired:
                del self._recent_direct[k]
        dedup_key = (stream_id, query)
        if direct and dedup_key in self._recent_direct and (now - self._recent_direct[dedup_key]) < self.config.query.dedup_window:
            elapsed = int(now - self._recent_direct[dedup_key])
            self.ctx.logger.info("直发去重拦截: key=%s 距上次=%ds", dedup_key, elapsed)
            return {"name": "stellasora_what", "content": "该主题的攻略刚刚已直接发送过，请勿重复发送。请立即调用 wait 工具（seconds=5）结束本轮。"}
        return await self._direct_send(
            tool_name="stellasora_what",
            question=effective_question,
            material=text,
            direct=direct,
            presets=False,
            query=query,
            **kwargs,
        )

    @Tool(
        "stellasora_how",
        description="查询星塔旅人游戏中配队、纹章搭配、秘纹搭配、技能升级优先度等操作指南。"
                    "输入：角色名或元素名（中文名：水/火/风/地/光/暗）。"
                    "输出：开启直接发送时攻略已直发聊天，返回后调 wait 结束本轮；"
                    "关闭直接发送时返回攻略正文，用 reply 组织回复。"
                    "适用：用户问'XX怎么配队''XX纹章怎么选''XX秘纹推荐''XX先升级什么技能'，"
                    "以及'XX的攻略/怎么玩'时；用户问'XX的完整资料'则改用 stellasora_what。"
                    "注意：仅当用户明确要求'预设码'时才传 presets=true 参数。"
                    "同一对象在同一轮只允许调用本组工具中的一个：已调用本工具并收到\u2018已发送\u2019后，不要再调用另一个，直接调 wait。",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="角色名或元素名（中/英均可，元素中文名：水/火/风/地/光/暗）",
                required=True,
            ),
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="用户的原始问题原文（如'夏花的完整攻略'），用于生成贴合问题的回答；无法提取时可不传",
                required=False,
            ),
            ToolParameterInfo(
                name="presets",
                param_type=ToolParamType.BOOLEAN,
                description="是否查询预设码（仅用户明确要求预设码时为 true）",
                required=False,
            ),
        ],
    )
    async def handle_how(self, query: str = "", question: str = "", presets: bool = False, **kwargs):
        if self._denied(**kwargs):
            return {"name": "stellasora_how", "content": "当前聊天不在星塔旅人插件的允许范围内。"}
        self._apply_overrides_config()
        query = (query or "").strip()
        if not query:
            return {"name": "stellasora_how", "content": "缺少查询词。"}
        self.ctx.logger.info(
            "how 查询: %s presets=%s (group=%s user=%s)",
            query, presets, kwargs.get("group_id", ""), kwargs.get("user_id", ""),
        )
        # Fix D：同 handle_what，query_how 为同步重活（抓取+替换），放入线程池执行
        text = await asyncio.to_thread(
            query_how,
            query,
            self._cache_dir_ready(),
            with_presets=bool(presets),
            max_length=int(self.config.query.default_max_length),
        )
        # 未找到时不走 LLM 加工，直接返回给 planner 自行处理（approach B）
        if "未在字典中找到" in text:
            return {"name": "stellasora_how", "content": "未在星塔旅人游戏中找到该角色或元素。"}
        # 命中时 LLM 加工；direct_send=true 直发聊天，false 回传给 replyer（approach A）
        # 联合查询检测：用户原话命中 ≥2 个角色名时强制回传，planner 汇总后单条回复避免刷屏
        # count_character_names 内含正则匹配，同样为同步 CPU 重活，放入线程池
        effective_question = (question or "").strip() or query
        direct = self.config.query.direct_send and (
            await asyncio.to_thread(count_character_names, effective_question)
        ) < 2
        # 去重守卫：同流同主题在 dedup_window 内直接拦截（Fix A，【Metis 修订 #7/#8】）
        stream_id = self._resolve_stream_id(kwargs)
        now = time.time()
        # 清理过期键（含硬上界保护，防多群内存增长，【双审 SH-4】）
        if len(self._recent_direct) > MAX_DEDUP_ENTRIES:
            self._recent_direct.clear()
        else:
            expired = [k for k, ts in self._recent_direct.items() if now - ts > 10 * self.config.query.dedup_window]
            for k in expired:
                del self._recent_direct[k]
        dedup_key = (stream_id, query)
        if direct and dedup_key in self._recent_direct and (now - self._recent_direct[dedup_key]) < self.config.query.dedup_window:
            elapsed = int(now - self._recent_direct[dedup_key])
            self.ctx.logger.info("直发去重拦截: key=%s 距上次=%ds", dedup_key, elapsed)
            return {"name": "stellasora_how", "content": "该主题的攻略刚刚已直接发送过，请勿重复发送。请立即调用 wait 工具（seconds=5）结束本轮。"}
        return await self._direct_send(
            tool_name="stellasora_how",
            question=effective_question,
            material=text,
            direct=direct,
            presets=presets,
            query=query,
            **kwargs,
        )


def create_plugin() -> StellaSoraPlugin:
    """创建插件实例（MaiBot 插件入口约定）。"""
    return StellaSoraPlugin()
