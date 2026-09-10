"""星塔旅人（Stella Sora）攻略查询插件

将三个查询工具包装为 MaiBot Tool，由 planner 自动路由：
  - stellasora_what    "是什么"：角色属性/技能/素材/礼物
  - stellasora_how     "怎么玩"：配队/纹章/秘纹/技能升级优先度（预设码按需）
  - lookup_game_term   查词：游戏术语 → 官方中文名 + 游戏内 ID

安全：白名单/黑名单模式可配置（config.toml），群聊按群号、私聊按用户号鉴权。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional
import asyncio
import json
import logging
import sys
import time

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParamType, ToolParameterInfo

MAX_DEDUP_ENTRIES = 2000  # 去重记录硬上界，防止多群场景内存增长

# 让插件可以导入 tools/ 下的模块
_TOOLS_DIR = Path(__file__).resolve().parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from cache import CacheManager  # noqa: E402
from service import (  # noqa: E402
    apply_priority_filter,
    check_permission,
    configure_overrides,
    count_character_names,
    detect_element_query,
    find_character_names_ordered,
    find_team_rows,
    lookup_term,
    preheat_services,
    query_how_rows,
    query_what,
    reload_team_table,
)
from sync_data import sync_offline_data  # noqa: E402

logger = logging.getLogger("stellasora.plugin")

# 直发提示词单一事实源文档路径与模块级缓存（修改文档后需重启插件生效）
# how 与 what 各自独立：how 用 docs/prompts_how.md（含内联游戏知识），what 用 docs/prompts_what.md
_PROMPT_DOC_PATH_HOW = Path(__file__).resolve().parent / "docs" / "prompts_how.md"
_PROMPT_DOC_PATH_WHAT = Path(__file__).resolve().parent / "docs" / "prompts_what.md"
_PROMPT_DOC_CACHE_HOW: Optional[str] = None
_PROMPT_DOC_CACHE_WHAT: Optional[str] = None


def _load_prompt_doc_how() -> Optional[str]:
    """加载 docs/prompts_how.md 直发提示词文档（模块级缓存，单一事实源）。

    提示词文档是直发模式的必需项：缺失或读取失败记录 error 并返回 None
    （不回退内嵌旧文），由 _direct_send 显式处理失败。
    """
    global _PROMPT_DOC_CACHE_HOW
    if _PROMPT_DOC_CACHE_HOW is None:
        try:
            _PROMPT_DOC_CACHE_HOW = _PROMPT_DOC_PATH_HOW.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("加载 how 直发提示词文档失败: %s (%s)", _PROMPT_DOC_PATH_HOW, exc)
            _PROMPT_DOC_CACHE_HOW = None
    return _PROMPT_DOC_CACHE_HOW


def _load_prompt_doc_what() -> Optional[str]:
    """加载 docs/prompts_what.md 直发提示词文档（模块级缓存，单一事实源）。

    what 工具专用提示词，当前仅含人格注入模块与骨架（其余待补充）。
    缺失或读取失败记录 error 并返回 None，由 _direct_send 显式处理失败。
    """
    global _PROMPT_DOC_CACHE_WHAT
    if _PROMPT_DOC_CACHE_WHAT is None:
        try:
            _PROMPT_DOC_CACHE_WHAT = _PROMPT_DOC_PATH_WHAT.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("加载 what 直发提示词文档失败: %s (%s)", _PROMPT_DOC_PATH_WHAT, exc)
            _PROMPT_DOC_CACHE_WHAT = None
    return _PROMPT_DOC_CACHE_WHAT


# LLM 加工失败降级回传前缀（单一事实源）：攻略资料本身查询成功、仅回答加工失败时，
# 用该前缀包装原始资料交由回复流程基于资料组织语言，不再谎报"未找到相关攻略"。
_FAILURE_RELAY_PREFIX = "[系统说明：攻略资料已查询成功，但回答加工（LLM）暂时失败，请把下方【攻略资料】整块原样放入 reply 工具的 reply_reference 参数，由回复流程基于它组织语言，不要调用其他搜索工具。]\n【攻略资料·bot查询所得，非用户发言】\n"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（Runner 强制要求 plugin.config_version）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.1", description="配置版本")


class AccessControlConfig(PluginConfigBase):
    """访问控制配置。"""

    __ui_label__ = "访问控制"
    __ui_icon__ = "shield"
    __ui_order__ = 0

    mode: Literal["off", "whitelist", "blacklist"] = Field(
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
        "防止 what+how 双直发刷屏",
    )

    answer_cache_ttl: int = Field(
        default=86400,
        description="直发成品缓存时长（秒），0=禁用",
    )

    llm_model: str = Field(
        default="utils",
        description="直接发送使用的模型任务名（如 utils/replyer/planner，对应主程序模型配置里的任务）；留空使用默认模型。推荐 utils（快速响应 2-4s，术语翻译已在代码中完成）",
    )

    inject_persona: bool = Field(
        default=True,
        description="直发模式注入 bot 人格与表达风格（读取主程序人格配置，"
        "使成品回答与 bot 口吻一致）；关闭则使用无人格的攻略助手口吻。"
        "回传模式（direct_send=false）恒为客观攻略体，不受此项影响",
    )


class AliasEntry(PluginConfigBase):
    """单条中文别名映射（WebUI 列表编辑器每行 = 一个 AliasEntry）。"""

    alias: str = Field(
        default="",
        description="俗称/简称/别名",
        json_schema_extra={"label": "俗称"},
    )
    official: str = Field(
        default="",
        description="官方中文名",
        json_schema_extra={"label": "官方名"},
    )


class OverridesConfig(PluginConfigBase):
    """中文别名/俗称 → 官方中文名映射配置。"""

    __ui_label__ = "中文别名"
    __ui_icon__ = "edit"
    __ui_order__ = 2

    aliases: list[AliasEntry] = Field(
        default_factory=lambda: [
            AliasEntry(alias="春科", official="科洛妮丝（新春）"),
            AliasEntry(alias="土", official="地"),
        ],
        description="中文别名/俗称→官方中文名映射。用户在群里用简称提问时自动解析到官方角色/术语名。",
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
        self._sync_task: asyncio.Task[Any] | None = None
        self._preheat_task: asyncio.Task[None] | None = None
        self._answer_cfg_fp: str = ""  # 答案相关配置指纹（on_config_update 去抖）

    # ===== 生命周期 =====

    def _apply_overrides_config(self) -> None:
        """将 config 中的 [overrides.aliases] 中文别名应用到运行时查词服务层。"""
        try:
            overrides = getattr(self.config, "overrides", None)
            if overrides and overrides.aliases:
                alias_dict = {
                    entry.alias: entry.official
                    for entry in overrides.aliases
                    if entry.alias and entry.official
                }
            else:
                alias_dict = {}
            configure_overrides(aliases=alias_dict)
        except Exception as exc:
            self.ctx.logger.warning("应用 overrides 别名配置失败: %s", exc)

    @staticmethod
    def _calculate_delay_to_sync(
        now: Optional[datetime] = None,
        target_hour: int = 17,
        target_minute: int = 0,
    ) -> float:
        """计算当前本地时间距离下一个目标同步时间（默认 17:00:00）的秒数。

        若当前未过目标时间，计算到今日目标时间；若已过，计算到明日目标时间。
        """
        if now is None:
            now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _schedule_daily_sync(self) -> None:
        """每日 17:00 自动定时静默全量同步离线数据后台任务。"""
        try:
            while True:
                delay = self._calculate_delay_to_sync()
                self.ctx.logger.info("星塔旅人离线数据定时同步计划在 %.1f 秒后执行（目标: 17:00）", delay)
                await asyncio.sleep(delay)
                try:
                    self.ctx.logger.info("开始执行每日 17:00 离线数据全量定时同步...")
                    await asyncio.to_thread(sync_offline_data, sync_all=True)
                    # 表缓存失效接线：定时同步产出新统一表后立即失效表缓存
                    reload_team_table()
                    # 清空直发成品缓存并清理 answers 磁盘缓存
                    answers_dir = self._cache_dir_ready() / "answers"
                    if answers_dir.exists():
                        for p in answers_dir.glob("*.json"):
                            try:
                                p.unlink()
                            except Exception:
                                pass
                    try:
                        self._get_answer_cache()._memory_cache.clear()
                    except Exception:
                        pass
                    self.ctx.logger.info("每日 17:00 离线数据定时同步完成")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.ctx.logger.warning("每日 17:00 离线数据定时同步执行异常: %s", exc)
        except asyncio.CancelledError:
            self.ctx.logger.info("每日定时同步后台任务已取消并优雅退出")

    async def on_load(self) -> None:
        """插件加载：准备运行时缓存目录并同步 overrides 配置，启动每日 17:00 定时同步任务。

        字典缓存放 runtime_dir（非持久，可随时重建）；
        字典本体在插件包内 data/（只读）。
        """
        self._cache_dir = Path(self.ctx.paths.runtime_dir) / "webcache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._apply_overrides_config()
        # 答案相关配置指纹基线：加载后的首次空更新不清缓存
        self._answer_cfg_fp = self._answer_relevant_fingerprint()
        self._sync_task = asyncio.create_task(self._schedule_daily_sync())
        # 后台预热共享服务（字典 8.8MB 解析+替换器编译+表加载）：消除首次查询秒级冷启动
        self._preheat_task = asyncio.create_task(asyncio.to_thread(preheat_services))
        self.ctx.logger.info("星塔旅人插件已加载，缓存目录: %s", self._cache_dir)

    async def on_unload(self) -> None:
        """插件卸载：取消每日定时同步任务并清空运行时缓存引用（文件留给磁盘回收）。"""
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
        self._sync_task = None
        if self._preheat_task and not self._preheat_task.done():
            self._preheat_task.cancel()
            await asyncio.gather(self._preheat_task, return_exceptions=True)
        self._preheat_task = None
        self._cache_dir = None
        self._answer_cache = None
        self.ctx.logger.info("星塔旅人插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        """配置热重载：黑白名单、overrides 与查询参数即时生效，无需重启。

        答案相关配置指纹去抖：宿主会推送 scope=self 的配置更新（WebUI 保存/
        轮询均可能触发），其中大量为无实质变化的空更新——若每次都清空答案缓存，
        刚写入的成品秒被清掉，重复问题重复调用 LLM（实例日志证实）。
        仅当影响答案的配置字段（aliases/replacements/llm_model/inject_persona/
        default_max_length/config_version）实际变化时才清缓存。
        """
        self.ctx.logger.info(
            "配置已更新: scope=%s version=%s（黑白名单与自定义覆盖即时生效）", scope, version
        )
        self._apply_overrides_config()
        fingerprint = self._answer_relevant_fingerprint()
        if fingerprint == self._answer_cfg_fp:
            self.ctx.logger.info("配置更新无答案相关变化，保留直发成品缓存")
            return
        self._answer_cfg_fp = fingerprint
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

    def _code_fingerprint(self) -> str:
        """插件代码指纹（源码/提示词文件最新 mtime 的整秒值）。

        纳入直发成品缓存 key：代码或提示词更新后旧答案自动失效，
        不再依赖人工 bump config_version（实例事故：过滤逻辑修复后
        config_version 未变，同 key 命中修复前的 10 队旧答案原样重发）。
        """
        root = Path(__file__).resolve().parent
        latest = 0.0
        candidates = [root / "plugin.py", *root.glob("tools/*.py"), *root.glob("docs/*.md")]
        for p in candidates:
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                continue
        return str(int(latest))

    def _answer_relevant_fingerprint(self) -> str:
        """影响直发成品答案的配置字段指纹（on_config_update 去抖用）。"""
        c = self.config
        try:
            aliases = json.dumps(c.overrides.aliases, ensure_ascii=False, sort_keys=True)
            replacements = json.dumps(c.overrides.replacements, ensure_ascii=False, sort_keys=True)
        except Exception:
            aliases = replacements = ""
        return "|".join((
            aliases,
            replacements,
            c.query.llm_model,
            str(c.query.inject_persona),
            str(c.query.default_max_length),
            c.plugin.config_version,
        ))

    # ===== 内部工具 =====

    def _cache_dir_ready(self) -> Path:
        if self._cache_dir is None:
            self._cache_dir = Path(self.ctx.paths.runtime_dir) / "webcache"
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def _get_answer_cache(self) -> CacheManager:
        """获取直发成品缓存管理器（懒创建并复用，同步当前 TTL 配置）。

        线程说明：答案缓存仅在事件循环侧访问。
        """
        ttl = int(self.config.query.answer_cache_ttl)
        expected_dir = self._cache_dir_ready() / "answers"
        if self._answer_cache is None or self._answer_cache.cache_dir != expected_dir:
            self._answer_cache = CacheManager(expected_dir, ttl_seconds=ttl)
        else:
            self._answer_cache.ttl_seconds = ttl
        return self._answer_cache

    def _resolve_stream_id(self, kwargs: dict) -> str:
        """从工具调用 kwargs 提取 stream_id（直发/去重/未找到发送共用）。"""
        return (str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")).strip()

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

    # 直发 Prompt 双模板：how 用 docs/prompts_how.md（含 {persona_block}/{question}/{material} 与内联游戏知识+回答规则 1-9），
    # what 用 docs/prompts_what.md（当前仅人格注入模块+骨架，其余待补充）。
    # 由模块级 _load_prompt_doc_how() / _load_prompt_doc_what() 加载（模块级缓存，修改后需重启生效）；
    # 加载失败为 None，_direct_send 开头显式判 None 返回"未找到相关攻略。"，不回退内嵌旧文。
    # 本 Prompt 为插件自维护文档模板，非 prompts/ 目录模板，不受多语言同步约束。
    _DIRECT_SEND_PROMPT_HOW: Optional[str] = _load_prompt_doc_how()
    _DIRECT_SEND_PROMPT_WHAT: Optional[str] = _load_prompt_doc_what()

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

        失败语义：提示词文档缺失/目标缺失返回"未找到相关攻略。"；
        LLM 加工失败（异常/success 非 True/响应空白）且资料非空时，
        降级为失败回传——用系统说明前缀包装原始资料返回 planner，不再谎报"未找到"。
        """
        not_found = {"name": tool_name, "content": "未找到相关攻略。"}

        # 提示词双模板选择：how → prompts_how.md，what → prompts_what.md
        is_what = tool_name.endswith("_what")
        prompt_template = self._DIRECT_SEND_PROMPT_WHAT if is_what else self._DIRECT_SEND_PROMPT_HOW
        # 提示词单一事实源守卫：加载失败时不静默兜底、不回退内嵌旧文
        if prompt_template is None:
            self.ctx.logger.error("%s 直发提示词文档缺失，无法加工攻略，返回未找到", tool_name)
            return not_found

        question = (question or "").strip()
        if not question:
            self.ctx.logger.warning("直接发送模式缺少用户问题，返回未找到")
            return not_found

        stream_id = self._resolve_stream_id(kwargs)
        if direct and not stream_id:
            self.ctx.logger.warning("直接发送模式缺少 stream_id，无法确定发送目标")
            return not_found

        # 直发成品缓存查取：仅对 direct=True 生效；TTL <= 0 时显式跳过
        cache_key = ""
        if direct and self.config.query.answer_cache_ttl > 0:
            inject_persona = self.config.query.inject_persona
            cache_key = f"{tool_name}|{query}|{question}|{presets}|{self.config.query.llm_model}|{self.config.plugin.config_version}|{inject_persona}|{self._code_fingerprint()}"
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
                # 发送成功即登记去重守卫，保证重复可拦截
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
        # 游戏知识已内联至 prompts_how.md 正文，无需运行时注入；what 模板无知识块
        prompt = prompt_template.format(
            persona_block=persona_block,
            question=question,
            material=material,  # service._fit_lines 已按 max_length 截断，此处不再硬切片
        )
        llm_model = (self.config.query.llm_model or "").strip()

        def _llm_failed(reason: str) -> dict:
            """LLM 加工失败的统一降级出口。

            资料非空 → 失败回传：用系统说明前缀包装原始资料返回 planner，
            不写直发成品缓存，不再谎报"未找到"；
            资料为空 → 无内容可回传，维持"未找到相关攻略。"。
            """
            if (material or "").strip():
                self.ctx.logger.warning(
                    "LLM 加工失败，降级回传原始资料（不写成品缓存）: 原因=%s, tool=%s",
                    reason,
                    tool_name,
                )
                return {"name": tool_name, "content": _FAILURE_RELAY_PREFIX + material}
            self.ctx.logger.warning(
                "LLM 加工失败且资料为空，返回未找到: 原因=%s, tool=%s", reason, tool_name
            )
            return not_found

        try:
            gen_kwargs: dict[str, Any] = {"prompt": prompt}
            if llm_model:
                gen_kwargs["model"] = llm_model
            llm_result = await self.ctx.llm.generate(**gen_kwargs)
        except Exception as exc:
            self.ctx.logger.exception("直接发送模式 LLM 调用异常")
            _ = exc
            return _llm_failed(f"LLM 调用异常: {exc}")

        answer = str((llm_result or {}).get("response") or "").strip()
        if not (llm_result or {}).get("success") or not answer:
            return _llm_failed(str((llm_result or {}).get("error") or "LLM 未返回内容"))

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

        # 去重登记：只有真正直发成功后才写入
        if query and stream_id:
            self._recent_direct[(stream_id, query)] = time.time()
            self.ctx.logger.info("直发去重登记: key=%s", (stream_id, query))

        # 写入直发成品缓存
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
                description="角色/装备的名字本身（只传名字，不要带'攻略'/'资料'/'素材'等后缀词；中/英均可）",
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
        # 未找到时不走 LLM 加工，直接返回
        if "未在字典中找到" in text:
            return {"name": "stellasora_what", "content": "未在星塔旅人游戏中找到该角色或装备。"}
        # 命中时 LLM 加工；direct_send=true 直发聊天，false 回传给 replyer
        # 联合查询检测：用户原话命中 ≥2 个角色名时强制回传，planner 汇总后单条回复避免刷屏
        # count_character_names 内含正则匹配，同样为同步 CPU 重活，放入线程池
        effective_question = (question or "").strip() or query
        direct = self.config.query.direct_send and (
            await asyncio.to_thread(count_character_names, effective_question)
        ) < 2
        # 去重守卫：同流同主题在 dedup_window 内直接拦截
        dedup_resp = self._dedup_guard("stellasora_what", query, direct, **kwargs)
        if dedup_resp is not None:
            return dedup_resp
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
                    "输入：query 传角色名（可空格分隔多个，如'小禾 格芮'）；纯属性泛查直接传属性词（如'风队'）。"
                    "输出：开启直接发送时攻略已直发聊天，返回后调 wait 结束本轮；"
                    "关闭直接发送时返回攻略正文，用 reply 组织回复。"
                    "适用：用户问'XX怎么配队''XX纹章怎么选''XX秘纹推荐''XX先升级什么技能'，"
                    "以及'XX的攻略/怎么玩'或'X系/属性队'时；用户问'XX的完整资料'则改用 stellasora_what。"
                    "注意：仅当用户明确要求'预设码'时才传 presets=true 参数。"
                    "question 必须传用户原话逐字内容（联合查询识别与'第一个角色'排序依赖原文，缺失时仅能按 query 兜底）。"
                    "同一对象在同一轮只允许调用本组工具中的一个：已调用本工具并收到\u2018已发送\u2019后，不要再调用另一个，直接调 wait。",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="角色名，可空格分隔多个（如'小禾 格芮'）；question 缺失时作为兜底归一（中/英均可）",
                required=True,
            ),
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="用户的原始问题原文，逐字传入（如'小禾 格芮攻略'）——联合查询识别与首个角色排序依赖原文，不可改写或省略",
                required=True,
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
        # 表查询/区块解析为同步重活，放入线程池执行
        effective_question = (question or "").strip() or query
        # 统一表驱动链路：问句提取角色（含别名预处理，支持多角色）→
        # 兜底归一 query 词 → char_id 集 → find_team_rows 交集查询。
        # 问句未命中任何角色时尝试纯属性泛查（如"风队"），仍未命中则"未找到相关攻略"。
        # 问句保序提取——联合查询详略与首角色排序依赖问句出现顺序。
        found_names = await asyncio.to_thread(find_character_names_ordered, effective_question)
        if not found_names:
            # 问句未命中角色名：用 query 参数做兜底归一（支持空格分隔多名）
            for token in query.split():
                res = lookup_term(token)
                if res and not res.get("not_found") and res.get("cat") == "Character":
                    cn = res["cn"]
                    if cn not in found_names:
                        found_names.append(cn)
        if found_names:
            member_ids = await asyncio.to_thread(self._resolve_character_ids, found_names)
            rows = await asyncio.to_thread(find_team_rows, member_ids)
            miss_desc = f"角色={found_names} member_ids={member_ids}"
        else:
            # 纯属性泛查：问句含「元素词+队/系/属性」→ 该元素全量候选行，交优先级过滤
            element = detect_element_query(effective_question)
            if not element:
                self.ctx.logger.info("how 表查询未命中角色且非属性泛查: query=%s question=%s", query, effective_question)
                return await self._send_or_relay("未找到相关攻略。", effective_question, presets, **kwargs)
            member_ids = []
            rows = await asyncio.to_thread(find_team_rows, [], element)
            miss_desc = f"元素={element}"
        if not rows:
            self.ctx.logger.info("how 表未命中: %s（交集为空）", miss_desc)
            return await self._send_or_relay("未找到相关攻略。", effective_question, presets, **kwargs)
        # 热门优先过滤（单角色/纯属性泛查统一）：热门全出，热门区块<3 按表序补冷门至 3；
        # 问句含 全部/所有/完整/详细 时不过滤
        rows = await asyncio.to_thread(apply_priority_filter, rows, effective_question)
        text = await asyncio.to_thread(
            query_how_rows,
            rows,
            bool(presets),
            int(self.config.query.default_max_length),
            effective_question,
        )
        return await self._send_or_relay(text, effective_question, presets, **kwargs)

    # ===== 去重守卫 =====

    def _dedup_guard(self, tool_name: str, query: str, direct: bool, **kwargs) -> Optional[dict]:
        """直发去重守卫：同流同主题在 dedup_window 内直接拦截。

        返回拦截响应 dict；放行时返回 None。同时清理过期键（含硬上界保护，防多群内存增长）。
        """
        stream_id = self._resolve_stream_id(kwargs)
        now = time.time()
        # 清理过期键（含硬上界保护，防多群内存增长）
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
            return {"name": tool_name, "content": "该主题的攻略刚刚已直接发送过，请勿重复发送。请立即调用 wait 工具（seconds=5）结束本轮。"}
        return None

    @staticmethod
    def _resolve_character_ids(names: list) -> list[int]:
        """角色中文名列表 → 去重后的 CharId 列表（经共享查词服务归一）。"""
        ids: list[int] = []
        for name in names:
            res = lookup_term(name)
            if res and not res.get("not_found") and res.get("cat") == "Character":
                try:
                    char_id = int(str(res["id"]).split(".")[1])
                except (IndexError, ValueError):
                    continue
                if char_id not in ids:
                    ids.append(char_id)
        return ids

    async def _send_or_relay(self, text: str, effective_question: str, presets, **kwargs):
        """how 查询的直发/回传公共路径（未找到直发提示 + 去重守卫 + LLM 加工）。

        直发判定由配置 direct_send 决定。
        未找到语义（用户裁定）：direct_send=true 时不再回传 planner——
        直接向聊天发送用户可读提示并返回"已发送"确认，planner 只需 wait 结束本轮，
        避免 planner 拿到否定结果后再组织一轮多余回复；direct_send=false 维持回传。
        """
        query = effective_question
        direct = self.config.query.direct_send
        stream_id = self._resolve_stream_id(kwargs)
        # 未找到短路（用户裁定）：不走 LLM 加工、不让 planner 组织否定回复。
        # 直发模式：提示文本直接发送到聊天，planner 收"已发送"告知只需 wait；
        # 回传模式：维持原文返回 planner（不直发，replyer 自行组织）。
        if not (text or "").strip() or "未找到相关攻略" in text:
            not_found = {"name": "stellasora_how", "content": "未找到相关攻略。"}
            if not direct:
                return not_found
            self.ctx.logger.info("how 未找到，直发模式直接发送提示: stream=%s", stream_id)
            if stream_id:
                try:
                    await self.ctx.send.text("攻略库里没有查到相关内容，换个说法或换个角色试试～", stream_id)
                except Exception:
                    self.ctx.logger.exception("how 未找到提示发送异常")
                    return not_found
            return {
                "name": "stellasora_how",
                "content": (
                    "已向聊天发送'未找到相关攻略'的提示。你不需要也不应该再调用 reply 工具，"
                    "请立即调用 wait 工具（seconds=5）结束本轮即可。"
                ),
            }
        # 去重守卫：同流同主题在 dedup_window 内直接拦截
        dedup_resp = self._dedup_guard("stellasora_how", query, direct, **kwargs)
        if dedup_resp is not None:
            return dedup_resp
        return await self._direct_send(
            tool_name="stellasora_how",
            question=effective_question,
            material=text,
            direct=direct,
            presets=presets,
            query=query,
            **kwargs,
        )

    # ===== Command 指令 =====

    @Command("st_update", description="手动触发星塔旅人全量攻略与预设码数据离线更新", pattern=r"^/st_update")
    async def handle_update(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """手动触发星塔旅人全量攻略与预设码数据离线更新。"""
        if self._denied(**kwargs):
            return False, "当前聊天无权限执行星塔旅人更新指令。", 1

        effective_stream_id = stream_id or self._resolve_stream_id(kwargs)
        if effective_stream_id:
            try:
                await self.ctx.send.text("正在后台同步星塔旅人离线数据...", effective_stream_id)
            except Exception as exc:
                self.ctx.logger.warning("发送更新开始提示异常: %s", exc)

        try:
            sync_res = await asyncio.to_thread(sync_offline_data, sync_all=True)
            self.ctx.logger.info("离线数据同步完成: %s", sync_res)
        except Exception as exc:
            self.ctx.logger.exception("星塔旅人离线数据同步异常: %s", exc)
            return False, f"星塔旅人离线数据同步失败: {exc}", 1

        # 表缓存失效接线：同步产出新统一表后立即失效运行时表缓存，
        # 后续 how 查询即时读取新表（在清空直发成品缓存之前执行）
        reload_team_table()

        # 清空直发成品缓存（磁盘文件与内存缓存）
        answers_dir = self._cache_dir_ready() / "answers"
        if answers_dir.exists():
            for p in answers_dir.glob("*.json"):
                try:
                    p.unlink()
                except Exception:
                    pass
        try:
            self._get_answer_cache()._memory_cache.clear()
        except Exception:
            pass

        if effective_stream_id:
            try:
                await self.ctx.send.text("星塔旅人离线数据同步完成，缓存已刷新。", effective_stream_id)
            except Exception as exc:
                self.ctx.logger.warning("发送更新完成提示异常: %s", exc)

        return True, "星塔旅人离线数据同步完成", 2


def create_plugin() -> StellaSoraPlugin:
    """创建插件实例（MaiBot 插件入口约定）。"""
    return StellaSoraPlugin()
