"""星塔旅人（Stella Sora）攻略与排行榜插件

GameKee 中文源（一图流攻略原图直发）+ ssleaderboard 排行榜图片化：
  - stellasora_how  Tool  一图流攻略查询（六属性 24 角色小节，合并转发）
  - stellasora_what Tool  GameKee 客观资料查询（默认关闭，本地库检索后合并转发）
  - /st_bb・/st_fe   Command  当期国服 Top100 图片榜单
  - /st_update      Command  强刷两榜 + 探查攻略源（操作员；what 开启时另起一路建库）

安全：白名单/黑名单模式可配置（config.toml），群聊按群号、私聊按用户号鉴权；
仅 QQ 平台（Hook 可见性 + 执行层双闸，未知平台保守拒绝）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, cast
import asyncio
import base64
import json
import logging
import os
import re
import threading
import sys
import time

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParamType, ToolParameterInfo


MAX_SESSION_CACHE_ENTRIES = 2000  # session 身份缓存硬上界（Hook 前置过滤用），防内存无界增长

# 直发成品合并转发卡的节点署名；user_id 留空由适配器回落机器人自身账号
_FORWARD_NICKNAME = "星塔旅人"

# GameKee 属性键 → 中文标签（攻略图节点头显示用；与 tools/gamekee.ELEMENT_CN 同源）
_ELEMENT_CN_LABELS = {"water": "水", "fire": "火", "light": "光", "dark": "暗", "wind": "风", "earth": "地"}

# 网页相册轮播提示（实机六页唯一变体）：只用于展示端剔除已污染存量索引的 note，
# 解析闸（tools/gamekee.parse_content_json 的图前位置闸）修好后新索引不再有这句。
_ALBUM_HINT_TEXT = "左右滑动可以查看下一张图！！"

# 工具 RPC 预算与插件内 LLM 调用预算（毫秒）。
# 宿主默认工具超时 60s（component_timeout.DEFAULT_COMPONENT_RPC_TIMEOUT_MS），
# 插件能力 RPC 默认超时 30s（runner rpc_client 默认值）——两者叠加会造成
# 「慢模型下 LLM 先于工具超时」的静默降级（LLM 异常 → 回传原始资料而非成品）。
# 故显式声明：工具预算由 @Tool(timeout_ms=...) 经 metadata 上报宿主，
# 内部 LLM 预算须略小于工具预算，余量留给 LLM 之前的抓取/渲染（更早发生）。
# timeout_ms 具名参数自 SDK 2.5.4 起支持；模型选择走 task_name（任务名）契约自
# SDK 2.8.1 起，manifest 的 sdk.min_version 已钉死 2.8.1 保证两者可用。
_TOOL_RPC_TIMEOUT_MS = 90000
_LLM_RPC_TIMEOUT_MS = 80000

# 让插件可以导入 tools/ 下的模块
_TOOLS_DIR = Path(__file__).resolve().parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from gamekee import (  # noqa: E402
    GameKeeError,
    atomic_write as _atomic_write_gamekee,
    build_index as gamekee_build_index,
    download_image as gamekee_download_image,
    index_schema_version as gamekee_index_schema_version,
    match_guides as gamekee_match_guides,
    probe_updates as gamekee_probe_updates,
)
from leaderboard import (  # noqa: E402
    BoardUnavailable,
    board_fingerprint,
    format_fetch_time,
    load_board_for_render,
    refresh_board_for_update,
    render_text_board_nodes,
    resolve_current_board,
)
from leaderboard_render import build_board_pages, render_board_pages
from bili_guides import (
    BiliGuideError,
    fetch_guide_images as bili_fetch_guide_images,
    load_manifest as bili_load_manifest,
    match_guides as bili_match_guides,
)
from access import check_permission  # noqa: E402

logger = logging.getLogger("stellasora.plugin")

class GameKeeProbeFailure(Exception):
    """GameKee 探查/重建失败（网络/结构漂移/代际过期），由调用方区分旧快照降级或向上暴露。"""


@dataclass
class GuideResult:
    """攻略源收集结果（两源对称；收集不发送，发送由公共体统一做）。

    - nodes：待发送的合并转发节点（已含图段/文本段）；
    - image_count：实际图片张数（摘要用）；
    - code_pages：附带的队伍码页数（仅 GameKee 源）；
    - miss：该源确认未收录（区别于失败）；
    - failure：该源出错（文案含原因，用于 all 模式标注与单源提示）。
    """

    nodes: list[dict] = field(default_factory=list)
    image_count: int = 0
    code_pages: int = 0
    miss: bool = False
    failure: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.nodes)



# planner 工具剔除清单（Hook 前置过滤）：1.5.0 起本清单只含 how。
# what 默认关闭，它是否暴露由 [what] enabled 决定，故不并入本清单——并入会让
# 「授权聊天里 what 关闭时」和「未授权聊天里剔除全部插件工具」两种语义混成一个集合。
_TOOL_NAMES = frozenset({"stellasora_how"})
_WHAT_TOOL = "stellasora_what"
_PLUGIN_TOOL_NAMES = _TOOL_NAMES | {_WHAT_TOOL}

# QQ 用户可见短提示：固定短语，不写解释、错误码或套话（细节只进日志与工具回执）。
_WORD_MISS = "没查到"
_WORD_READ_FAIL = "读取失败"
_WORD_PARTIAL = "部分发出"
_WORD_UNKNOWN = "送达不确定"
_WORD_SEND_FAIL = "发送失败"
_WORD_NO_LIBRARY = "请先执行 /st_update"
_WORD_BUILDING = "建库中"


def _what_reason_text(reasons: Any, *, limit: int = 3) -> str:
    """刷新失败原因（库侧逐条 dict：code/detail）→ 一行短文本，最多 limit 条。"""
    parts: list[str] = []
    for item in list(reasons or [])[:limit]:
        if isinstance(item, dict):
            code = str(item.get("code") or "")
            detail = str(item.get("detail") or "")
            parts.append(f"{code}: {detail}" if detail else code)
        elif item:
            parts.append(str(item))
    return "；".join(p for p in parts if p)

# 资料查询意图契约：只声明业务参数（query + intent）。会话字段（stream_id/platform/
# group_id/user_id）由宿主注入，不出站在 schema 里让模型自己填。
_WHAT_MODES = ("field", "article", "media", "catalog")
_WHAT_SUBJECT_KINDS = ("traveler", "sigil", "npc", "monster", "term")
_WHAT_SHAPES = ("single", "list", "whole_article")
_WHAT_INTENT_PROPERTIES: dict[str, Any] = {
    "mode": {
        "type": "string",
        "enum": list(_WHAT_MODES),
        "description": "必填。field=客观字段值；article=按原文/标题检索文章；"
        "media=立绘/卡池等媒体；catalog=列目录（只列事实，不冒充正文）",
    },
    "subject": {
        "type": "object",
        "description": "查询对象；名称只作查询线索，最终对象由库内别名与证据确定，"
        "查不到就报未收录，不硬凑",
        "properties": {
            "name": {"type": "string", "description": "对象名或用户写法（可带俗称/括号版本名）"},
            "kind": {
                "type": "string",
                "enum": list(_WHAT_SUBJECT_KINDS),
                "description": "对象种类：traveler=旅人 / sigil=秘纹 / npc / monster / term=术语",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "shape": {
        "type": "string",
        "enum": list(_WHAT_SHAPES),
        "description": "答复形态：single=单值；list=多项；whole_article=整篇（要正文与正文图时用）",
    },
    "fields": {
        "type": "array",
        "items": {"type": "string"},
        "description": "mode=field 时的字段名：birthday/affiliation/weapon/salary/address/"
        "experience/skill.talent/sigil.main_effect/sigil.sub_effect/stat.<指标>/"
        "monster.weakness/age（age 无数据源，会明确答没有）",
    },
    "levels": {
        "type": "array",
        "items": {"type": "integer"},
        "description": "面板字段的等级，只能用该页实际存在的等级（常见 1/10/…/90）；"
        "传入不存在的等级会被告知可用等级，不杜撰数值",
    },
    "groups": {
        "type": "array",
        "items": {"type": "string"},
        "description": "mode=media 时的媒体分组（只能用库里真实存在的分组名）",
    },
    "conditions": {
        "type": "object",
        "description": "描述题的同一效果内条件（如 attribute=水系 / trigger=获得护盾时 / "
        "slot=主位）；不给 subject.name 时用它做同条检索",
        "additionalProperties": {"type": "string"},
    },
    "query": {
        "type": "string",
        "description": "mode=article 的检索文本（原文子串）；不填则用 query 参数的问题原文",
    },
    "literal": {
        "type": "string",
        "description": "mode=article 需要逐字命中的原文片段（跨标点假命中会被原文复核剔除）",
    },
}



class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（Runner 强制要求 plugin.config_version）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default="1.5.0",
        description="配置版本（1.5.0=新增 [what] 资料查询开关与 stale_warn_hours；"
        "1.4.2=新增 [how] 攻略来源开关与 [leaderboard] proxy；"
        "1.4.1=新增 [leaderboard] render_images 开关）",
    )


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


class LeaderboardConfig(PluginConfigBase):
    """排行榜命令（/st_lb）配置。"""

    __ui_label__ = "排行榜"
    __ui_icon__ = "trophy"
    __ui_order__ = 3

    ttl_minutes: int = Field(
        default=10,
        description="排行榜瘦身缓存有效期（分钟）：有效期内 /st_bb・/st_fe 零网络直读本地缓存；"
        "上游约每 5 分钟滚动更新一次，0=每次触发都重新拉取",
    )
    render_images: bool = Field(
        default=True,
        description="/st_bb・/st_fe 发送形态：开启=渲染图片榜单（合并转发）；"
        "关闭=纯文字榜单（1.4.x 旧版合并转发格式：每 25 名一节点）",
    )
    proxy: str = Field(
        default="",
        description="仅用于 ssleaderboard 上游拉取；留空强制直连。GameKee 和 B 站不受此设置影响。",
    )


class GameKeeConfig(PluginConfigBase):
    """GameKee 攻略源配置（1.5.0 新源）。"""

    __ui_label__ = "攻略源（GameKee）"
    __ui_icon__ = "book-open"
    __ui_order__ = 4

    index_ttl_minutes: int = Field(
        default=360,
        description="攻略索引有效期（分钟）：查询时索引年龄超过此值先刷新再匹配"
        "（绑定 last_verified，六页无变化的探查也推进）",
    )

    prefer_webp: bool = Field(
        default=True,
        description="攻略图片使用 CDN webp 转换（同尺寸约 1/9 体积）；关闭发原图 PNG（约 2MB/张，发送慢）",
    )


class HowConfig(PluginConfigBase):
    """攻略查询（stellasora_how）配置：攻略来源选择。"""

    __ui_label__ = "攻略查询"
    __ui_icon__ = "search"
    __ui_order__ = 5

    source: Literal["gamekee", "bili", "all"] = Field(
        default="gamekee",
        description="攻略来源：gamekee=GameKee 中文 wiki（含队伍码）；"
        "bili=猫猫球 B 站一图流（铃儿的猫猫球）；all=两边都查，发可用源",
    )


class WhatConfig(PluginConfigBase):
    """资料查询（stellasora_what）配置：默认关闭，开关只开通查询能力。"""

    __ui_label__ = "资料查询"
    __ui_icon__ = "database"
    __ui_order__ = 6

    enabled: bool = Field(
        default=False,
        description="资料查询工具（stellasora_what）是否向 planner 暴露：关闭=零 schema、"
        "零网络、零后台任务；开启只开通查询，不会自动抓取，建库/更新仍需操作员执行 /st_update",
    )
    stale_warn_hours: int = Field(
        default=168,
        description="资料库时效提示阈值（小时）：已发布代距 last_verified 超过此值只在答复里"
        "提示「资料可能过时」，不触发任何自动更新",
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
    leaderboard: LeaderboardConfig = Field(default_factory=LeaderboardConfig)
    gamekee: GameKeeConfig = Field(default_factory=GameKeeConfig)
    overrides: OverridesConfig = Field(default_factory=OverridesConfig)
    how: HowConfig = Field(default_factory=HowConfig)
    what: WhatConfig = Field(default_factory=WhatConfig)


class StellaSoraPlugin(MaiBotPlugin):
    """星塔旅人攻略查询插件。"""

    config_model = StellaSoraConfig

    @property
    def config(self) -> StellaSoraConfig:
        """强类型配置访问。

        基类属性标注为 PluginConfigBase（SDK 文档注明实际返回强类型配置实例），
        此处协变收窄为 StellaSoraConfig，使静态检查可直接访问 plugin/query/
        overrides 等分区字段；纯静态 cast，运行时行为与基类一致。
        """
        return cast(StellaSoraConfig, super().config)

    def __init__(self) -> None:
        super().__init__()
        self._data_dir: Path | None = None
        self._update_task: asyncio.Task[None] | None = None  # /st_update 后台任务（兼作在飞判定与强引用）
        self._overrides_fp: tuple[tuple[str, str], ...] | None = None  # 别名配置指纹（跳过热重装）
        self._session_identity_cache: dict[str, tuple[str, str]] = {}  # session_id → (group_id, user_id)
        self._session_platform_cache: dict[str, str] = {}  # session_id → platform（P3 执行层校验源）
        # GameKee 探查状态（P2）：三个时间戳 + single-flight 批次 + 内存索引缓存
        self._gamekee_state: Dict[str, Any] = {}  # state.json 的内存镜像
        self._gamekee_index: Optional[Dict[str, Any]] = None  # 已发布索引（整批成功才更新）
        self._gamekee_batch: Optional[asyncio.Future] = None  # single-flight 在飞批次（并发入口共享）
        self._gamekee_executor_task: Optional[asyncio.Task] = None  # 批次执行体（独立于入口，取消入口不中断它）
        self._gamekee_probe_task: asyncio.Task[None] | None = None  # 后台到期探查任务
        self._gamekee_probe_waiter: asyncio.Future | None = None  # 启动延迟可被手动更新取消
        self._gamekee_waiter_phase: str = ""  # waiter 阶段标记（boot=可接管 / normal=保持排程）
        self._gamekee_commit_lock = threading.Lock()  # 提交与卸载互斥（晚写守卫，P2）
        self._gamekee_probe_generation = 0  # 卸载/重载代际标记：旧线程结果不得发布
        # 榜单渲染复用：key=(sid, 数据指纹) → 分页 PNG base64 列表。精确时间戳让
        # 同数据渲染出同图，TTL 内重复 /st_bb 直接复用上次结果。只留最近一次。
        self._lb_render_cache: Optional[tuple[str, list[str]]] = None
        # 上一次生效的攻略来源：宿主先更新配置再调 on_config_update，
        # 旧值只能靠实例自己记，用于判断"是否需要启停 P2 任务"。
        self._last_how_source: str = ""
        # 资料查询（1.5.3）：/st_update 资料同步独立一路，句柄与取消信号各自持有。
        # 首次 await 前登记（重复触发只看句柄，不启第二份）；取消信号是 threading.Event，
        # 因为刷新 worker 在 to_thread 里轮询它（asyncio.Event 不能跨线程 await）。
        self._what_update_task: asyncio.Task[None] | None = None
        self._what_update_stop = threading.Event()
        self._what_update_progress: Dict[str, Any] = {}  # worker 线程写入的最新进度（重复触发时回报）
        self._what_probe_gate: asyncio.Event | None = None  # 短探查优先闸门：探查段结束才放行长批
        self._what_component_state: bool | None = None  # 已同步的组件启用态（None=未知，需重试）

    # ===== 生命周期 =====

    def _apply_overrides_config(self, *, force: bool = False) -> None:
        """别名配置指纹维护（1.5.0：别名由 match_guides 直接读 self.config）。

        保留指纹机制以兼容 on_config_update 触发路径；别名消费已改为查询时
        从 config 实时构建（_resolve_guide_aliases），此处仅维护指纹字段。
        """
        try:
            fingerprint = tuple(
                (entry.alias, entry.official) for entry in self.config.overrides.aliases
            )
            if not force and self._overrides_fp is not None and fingerprint == self._overrides_fp:
                return
            self._overrides_fp = fingerprint
        except Exception as exc:
            self.ctx.logger.warning("应用 overrides 别名配置失败: %s", exc)

    async def on_load(self) -> None:
        """插件加载：配置数据/缓存目录，按攻略来源决定是否启动 GameKee 后台探查。

        持久数据（GameKee state/index、榜单缓存）写宿主分配的 ctx.paths.data_dir，
        网络缓存与图片缓存写 ctx.paths.runtime_dir（非持久，可随时重建）。
        """
        self._data_dir = Path(self.ctx.paths.data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lb_data_dir_ready()
        # GameKee 探查：读持久状态 → 内存捡索引快照 → 到期则延迟 60s 后台探查
        #（延迟避开宿主 reload 等待窗口；延迟期间收到手动 /st_update 即取消）
        self._load_gamekee_state()
        self._load_gamekee_index_snapshot()
        self._last_how_source = self.config.how.source
        self._ensure_probe_task_if_enabled()
        await self._sync_what_component()
        self._log_what_library_state()
        self.ctx.logger.info("星塔旅人插件已加载，数据目录: %s", self._data_dir)

    def _log_what_library_state(self) -> None:
        """开启资料查询时只读地报一次本地已发布代是否存在（不建库、不访问网络）。"""
        if not self._what_enabled():
            return
        if (self._what_root() / "current.json").exists():
            self.ctx.logger.info("资料查询已开启，本地资料库指针存在: %s", self._what_root())
        else:
            self.ctx.logger.info(
                "资料查询已开启，但本地还没有已发布资料库（需操作员执行 /st_update）: %s",
                self._what_root(),
            )

    # ===== 资料查询（1.5.3）：开关 / 组件状态 / 取消 =====

    def _what_enabled(self) -> bool:
        """资料查询是否开启（默认关闭：零 schema 暴露、零网络、零后台任务）。"""
        return bool(self.config.what.enabled)

    def _what_root(self) -> Path:
        """资料库根目录（data_dir/what，与 what_library 约定的唯一落盘位置）。"""
        return Path(self.ctx.paths.data_dir) / "what"

    async def _sync_what_component(self) -> None:
        """按 [what] enabled 同步 TOOL 组件启用态（桌面保存后的热切换）。

        静态 `@Tool(enabled=False)` 只决定初始状态；宿主改配置后必须显式
        enable/disable 才会真正进入/退出 planner 的工具清单。失败只告警不抛出：
        组件状态是暴露层增强，执行层 `handle_what` 与 Hook 前置过滤各自还有闸，
        加载/保存配置不得因一次能力 RPC 失败而中断。
        """
        desired = self._what_enabled()
        if self._what_component_state is desired:
            return
        full_name = f"{self.ctx.plugin_id}.{_WHAT_TOOL}"
        toggle = (
            self.ctx.component.enable_component
            if desired
            else self.ctx.component.disable_component
        )
        try:
            result = await toggle(full_name, "TOOL")
        except Exception as exc:
            self._what_component_state = None
            self.ctx.logger.warning("资料查询组件状态同步失败（下次配置变更重试）: %s", exc)
            return
        ok = result is True or (isinstance(result, dict) and bool(result.get("success")))
        if ok:
            self._what_component_state = desired
            self.ctx.logger.info("资料查询组件已同步为 %s（%s）", "启用" if desired else "关闭", full_name)
            return
        self._what_component_state = None
        detail = result.get("error") if isinstance(result, dict) else result
        self.ctx.logger.warning("资料查询组件状态同步未成功: %s（full_name=%s）", detail, full_name)

    def _stop_what_update(self) -> None:
        """置取消信号：正在跑的刷新 worker 每篇检查一次后收敛，不晚写指针。"""
        self._what_update_stop.set()


    def _gamekee_enabled(self) -> bool:
        """当前攻略来源是否需要 GameKee（bili 单源时后台流量应当为零）。"""
        return self.config.how.source != "bili"

    def _ensure_probe_task_if_enabled(self) -> None:
        """按当前来源启用 P2 后台探查任务（唯一的"创建点"入口）。

        只有 bili 单源才跳过：GameKee 自动流量（24h 探查 + 60s 启动探查）在
        纯猫猫球模式下没有意义。/st_update 手动探查不受此门控（用户显式操作）。
        """
        if not self._gamekee_enabled():
            self.ctx.logger.info("攻略来源为猫猫球（bili），不启动 GameKee 后台探查")
            return
        if self._gamekee_probe_task is not None and not self._gamekee_probe_task.done():
            return  # 已有在飞任务，不重复创建
        self._gamekee_probe_task = asyncio.create_task(self._schedule_gamekee_probe())

    async def on_unload(self) -> None:
        """插件卸载：取消全部后台任务并清空运行时缓存引用（文件留给磁盘回收）。

        代际自增先行：在飞的探查批次结果被判 stale 不得发布；手动 /st_update
        任务（1.5.0 已不自 reload）同样取消并等待，防止卸载后旧任务继续刷榜或
        重新排程。
        """
        # 代际自增需与提交线程互斥（晚写守卫）；锁可能被慢盘写持有一段，
        # 用 to_thread 获取避免阻塞事件循环（AGENTS.md 事件循环规范）
        await asyncio.to_thread(self._bump_generation_locked)
        if self._gamekee_probe_task and not self._gamekee_probe_task.done():
            self._gamekee_probe_task.cancel()
            await asyncio.gather(self._gamekee_probe_task, return_exceptions=True)
        self._gamekee_probe_task = None
        # 批次执行体独立于入口任务：卸载时等它收敛（代际已自增，其结果不会发布）
        executor = self._gamekee_executor_task
        if executor is not None and not executor.done():
            await asyncio.gather(executor, return_exceptions=True)
        self._gamekee_executor_task = None
        self._gamekee_batch = None
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            await asyncio.gather(self._update_task, return_exceptions=True)
        self._update_task = None
        # 资料同步先置取消信号再取消任务：worker 在 to_thread 里轮询该信号，
        # 取消协程不等同于中止线程，只有信号能阻止它继续抓取并提交新代。
        self._stop_what_update()
        if self._what_update_task and not self._what_update_task.done():
            self._what_update_task.cancel()
            await asyncio.gather(self._what_update_task, return_exceptions=True)
        self._what_update_task = None
        self._what_probe_gate = None
        self._lb_render_cache = None
        self.ctx.logger.info("星塔旅人插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        """配置热重载：黑白名单/GameKee 参数即时生效；攻略来源变化时管理调度任务。

        宿主保存配置 → `plugin.config_updated` → 本方法被调；runner 侧
        `_apply_plugin_config` **先于**本方法更新实例配置，故进来时
        `self.config.how.source` 已是新值，"旧值"取自 `_last_how_source`。
        插件各处 `self.config.x` 都是调用时现读，唯后台调度任务需要显式管理。

        切换只动**调度任务**，不打断已发出的探查请求：调度任务里的探查调用经
        `_run_probe_detached` shield 包裹，取消调度不会取消共享批次，已加入批次
        的等待者（如并发的手动更新）照常拿到同一结果。to_thread 里已发出的同步
        请求无法真正中止，这一点由单飞终态纪律自愈。
        """
        self.ctx.logger.info(
            "配置已更新: scope=%s version=%s（黑白名单与自定义覆盖即时生效）", scope, version
        )
        self._apply_overrides_config()
        # [what] 开关的组件同步必须走在下面 `how.source` 相同的提前 return 之前：
        # 只改资料查询开关时 how 来源没变，提前 return 会把热切换整体吞掉。
        await self._sync_what_component()
        if not self._what_enabled():
            # 关闭即停：正在跑的资料同步立即收敛（信号由 worker 线程轮询），
            # 否则关闭后仍可能抓完并切换指针，等于「关了还在后台做事」。
            self._stop_what_update()
            task = self._what_update_task
            if task is not None and not task.done():
                task.cancel()
            self._what_update_task = None
        elif self._what_update_task is None:
            # 重新打开后取消信号必须复位，否则下一轮同步开工即自我取消。
            self._what_update_stop = threading.Event()

        source = self.config.how.source
        previous_source = self._last_how_source
        if source == previous_source:
            return
        self._last_how_source = source
        self.ctx.logger.info("攻略来源已切换为 %s（原 %s）", source, previous_source)
        if source == "bili":
            # 切入猫猫球单源：停调度不再排新的探查；在飞批次继续跑完（见方法注释）
            task = self._gamekee_probe_task
            if task is not None and not task.done():
                task.cancel()
            self._gamekee_probe_task = None
        else:
            # 切回 gamekee/all：重建调度任务
            self._ensure_probe_task_if_enabled()


    def _lb_data_dir_ready(self) -> Path:
        """榜单数据目录（data_dir/leaderboard，1.5.0 起活动榜单缓存；旧 offline/ssleaderboard 不再读写）。

        瘦身缓存/meta/season 均落此目录；目录即建（首次查询时写入）。
        """
        d = Path(self.ctx.paths.data_dir) / "leaderboard"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ===== GameKee 探查状态（P2：三时间戳 / 三入口共用 gate / 整批发布）=====

    # 探查失败后的后台重试间隔（秒）：从**失败完成时刻**起算（而非尝试开始——
    # 长失败请求不吞噬等待期），既不断停摆（到重试点必然再试），
    # 也不因旧截止时间已过而连拉（每次失败后至少等满该间隔）
    _GAMEKEE_RETRY_INTERVAL = 30 * 60
    # 加载后首个探查的延迟（秒）：避开宿主 reload 等待窗口
    _GAMEKEE_BOOT_DELAY = 60
    # 每日探查间隔（秒）：距 last_verified ≥24h 触发后台探查
    _GAMEKEE_PROBE_INTERVAL = 24 * 3600

    def _gamekee_state_path(self) -> Path:
        """探查状态文件（data_dir/gamekee/state.json，持久跨 reload）。"""
        return Path(self.ctx.paths.data_dir) / "gamekee" / "state.json"

    def _gamekee_index_path(self) -> Path:
        """索引快照文件（data_dir/gamekee/index.json，整批成功才覆盖）。"""
        return Path(self.ctx.paths.data_dir) / "gamekee" / "index.json"

    def _load_gamekee_state(self) -> None:
        """加载持久探查状态到内存（损坏/缺失按空状态起步，不猜字段）。"""
        try:
            raw = json.loads(self._gamekee_state_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._gamekee_state = raw
                return
        except FileNotFoundError:
            pass  # 首次运行无状态文件：空状态起步（正常路径）
        except (json.JSONDecodeError, OSError) as exc:
            self.ctx.logger.warning("GameKee 状态文件读取失败（按空状态起步）: %s", exc)
        self._gamekee_state = {}

    def _save_gamekee_state_sync(self) -> None:
        """状态原子落盘同步体（仅供 to_thread 调用；OSError 上抛由调用方记日志）。"""
        self._gamekee_state_path().parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_gamekee(
            self._gamekee_state_path(),
            json.dumps(self._gamekee_state, ensure_ascii=False, indent=2),
        )

    def _save_gamekee_state_sync_commit(self, generation: int) -> None:
        """状态提交同步体：锁内核验代际 → 匹配才 rename 提交（与发布同款互斥）。"""
        with self._gamekee_commit_lock:
            target = self._gamekee_state_path()
            staging = target.with_name(f".{target.stem}.gen{generation}.{time.time_ns()}.tmp")
            payload = json.dumps(self._gamekee_state, ensure_ascii=False, indent=2)
            staging.write_text(payload, "utf-8")
            if generation != self._gamekee_probe_generation:
                staging.unlink(missing_ok=True)
                return
            staging.replace(target)

    async def _save_gamekee_state_async(self) -> None:
        """状态落盘（to_thread 提交体 + 代际核验内联）；OSError 上抛由调用方决定语义。"""
        generation = self._gamekee_probe_generation
        await asyncio.to_thread(self._save_gamekee_state_sync_commit, generation)

    def _load_gamekee_index_snapshot(self) -> None:
        """内存捡上次发布的索引快照（reload 后不重新建索引即可服务查询）。

        快照损坏/缺失保持 None——懒刷新入口会因"索引不可用"触发探查重建。
        """
        try:
            raw = json.loads(self._gamekee_index_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("characters"), list):
                self._gamekee_index = raw
                self.ctx.logger.info(
                    "GameKee 索引快照已捡入内存（%d 小节，built_at=%s）",
                    len(raw["characters"]), raw.get("built_at"),
                )
                return
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.ctx.logger.warning("GameKee 索引快照读取失败（视为需重建）: %s", exc)
        self._gamekee_index = None

    def _publish_gamekee_index_sync(self, index: Dict[str, Any], generation: int) -> bool:
        """索引提交同步体：锁内核验代际 → 匹配才 rename 提交 → 提交后二次核验。

        卸载竞态纪律：核验与 rename 在 threading.Lock 内与卸载线程互斥——
        卸载的代际自增要么发生在核验前（本批被丢弃），要么发生在提交后
        （提交后二次核验发现更替，回滚内存引用；正式文件若已被新实例覆盖，
        新实例的写是原子 rename，本线程不会覆盖它）。两次核验都在锁内，
        不存在跨 await/跨锁的窗口。
        """
        with self._gamekee_commit_lock:
            target = self._gamekee_index_path()
            staging = target.with_name(f".{target.stem}.gen{generation}.{time.time_ns()}.tmp")
            payload = json.dumps(index, ensure_ascii=False)
            staging.write_text(payload, "utf-8")
            if generation != self._gamekee_probe_generation:
                staging.unlink(missing_ok=True)
                return False
            staging.replace(target)
            # 提交后二次核验：若卸载/重载发生在 rename 之后（同锁内不会，但
            # 防御未来改动），恢复代际一致性——内存引用交给新实例决定
            if generation != self._gamekee_probe_generation:
                return False
            return True

    def _bump_generation_locked(self) -> None:
        """代际自增（须持提交锁）；独立同步体供 on_unload 经 to_thread 调用。"""
        with self._gamekee_commit_lock:
            self._gamekee_probe_generation += 1

    async def _publish_gamekee_index(self, index: Dict[str, Any]) -> None:
        """整批校验成功后发布索引（to_thread 提交体 + 代际核验内联）。"""
        generation = self._gamekee_probe_generation
        try:
            committed = await asyncio.to_thread(
                self._publish_gamekee_index_sync, index, generation
            )
        except OSError as exc:
            self.ctx.logger.warning("GameKee 索引落盘失败（内存快照仍生效，下次探查重建）: %s", exc)
            if generation == self._gamekee_probe_generation:
                self._gamekee_index = index
            return
        if not committed:
            self.ctx.logger.info("GameKee 索引写盘后实例已更替，丢弃过期批次")
            return
        self._gamekee_index = index

    @staticmethod
    def _now() -> float:
        """时间源（单点便于测试注入可控时钟）。"""
        return time.time()

    def _gamekee_due_delay(self) -> float:
        """距下次后台探查的秒数（P2 排程核心）。

        正常调度 = last_verified + 24h；失败重试 = max(last_verified, last_failure_at)
        + 30min（last_failure_at 是失败**完成**时刻——长失败请求不吞噬等待期）。
        首次（无状态）= 0（立即进入 60s 启动延迟探查）。
        """
        now = self._now()
        last_verified = float(self._gamekee_state.get("last_verified") or 0)
        last_failure = float(self._gamekee_state.get("last_failure_at") or 0)
        if not last_verified and not last_failure:
            return 0.0
        # 失败后（last_failure_at 存在且晚于 last_verified）以重试点为准；
        # 成功后（verified >= failure）以 24h 为准。
        if last_failure > last_verified:
            due_soonest = last_failure + self._GAMEKEE_RETRY_INTERVAL
        else:
            due_soonest = last_verified + self._GAMEKEE_PROBE_INTERVAL
        return max(0.0, due_soonest - now)

    async def _schedule_gamekee_probe(self) -> None:
        """后台到期探查任务（唯一后台排程体；手动入口通过共用 gate 复用）。

        启动延迟 60s 内收到手动更新会取消本次排程（_wait_with_reset 以事件驱动）；
        探查完成后按结果重排下一次。单轮探查失败**不退出循环**——失败走
        30min 重试排程（P2 纪律），只有手动接管或卸载才让路/退出。
        """
        try:
            probe_now = False  # 到期重核后的"立即探查"旗标
            while True:
                if probe_now:
                    # 内层等待确认新截止点已到：直接探查（跳过循环头 due_delay——
                    # 它可能混入手动失败的重试点，与成功节奏语义冲突）
                    probe_now = False
                    try:
                        await self._run_gamekee_probe(trigger="scheduled")
                    except GameKeeProbeFailure:
                        continue  # 失败回循环头按 attempt/failure+30min 重算
                    continue  # 成功回循环头按 last_verified+24h 重算
                delay = self._gamekee_due_delay()
                # phase 语义：首次（无任何时间戳）的 60s 启动延迟 = boot（可被手动接管）；
                # 其余轮次的到期点等待 = normal（手动不接管，保持原排程）
                is_boot = not self._gamekee_state
                wait_total = max(delay, 0) + (self._GAMEKEE_BOOT_DELAY if is_boot else 0)
                self.ctx.logger.info(
                    "GameKee 探查排程：%.0f 秒后到期（phase=%s）", delay, "boot" if is_boot else "normal"
                )
                # 可取消等待：boot 阶段被手动 /st_update 接管时让路退出，
                # 由手动入口执行探查并在完成后重排本任务
                woke_by_reset = await self._wait_cancellable(wait_total, "boot" if is_boot else "normal")
                if woke_by_reset:
                    self.ctx.logger.info("GameKee 探查排程被手动更新接管，让路退出")
                    return
                # 自然到期唤醒：先重核**成功节奏**——手动成功已推进 last_verified 时
                # （如距原截止仅 1h 时手动更新），新 24h 截止恢复，继续等待而非立即
                # 探查（P2：last_verified 是唯一成功节奏基准）。
                # 刻意不读 last_failure_at：正常等待期间的手动失败**不影响原排程**
                # （计划 P2 恢复规则——只有接管既有排程失败才切重试时间）；
                # 失败重试点仅由后台任务自身失败（本循环 except 分支）触发。
                last_verified_now = float(self._gamekee_state.get("last_verified") or 0)
                success_remaining = last_verified_now + self._GAMEKEE_PROBE_INTERVAL - self._now()
                if success_remaining > 0:
                    # 手动成功已推进成功节奏：原地精确等到新 24h 截止点（可取消、
                    # normal 阶段手动不接管），**不回循环头**——循环头的 due_delay
                    # 会混入 last_failure_at 的 30min 重试点，造成"截止点已过就
                    # 空唤醒一次再被挡回"的抖动循环。
                    self.ctx.logger.info(
                        "GameKee 排程到期但 last_verified 已被手动成功推进（剩 %.0f 秒），原地等待新截止点",
                        success_remaining,
                    )
                    woke_again = await self._wait_cancellable(success_remaining, "normal")
                    if woke_again:
                        self.ctx.logger.info("GameKee 排程等待期被手动更新接管，让路退出")
                        return
                    # 等待结束：再次核对最新成功节奏——期间又有手动成功时
                    # last_verified 已再推进，须按**最新**成功时间重算剩余量继续
                    # 原地等（不立即探查）；只有最新 24h 截止点真实到期才探查
                    last_verified_after = float(self._gamekee_state.get("last_verified") or 0)
                    after_remaining = last_verified_after + self._GAMEKEE_PROBE_INTERVAL - self._now()
                    if after_remaining > 0:
                        self.ctx.logger.info(
                            "GameKee 等待期内又有手动成功（剩 %.0f 秒），继续等待新截止点", after_remaining
                        )
                        while True:
                            woke_third = await self._wait_cancellable(after_remaining, "normal")
                            if woke_third:
                                self.ctx.logger.info("GameKee 排程等待期被手动更新接管，让路退出")
                                return
                            last_verified_final = float(self._gamekee_state.get("last_verified") or 0)
                            final_remaining = last_verified_final + self._GAMEKEE_PROBE_INTERVAL - self._now()
                            if final_remaining > 0:
                                after_remaining = final_remaining  # 又被推进：继续等新截止点
                                continue
                            break  # 最新截止点真实到期
                    # 最新成功节奏的 24h 截止点已真实到期：探查（不经循环头）
                    probe_now = True
                    continue
                try:
                    await self._run_gamekee_probe(trigger="scheduled")
                except GameKeeProbeFailure:
                    # 失败不退出：回到循环头按 attempt/failure+30min 重算重试点（P2）
                    continue
                # 成功后回到循环头重算延迟（last_verified+24h）
        except asyncio.CancelledError:
            self.ctx.logger.info("GameKee 后台探查任务已取消并优雅退出")

    async def _wait_cancellable(self, seconds: float, phase: str) -> bool:
        """可被手动更新打断的等待。

        phase: "boot"=启动延迟（可被手动接管让路）；"normal"=正常等到期点
        （手动不接管——保持原排程，手动失败不影响 24h 截止点）。
        返回 True=被接管（本任务应让路退出）。
        """
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._gamekee_probe_waiter = waiter
        self._gamekee_waiter_phase = phase
        try:
            await asyncio.wait_for(waiter, timeout=max(0.1, seconds))
            return True  # _reset_probe_waiter set() 了它
        except asyncio.TimeoutError:
            return False  # 自然到点
        except asyncio.CancelledError:
            raise
        finally:
            if self._gamekee_probe_waiter is waiter:
                self._gamekee_probe_waiter = None
                self._gamekee_waiter_phase = ""

    def _reset_probe_waiter(self) -> None:
        """手动 /st_update 接管排程：仅唤醒**启动延迟**中的任务（正常等待不动）。"""
        if self._gamekee_waiter_phase != "boot":
            return  # 正常等待期：保持原排程，手动探查独立执行
        waiter = self._gamekee_probe_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def _background_in_boot_delay(self) -> bool:
        """后台任务是否处于启动延迟等待（phase=="boot" 且 waiter 挂着）。

        只有这种状态下手动探查才接管让路；正常等待期（等到自己到期点）不动。
        """
        waiter = self._gamekee_probe_waiter
        return self._gamekee_waiter_phase == "boot" and waiter is not None and not waiter.done()

    def _bump_probe_attempt(self) -> None:
        """每次探查尝试后推进 last_probe_attempt（无论成败；落盘失败只丢时机不丢内存态）。"""
        self._gamekee_state["last_probe_attempt"] = self._now()
        try:
            self._save_gamekee_state_sync()
        except OSError as exc:
            self.ctx.logger.warning("GameKee 状态写入失败: %s", exc)

    async def _run_gamekee_probe(self, trigger: str) -> Dict[str, Any]:
        """执行一次 GameKee 探查/重建（三入口统一体，single-flight）。

        - 合并请求（single-flight）：并发入口共享同一 asyncio.Future——首个入口
          创建批次并真实探查，其余入口 await 同一 future 拿同一结果（成功复用索引、
          失败复用异常），不重复发起网络请求；
        - **批次执行体独立成 task**：任何入口（含后台调度任务）被取消时，批次
          继续跑到终态，其他等待者照常拿到同一结果——取消一个入口不等于取消
          已发出的网络请求（to_thread 里的同步请求本来也停不下来）；
        - batch 终态统一负责：成功 set_result、异常 set_exception——等待者永不挂起。
        """
        while True:
            batch = self._gamekee_batch
            if batch is not None:
                # 已有在飞批次：shield 包裹——本入口被取消不影响共享批次。
                # shield 内层异常被本协程接收时同步消费，防悬挂告警。
                try:
                    return await asyncio.shield(batch)
                except asyncio.CancelledError:
                    if batch.done() and not batch.cancelled() and batch.exception() is not None:
                        batch.exception()  # 已被本协程接收，消费防告警
                    raise
                except BaseException:
                    if batch.done() and not batch.cancelled() and batch.exception() is not None:
                        batch.exception()  # 同上
                    raise
            loop = asyncio.get_running_loop()
            batch: asyncio.Future = loop.create_future()
            self._gamekee_batch = batch
            # 执行体独立 task：入口协程被取消（卸载/来源切换）时它继续跑完，
            # 等待者不会拿到"被取消"而丢失已完成的抓取结果
            executor = asyncio.create_task(self._gamekee_probe_executor(trigger, batch))
            self._gamekee_executor_task = executor
            try:
                return await asyncio.shield(batch)
            except asyncio.CancelledError:
                # 本入口取消：批次仍在跑，保持 _gamekee_batch 供其他等待者加入
                raise
            finally:
                if self._gamekee_batch is batch and batch.done():
                    self._gamekee_batch = None
                if executor.done():
                    if self._gamekee_executor_task is executor:
                        self._gamekee_executor_task = None

    async def _gamekee_probe_executor(self, trigger: str, batch: asyncio.Future) -> None:
        """批次执行体：跑真实探查并把终态写进 batch（独立 task，不被入口取消波及）。"""
        try:
            result = await self._probe_batch_execute(trigger)
        except asyncio.CancelledError:
            if not batch.done():
                batch.set_exception(GameKeeProbeFailure("探查被取消"))
                batch.exception()  # 创建者消费，防悬挂告警
            raise
        except BaseException as exc:
            if not batch.done():
                failure = exc if isinstance(exc, GameKeeProbeFailure) else GameKeeProbeFailure(str(exc))
                batch.set_exception(failure)
                batch.exception()  # 无等待者时自行消费，防 asyncio 告警
            return
        if not batch.done():
            batch.set_result(result)
        if self._gamekee_batch is batch:
            self._gamekee_batch = None

    async def _probe_batch_execute(self, trigger: str) -> Dict[str, Any]:
        """single-flight 批次主体：轻量探查 → 无变化且 schema 最新仅推进时间戳，
        有变化或内存索引 schema 偏低才完整重建。

        schema 条件：`schema_version` 升级（如 v1→v2 增队伍码）与上游 updated_at
        无关，只靠"无变化跳过"会永久停留在旧快照（实机 v1 索引 → 迁移触发探查 →
        六页无变化 → 返回无 team_codes 的旧快照，攻略永远缺码）。故索引 schema
        偏低时即使 changed=False 也走完整重建，复用同一发布/落盘/回滚纪律。
        """
        generation = self._gamekee_probe_generation
        try:
            self._bump_probe_attempt()
            proxy_url = None  # 代理解析在 net_common 内按环境处理（与榜单一致）
            known = {
                element: int((self._gamekee_index or {}).get("pages", {}).get(element, {}).get("updated_at") or 0)
                for element in ("water", "fire", "light", "dark", "wind", "earth")
            } if self._gamekee_index else None
            # 1. 轻量探查：六页仅取 updated_at（整批校验；失败即整批失败）
            last_verified_before = float(self._gamekee_state.get("last_verified") or 0)
            probe = await asyncio.to_thread(gamekee_probe_updates, proxy_url, 20, known)
            if generation != self._gamekee_probe_generation:
                self.ctx.logger.info("GameKee 探查结果已过期（实例被卸载/重载），不发布")
                raise GameKeeProbeFailure("stale generation")
            current_schema = int((self._gamekee_index or {}).get("schema_version") or 0)
            schema_stale = current_schema < gamekee_index_schema_version()
            if not probe.get("changed") and not schema_stale:
                # 六页无变化且索引 schema 已最新：仅推进 last_verified，不重建（P2 探查语义）。
                # 顺序纪律：先落盘成功才推进内存——落盘失败内存不推进，
                # 手动更新报告"计时未重置"与实际一致。
                self._gamekee_state["last_verified"] = self._now()
                try:
                    await self._save_gamekee_state_async()
                except OSError as exc:
                    self._gamekee_state["last_verified"] = last_verified_before
                    self._gamekee_state["last_failure_at"] = self._now()
                    raise GameKeeProbeFailure(f"状态落盘失败: {exc}") from exc
                self.ctx.logger.info(
                    "GameKee 探查完成（trigger=%s）：六页无变化，索引保持（last_verified 已推进）", trigger
                )
                return self._gamekee_index or {}
            if schema_stale and not probe.get("changed"):
                self.ctx.logger.info(
                    "GameKee 索引 schema 迁移（trigger=%s）：%d → %d，上游无变化仍完整重建",
                    trigger, current_schema, gamekee_index_schema_version(),
                )
            # 2. 完整重建：上游有变化，或索引 schema 偏低（迁移，与上游无关）
            built_at_before = float(self._gamekee_state.get("index_built_at") or 0)
            index = await asyncio.to_thread(gamekee_build_index, proxy_url)
            if generation != self._gamekee_probe_generation:
                self.ctx.logger.info("GameKee 探查结果已过期（实例被卸载/重载），不发布")
                raise GameKeeProbeFailure("stale generation")
            await self._publish_gamekee_index(index)
            self._gamekee_state["last_verified"] = self._now()
            self._gamekee_state["index_built_at"] = int(index.get("built_at") or 0)
            try:
                await self._save_gamekee_state_async()
            except OSError as exc:
                # 状态落盘失败：内存回滚（计时未重置）+ 登记失败完成时刻
                #（转 ProbeFailure 绕过通用失败分支，须自行登记 30min 重试基准）
                self._gamekee_state["last_verified"] = last_verified_before
                self._gamekee_state["index_built_at"] = built_at_before
                self._gamekee_state["last_failure_at"] = self._now()
                raise GameKeeProbeFailure(f"状态落盘失败: {exc}") from exc
            self.ctx.logger.info(
                "GameKee 探查成功（trigger=%s）：%d 小节，已发布索引",
                trigger, len(index.get("characters") or []),
            )
            return index
        except asyncio.CancelledError:
            raise
        except GameKeeProbeFailure as exc:
            # 失败终态补到批次（等待者拿同一异常，不重拉不误成功）
            raise
        except BaseException as exc:
            # 失败完成时刻：30min 重试从此刻起算（非尝试开始）
            self._gamekee_state["last_failure_at"] = self._now()
            try:
                await self._save_gamekee_state_async()
            except OSError:
                pass
            self.ctx.logger.warning("GameKee 探查失败（trigger=%s）：%s", trigger, str(exc)[:200])
            # 失败终态补到批次（等待者拿同一异常，不重拉不误成功）
            raise GameKeeProbeFailure(str(exc)) from exc

    async def ensure_gamekee_index(self) -> Dict[str, Any]:
        """查询入口的懒刷新（P2 入口③）：绑定 last_verified 与 index_ttl。

        需要刷新（索引不可用 / last_verified 距今超过 index_ttl_minutes / 索引为
        旧 schema——schema 升级与上游 updated_at 无关，只等自然探查会永久缺码）
        时先探查；gate 保证并发查询共享同一次探查。

        探查失败时：有旧索引则带旧索引降级服务（并发等来的失败结果同样走这条
        旧快照路径，不隐式重试）；无旧索引则向上抛（handler 转用户可见"攻略源
        暂不可用"）。失败纪律与后台排程同源：`last_failure_at` 晚于 `last_verified`
        且距 30min 重试点未到时不再发起探查，直接带旧快照——否则每次查询都会重拉
        一次失败中的上游（排队刷屏时按分钟级放大）。成功后 `last_verified` 越过
        `last_failure_at`，纪律自动解除。
        """
        ttl_seconds = max(1, int(self.config.gamekee.index_ttl_minutes)) * 60
        last_verified = float(self._gamekee_state.get("last_verified") or 0)
        needs_refresh = (
            self._gamekee_index is None
            or not last_verified
            or (self._now() - last_verified) > ttl_seconds
            or int((self._gamekee_index or {}).get("schema_version") or 0) < gamekee_index_schema_version()
        )
        if not needs_refresh:
            return self._gamekee_index  # type: ignore[return-value]
        last_failure = float(self._gamekee_state.get("last_failure_at") or 0)
        if (
            self._gamekee_index is not None
            and last_failure > last_verified
            and last_failure + self._GAMEKEE_RETRY_INTERVAL > self._now()
        ):
            self.ctx.logger.info("GameKee 懒刷新处于失败重试间隔内，本次查询直接用旧快照")
            return self._gamekee_index
        try:
            return await self._run_gamekee_probe(trigger="lazy")
        except GameKeeProbeFailure:
            if self._gamekee_index is not None:
                self.ctx.logger.warning("GameKee 懒刷新失败，使用旧快照服务本次查询")
                return self._gamekee_index
            raise

    def _resolve_guide_aliases(self) -> Dict[str, str]:
        """config [overrides.aliases] → match_guides 别名表（alias→official）。"""
        return {
            entry.alias.strip(): entry.official.strip()
            for entry in self.config.overrides.aliases
            if entry.alias.strip() and entry.official.strip()
        }

    def _resolve_stream_id(self, kwargs: dict) -> str:
        """从工具调用 kwargs 提取 stream_id（直发/去重/未找到发送共用）。"""
        return (str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")).strip()

    def _denied(self, **kwargs) -> bool:
        """黑白名单鉴权。群聊看 group_id，私聊看 user_id。"""
        cfg = self.config.access_control
        return not check_permission(
            cfg.mode, cfg.whitelist, cfg.blacklist,
            group_id=str(kwargs.get("group_id") or ""),
            user_id=str(kwargs.get("user_id") or ""),
        )

    # 1.5.0 QQ 限定平台的短标签（插件仅适配 QQ 适配器；其他平台无发送回执保障）
    _SUPPORTED_PLATFORMS = frozenset({"qq"})

    async def _platform_allowed(self, **kwargs) -> bool:
        """执行层平台校验（P3）：当前流平台必须明确属于支持列表。

        平台来源优先级：宿主直传 platform 字段 → session_id/stream_id 命中
        `_session_platform_cache`（Hook 反查链路填充）→ 有 session_id 时主动
        反查一次（直调工具不经 Hook，缓存可能是冷的）。全 miss 即未知平台，
        保守拒绝（宁可漏不可错）：非支持平台的调用不发起任何网络抓取。
        """
        explicit = str(kwargs.get("platform") or "").strip().lower()
        if explicit:
            return explicit in self._SUPPORTED_PLATFORMS
        session_id = str(kwargs.get("session_id") or "").strip()
        cache_key = session_id or self._resolve_stream_id(kwargs)
        cached = self._session_platform_cache.get(cache_key) if cache_key else None
        if cached is None and session_id:
            # 主动反查（顺带填充 group/user 身份缓存）；失败/未命中视为未知
            await self._resolve_session_identity(session_id)
            cached = self._session_platform_cache.get(session_id)
        session_platform = str(cached or "").strip().lower()
        if session_platform:
            return session_platform in self._SUPPORTED_PLATFORMS
        # 平台未知：保守拒绝
        return False

    async def _resolve_session_identity(self, session_id: str) -> tuple[str, str] | None:
        """session_id → (group_id, user_id, platform)，供 Hook 复现授权判定。

        payload 只有 MD5 形态的 session_id（不可反解），经宿主 chat.get_all_streams
        反查；缓存命中前每个 planner 内部轮都会触发本 hook，必须落缓存避免每轮
        全量 RPC。查询失败/未命中返回 None——调用方 fail-open 放行，漏拦由
        handler 内 _denied/_platform_allowed 兜底（查询故障不得把授权聊天的工具弄丢）。
        返回值第二处消费：platform 写入 _session_platform_cache，供执行层
        _platform_allowed 使用（1.5.0 QQ 限定）。
        """
        if not session_id:
            return None
        try:
            raw = await self.ctx.chat.get_all_streams()
            # SDK 代理按解包表（context.py "chat.get_all_streams": "streams"）把宿主
            # 结果解成裸 list——按解包后契约消费，其余形状视为查询失败走 fail-open
            stream_list = raw if isinstance(raw, list) else []
            for stream in stream_list:
                if not isinstance(stream, dict) or stream.get("session_id") != session_id:
                    continue
                identity = (str(stream.get("group_id") or ""), str(stream.get("user_id") or ""))
                if len(self._session_identity_cache) >= MAX_SESSION_CACHE_ENTRIES:
                    self._session_identity_cache.clear()
                self._session_identity_cache[session_id] = identity
                platform = str(stream.get("platform") or "").strip().lower()
                if len(self._session_platform_cache) >= MAX_SESSION_CACHE_ENTRIES:
                    self._session_platform_cache.clear()
                self._session_platform_cache[session_id] = platform
                return identity
        except Exception as exc:
            self.ctx.logger.warning("Hook 解析会话身份失败（fail-open 放行）: %s", exc)
            return None
        return None

    @HookHandler(
        "maisaka.planner.before_request",
        name="stellasora_planner_tool_gate",
        description="按黑白名单从 planner 工具清单中剔除星塔旅人工具",
        mode=HookMode.BLOCKING,
    )
    async def _filter_planner_tools(
        self, tool_definitions: list | None = None, session_id: str = "", **kwargs
    ) -> dict:
        """未授权/非平台聊天的 planner 工具清单前置拦截：剔除即零 token 暴露。

        P3 两层口径：**平台 + 黑白名单双判定**——仅当确认为 QQ 平台且通过名单
        才保留工具；非 QQ、反查失败（未知平台/身份）一律剔除（保守剔除与执行层
        同口径，宁可漏不可错）。deferred 提醒文本不经本 hook，残留一行精简提醒
        属已知边界；执行层另有 handler 的 _denied/_platform_allowed 兜底。
        """
        defs = tool_definitions if isinstance(tool_definitions, list) else []
        if not defs:
            return {"action": "continue"}
        identity = self._session_identity_cache.get(session_id)
        platform = self._session_platform_cache.get(session_id)
        if identity is None or platform is None:
            identity = await self._resolve_session_identity(session_id)
            platform = self._session_platform_cache.get(session_id)
        # 反查失败（身份或平台未知）：保守剔除
        if identity is None or not platform:
            return self._strip_tool_definitions(defs, kwargs, session_id)
        group_id, user_id = identity
        if not str(platform).strip().lower() in self._SUPPORTED_PLATFORMS:
            return self._strip_tool_definitions(defs, kwargs, session_id)
        if self._denied(group_id=group_id, user_id=user_id):
            return self._strip_tool_definitions(defs, kwargs, session_id)
        if not self._what_enabled():
            # 授权聊天但资料查询关闭：只剔除 what，how 照常保留。宿主侧组件已
            # disable，这里再剔一次使「默认关闭」在 Hook 层也是零 schema（不靠
            # 单点状态，两条路径任一失效都不出站）。
            return self._strip_tool_definitions(defs, kwargs, session_id, names=frozenset({_WHAT_TOOL}))
        return {"action": "continue"}

    def _strip_tool_definitions(
        self,
        defs: list,
        kwargs: dict,
        session_id: str,
        *,
        names: frozenset[str] | None = None,
    ) -> dict:
        """从工具定义清单剔除指定插件工具并回填 modified_kwargs（整体替换语义）。"""
        targets = names if names is not None else _PLUGIN_TOOL_NAMES
        kept = [
            d for d in defs
            if not isinstance(d, dict) or d.get("function", {}).get("name") not in targets
        ]
        if len(kept) == len(defs):
            return {"action": "continue"}
        # modified_kwargs 为整体替换语义：session_id/tool_definitions 被形参声明吃掉，
        # 必须显式补回，保证宿主消费端拿全 8 键
        modified = {**kwargs, "session_id": session_id, "tool_definitions": kept}
        return {"action": "continue", "modified_kwargs": modified}

    # ===== Tool 组件 =====

    @Tool(
        "stellasora_how",
        description="查询星塔旅人一图流攻略：返回角色或属性的开局/配队/纹章/秘纹攻略图"
                    "（GameKee 攻略组或 B站@铃儿的猫猫球，按配置的攻略来源选取，均授权转载）。"
                    "'XX攻略/XX怎么玩/风队怎么玩'调用本工具。"
                    "角色客观资料（属性/技能/素材）不在本工具范围。",
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="必填，非空。填写用户的问题原文或角色名（如'艾蕾攻略'/'风队怎么玩'/'春科直伤篇'）；"
                "支持别名与属性泛查（水/火/光/暗/风/地 + 队/系）",
                required=True,
            ),
        ],
        platforms="qq",
        # 宿主 1.4.x 起 planner 只把 visibility=visible 的插件工具放进原生 tools
        # 数组（其余仅可经 tool_search 发现且 schema 不出站，实测模型发不出带参
        # 调用）；未声明时默认 deferred，故必须显式声明原生可见。
        visibility="visible",
        timeout_ms=_TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_how(self, query: str = "", **kwargs):
        """一图流攻略查询：按 [how] source 分流到 GameKee / 猫猫球 / 双源。

        P3 两层鉴权：执行层在 _denied（黑白名单）之外先核平台——非 QQ 或
        平台反查失败（未知）保守拒绝且不发起任何抓取（新链路每次调用有
        真实网络成本，宁可漏不可错）。

        分流位置在 ensure_gamekee_index **之前**：bili 模式天然零 GameKee
        流量（连懒刷新都不触发），无需给 P2 懒刷新加闸。
        """
        if not await self._platform_allowed(**kwargs):
            return {"name": "stellasora_how", "content": "该平台暂不支持星塔旅人攻略查询。"}
        if self._denied(**kwargs):
            return {"name": "stellasora_how", "content": "当前聊天不在星塔旅人插件的允许范围内。"}
        query = (query or "").strip()
        if not query:
            # success=False 让宿主把结果记为失败（component_query 只读 success/error
            # 键）；带 JSON 示例的可引导错误降低模型连环空调用触发重复熔断的概率。
            error = (
                '缺少必填参数 query：请传入非空查询词，例如 {"query":"艾蕾攻略"}。'
                "仅在参数已补齐时重试，请勿再次发送空参数。"
            )
            return {"name": "stellasora_how", "success": False, "error": error, "content": error}
        self.ctx.logger.info(
            "how 查询: %s (source=%s group=%s user=%s)",
            query, self.config.how.source, kwargs.get("group_id", ""), kwargs.get("user_id", ""),
        )

        stream_id = self._resolve_stream_id(kwargs)
        if not stream_id:
            return {"name": "stellasora_how", "content": "缺少发送目标（stream_id）。"}

        source = self.config.how.source
        if source == "bili":
            return await self._how_bili(query, stream_id)
        if source == "all":
            return await self._how_all(query, stream_id)
        return await self._send_guides(
            stream_id,
            [("GameKee", await self._collect_gamekee(query))],
            miss_text="攻略库里暂时没有该角色/属性的图流攻略",
        )

    async def _collect_gamekee(self, query: str) -> GuideResult:
        """GameKee 源收集：匹配 → 下载 → 组节点（含队伍码节点）。不发送。"""
        try:
            index = await self.ensure_gamekee_index()
        except GameKeeProbeFailure as exc:
            self.ctx.logger.error("GameKee 索引不可用: %s", exc)
            return GuideResult(failure=f"GameKee 索引不可用: {str(exc)[:80]}")

        hits, miss_reason = gamekee_match_guides(index, query, aliases=self._resolve_guide_aliases())
        if not hits:
            self.ctx.logger.info("how 未命中(GameKee): query=%s reason=%s", query, miss_reason)
            return GuideResult(miss=True)

        # 下载图片（同步重活进线程池；webp 可配）。多图小节按相册原顺序全发
        # （GameKee 相册 data 数组即展示顺序；旧"末张=完整版长图"假设实测只对
        # 部分角色成立，纯多图相册取末张会丢前 N-1 张）。
        prefer_webp = bool(self.config.gamekee.prefer_webp)
        cache_dir = Path(self.ctx.paths.runtime_dir) / "cache" / "gamekee_images"
        images: list[dict] = []
        download_errors: list[str] = []
        for hit in hits:
            srcs = hit["image_urls"]
            group: list[str] = []
            for pos, src in enumerate(srcs, start=1):
                try:
                    data = await asyncio.to_thread(
                        gamekee_download_image, src, cache_dir, prefer_webp
                    )
                    group.append(base64.b64encode(data).decode("ascii"))
                except GameKeeError as exc:
                    # 单张失败只跳该图保其余（预期内失败不吞整节）
                    download_errors.append(f"{hit['name']} 第{pos}/{len(srcs)}张: {str(exc)[:120]}")
            if group:
                images.append({
                    "name": hit["name"],
                    "note": hit.get("note") or "",
                    "element_cn": _ELEMENT_CN_LABELS.get(hit.get("element"), hit.get("element", "")),
                    "base64_list": group,
                    "total": len(srcs),
                })
        if not images:
            self.ctx.logger.error("how 图片全部下载失败: %s", "; ".join(download_errors))
            return GuideResult(failure="GameKee 攻略图片全部下载失败")
        if download_errors:
            self.ctx.logger.warning("how 部分图片下载失败（跳过该图）: %s", "; ".join(download_errors))

        # 单角色=单节点图文混排，多角色=每角色一节点；多图小节=节点内多 image 段
        nodes = []
        sent_total = 0
        for img in images:
            segments = []
            for idx, b64 in enumerate(img["base64_list"], start=1):
                suffix = f"_p{idx}" if len(img["base64_list"]) > 1 else ""
                segments.append({
                    "type": "image",
                    "content": b64,
                    "filename": f"{img['name']}_guide{suffix}.webp",
                })
            sent_total += len(img["base64_list"])
            note_parts = [f"【{img['element_cn']}】{img['name']}"]
            # 存量 v2 索引兼容：解析闸修好前的索引把图后轮播提示收成了 note。
            # 不 bump schema（避免全量联网重建），只对已证实的整句精确剔除。
            note = (img["note"] or "").strip()
            if note and note != _ALBUM_HINT_TEXT:
                note_parts.append(note)
            if img["total"] > len(img["base64_list"]):
                note_parts.append(f"（{img['total']} 张中 {len(img['base64_list'])} 张下载失败未发送）")
            note_parts.append("来源：GameKee 星塔旅人 wiki（星塔攻略组）")
            segments.append({"type": "text", "content": "\n".join(note_parts)})
            nodes.append({"nickname": _FORWARD_NICKNAME, "segments": segments})
        # 队伍码是页级公共段：命中角色/属性所在页各附一个文本节点（无码页不附）
        for element in dict.fromkeys(h["element"] for h in hits):
            node = self._team_code_node(index, element)
            if node is not None:
                nodes.append(node)
        code_pages = len({h["element"] for h in hits if self._team_code_node(index, h["element"]) is not None})
        return GuideResult(nodes=nodes, image_count=sent_total, code_pages=code_pages)

    def _bili_manifest(self) -> Dict[str, Any]:
        """加载内置猫猫球清单（每次调用现读，清单随包发放不会运行时变化）。

        加载失败直接抛 BiliGuideError，由 how 分支转用户可见提示——清单损坏
        属于分发包问题，静默降级成"未收录"会掩盖故障。
        """
        return bili_load_manifest(Path(__file__).resolve().parent)

    def _bili_cache_root(self) -> Path:
        """猫猫球缓存根目录（runtime_dir/cache/bili_guides，永久缓存）。"""
        return Path(self.ctx.paths.runtime_dir) / "cache" / "bili_guides"

    async def _collect_bili(self, query: str) -> GuideResult:
        """猫猫球源收集：匹配 → 抓图（缓存命中零网络）→ 组节点。不发送。

        节点形态：一个 opus（一次相册）一个节点，节点内按原顺序多 image 段，
        末尾一段文字（标题 + 来源），与 GameKee 源的多图节点一致。
        """
        try:
            manifest = await asyncio.to_thread(self._bili_manifest)
        except BiliGuideError as exc:
            self.ctx.logger.error("猫猫球清单加载失败: %s", exc)
            return GuideResult(failure=f"猫猫球清单加载失败: {str(exc)[:100]}")

        hits, miss_reason = bili_match_guides(
            manifest, query, aliases=self._resolve_guide_aliases()
        )
        if not hits:
            self.ctx.logger.info("how 未命中(猫猫球): query=%s reason=%s", query, miss_reason)
            return GuideResult(miss=True)

        cache_root = self._bili_cache_root()
        nodes: list[dict] = []
        image_total = 0
        failures: list[str] = []
        for guide in hits:
            opus_id = str(guide["opus_id"])
            role = str(guide.get("role") or opus_id)
            try:
                files = await asyncio.to_thread(bili_fetch_guide_images, opus_id, cache_root)
                segments: list[dict] = []
                for path in files:
                    data = await asyncio.to_thread(path.read_bytes)
                    segments.append({
                        "type": "image",
                        "content": base64.b64encode(data).decode("ascii"),
                        "filename": f"{role}_bili_p{path.stem}.webp",
                    })
            except (BiliGuideError, OSError) as exc:
                # 单条失败不影响其余条目（与 GameKee 单图失败同纪律）；全部失败时
                # 由调用方转用户可见提示，不隐式回退其它源。
                self.ctx.logger.error("猫猫球攻略抓取失败: opus=%s: %s", opus_id, exc)
                failures.append(f"{role}(opus={opus_id}): {str(exc)[:100]}")
                continue
            element_cn = _ELEMENT_CN_LABELS.get(guide.get("element") or "", "")
            label = f"【{element_cn}】{role}" if element_cn else role
            segments.append({
                "type": "text",
                "content": f"{label} · {guide.get('title') or ''}\n来源：B站@铃儿的猫猫球",
            })
            nodes.append({"nickname": _FORWARD_NICKNAME, "segments": segments})
            image_total += len(files)
        if not nodes:
            return GuideResult(
                failure="猫猫球攻略图片全部抓取失败: " + "; ".join(failures[:3])
            )
        if failures:
            self.ctx.logger.warning("猫猫球部分条目抓取失败（跳过）: %s", "; ".join(failures))
        return GuideResult(nodes=nodes, image_count=image_total)

    async def _send_guides(
        self,
        stream_id: str,
        results: list[tuple[str, GuideResult]],
        *,
        miss_text: str,
    ) -> dict:
        """公共发送体：合并各源节点 → forward → 统一处理异常/失败/摘要。

        `results` 为 (源显示名, GuideResult) 列表，顺序即节点顺序。任一源失败
        不终止其余源（发可用源并标注失败侧）；全无图才发一次提示。
        真实发送失败返回 success=False + error，让宿主把本轮记为失败。
        """
        nodes: list[dict] = []
        notes: list[str] = []
        parts: list[str] = []
        for label, result in results:
            if result.ok:
                nodes.extend(result.nodes)
                detail = f"{result.image_count} 张"
                if result.code_pages:
                    detail += f"及 {result.code_pages} 页队伍码"
                parts.append(f"{label} {detail}")
            elif result.failure:
                notes.append(f"{label}源不可用（{str(result.failure)[:60]}）")

        if not nodes:
            reason = "；".join(notes) if notes else "两个攻略源都没有该角色/属性的收录"
            body = f"{miss_text}（{reason}）。"
            try:
                await self.ctx.send.text(body, stream_id)
            except Exception as exc:
                self.ctx.logger.exception("攻略提示发送异常: stream=%s", exc)
                return {
                    "name": "stellasora_how",
                    "success": False,
                    "error": f"攻略提示发送失败: {exc}",
                    "content": f"攻略提示发送失败: {exc}",
                }
            return {
                "name": "stellasora_how",
                "success": True,
                "content": "已向聊天发送'未找到相关攻略'提示。你不需要也不应该再调用 reply 工具，本轮无需再回复。",
                "stop_after_execution": True,
            }

        if notes:
            # 只标注失败的那一侧，不冒充"没收录"
            nodes = nodes + [{
                "nickname": _FORWARD_NICKNAME,
                "segments": [{"type": "text", "content": "\n".join(notes)}],
            }]
        try:
            sent = await self.ctx.send.forward(nodes, stream_id)
        except Exception as exc:
            self.ctx.logger.exception("攻略合并转发发送异常: stream=%s", stream_id)
            return {
                "name": "stellasora_how",
                "success": False,
                "error": f"攻略图片发送失败: {exc}",
                "content": f"攻略图片发送失败: {exc}",
            }
        if not sent:
            self.ctx.logger.error("攻略合并转发返回失败: stream=%s", stream_id)
            return {
                "name": "stellasora_how",
                "success": False,
                "error": "攻略图片发送失败（宿主返回未发送）",
                "content": "攻略图片发送失败（宿主返回未发送）",
            }
        total = sum(r.image_count for _, r in results)
        summary = f"攻略图已直接发送到聊天（共 {total} 张：{'，'.join(parts)}）" if len(parts) > 1 \
            else f"攻略图已直接发送到聊天（{total} 张{'及 ' + str(results[0][1].code_pages) + ' 页队伍码' if results[0][1].code_pages else ''}）"
        return {
            "name": "stellasora_how",
            "success": True,
            "content": (
                f"{summary}。用户已经可以看到完整答案，"
                "你不需要也不应该再调用 reply 工具，本轮无需再回复。"
            ),
            "stop_after_execution": True,
        }

    async def _how_bili(self, query: str, stream_id: str):
        """bili 单源：未收录与抓取失败分别提示，不隐式回退 GameKee。"""
        result = await self._collect_bili(query)
        if not result.ok:
            if result.miss:
                await self.ctx.send.text(
                    "猫猫球暂未收录该角色/属性的攻略，换个说法或换个角色试试～", stream_id
                )
                return {
                    "name": "stellasora_how",
                    "success": True,
                    "content": "已向聊天发送'猫猫球暂未收录'提示。你不需要也不应该再调用 reply 工具，本轮无需再回复。",
                    "stop_after_execution": True,
                }
            self.ctx.logger.error("猫猫球攻略不可用: %s", result.failure)
            await self.ctx.send.text("猫猫球攻略加载失败，请稍后再试（不是未收录）。", stream_id)
            return {
                "name": "stellasora_how",
                "success": False,
                "error": f"猫猫球攻略加载失败: {result.failure}",
                "content": "已向聊天发送'猫猫球攻略加载失败'提示。",
            }
        return await self._send_guides(
            stream_id, [("猫猫球", result)], miss_text="猫猫球暂未收录"
        )

    async def _how_all(self, query: str, stream_id: str):
        """双源：两源独立收集，任一源失败发另一源并标注（GameKee 在前）。"""
        gk = await self._collect_gamekee(query)
        bili = await self._collect_bili(query)
        return await self._send_guides(
            stream_id,
            [("GameKee", gk), ("猫猫球", bili)],
            miss_text="攻略库里暂时没有该角色/属性的图流攻略",
        )

    # ===== Tool 组件：资料查询（1.5.3）=====

    @Tool(
        _WHAT_TOOL,
        description=(
            "查询星塔旅人 GameKee 客观资料：旅人生日/所属/武器/薪资/天赋/指定等级面板、"
            "秘纹主位与辅位效果、NPC 基本资料、怪物已核实的弱点、日中术语、立绘与语音目录、"
            "以及按标题或原文查找公告与攻略文章。只查客观资料，不给配队建议；"
            "“怎么玩/配队/队伍码/一图流”用 stellasora_how。\n"
            "本工具自行把结果合并转发到聊天：命中图文时同时发出全部最终合格候选与图，"
            "全部合格内容发完才结束本轮；拿到成功结果后不要再调用 reply 复述，也不要再发同样的图。\n"
            "intent 必须按用户真实要求填写：mode 为 field/article/media/catalog；"
            "field 时给出 fields，面板类给出 levels（只用实际存在的等级）；"
            "media 时给出 groups；要整篇正文与正文图时 shape=whole_article；"
            "名称不确定就不要硬写，交由本工具判定未收录或列候选。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="必填，非空。用户问题原文（保留其字段与等级说法）；"
                "article 检索在 intent.query 未给出时用它做原文检索",
                required=True,
            ),
            ToolParameterInfo(
                name="intent",
                param_type=ToolParamType.OBJECT,
                description="必填。结构化意图：mode/subject{name,kind}/shape/fields/levels/"
                "groups/conditions（见计划 5.1）；日期条件尚未实现，不要传 date_window",
                required=True,
                properties=_WHAT_INTENT_PROPERTIES,
                required_properties=["mode"],
                additional_properties=False,
            ),
        ],
        platforms="qq",
        # 与 how 同源：宿主 1.4.x 只把 visibility=visible 的插件工具放进原生 tools 数组。
        visibility="visible",
        # 默认关闭：Host 读 metadata.enabled 作为组件初始状态；配置保存后由
        # _sync_what_component 用 component.enable/disable 热切换。
        enabled=False,
        timeout_ms=_TOOL_RPC_TIMEOUT_MS,
    )
    async def handle_what(self, query: str = "", intent: Any = None, **kwargs):
        """资料查询入口：执行层三层闸 → 只读已发布代 → 查询 → 交付层直发 → 五状态分流。

        执行层闸门与 how 同口径（平台 + 黑白名单），另加一层 [what] enabled：
        Hook 剔除是暴露层治理，直调工具不经 Hook，这里必须再判一次。
        查询与交付准备在同一个 to_thread 内完成（sqlite 连接不跨线程、整次调用固定
        同一已发布代），交付只拿到计划与图片取数回调，自己不读库。
        """
        if not self._what_enabled():
            return self._what_result(
                success=False,
                error="资料查询未启用（[what] enabled=false）",
            )
        if not await self._platform_allowed(**kwargs):
            return {"name": _WHAT_TOOL, "content": "该平台暂不支持星塔旅人资料查询。"}
        if self._denied(**kwargs):
            return {"name": _WHAT_TOOL, "content": "当前聊天不在星塔旅人插件的允许范围内。"}

        query = (query or "").strip()
        if not query:
            return self._what_result(
                success=False,
                error='缺少必填参数 query：请传入非空问题原文，例如 {"query":"优叶生日",'
                '"intent":{"mode":"field","subject":{"name":"优叶","kind":"traveler"},'
                '"fields":["birthday"]}}。仅在参数已补齐时重试，请勿再次发送空参数。',
            )
        stream_id = self._resolve_stream_id(kwargs)
        if not stream_id:
            return self._what_result(success=False, error="缺少发送目标（stream_id）。")

        normalized, intent_error = self._what_intent(intent, query)
        if intent_error or normalized is None:
            return self._what_result(success=False, error=intent_error or "intent 不成立")

        self.ctx.logger.info(
            "what 查询: mode=%s query=%s group=%s user=%s",
            normalized.get("mode"), query[:40], kwargs.get("group_id", ""), kwargs.get("user_id", ""),
        )

        try:
            result, plan, generation = await asyncio.to_thread(self._what_prepare_sync, normalized)
        except Exception as exc:
            word, detail = self._what_classify_error(exc)
            self.ctx.logger.exception("what 查询/准备失败: %s", detail)
            if word:
                await self._what_notice(word, stream_id)
            return self._what_result(success=False, error=detail, content=word)

        if result.status == "invalid_intent":
            # 意图不成立是 planner 侧参数问题（不能换字段猜答案），只回 planner 不打扰群
            return self._what_result(success=False, error=f"intent 不成立：{result.message}")

        stale_note = ""
        if generation.is_stale(int(self.config.what.stale_warn_hours)):
            stale_note = f"（资料可能过时：截至 {generation.last_verified}）"

        try:
            import what_delivery  # 运行时加载：tools/ 模块 import 期不得读库或触网

            report = await what_delivery.deliver_what(
                stream_id=stream_id,
                send_forward=self.ctx.send.forward,
                plan=plan,
                load_image=self._what_load_image,
                host_version=os.environ.get("MAIBOT_HOST_VERSION", ""),
                logger=self.ctx.logger,
            )
        except Exception as exc:
            self.ctx.logger.exception("what 交付层异常: %s", exc)
            await self._what_notice(_WORD_UNKNOWN, stream_id)
            return self._what_result(
                success=False,
                error=f"资料交付异常（未重发）: {type(exc).__name__}: {exc}",
                content=_WORD_UNKNOWN,
            )

        return await self._what_report(report, result, generation, stream_id, stale_note)

    def _what_result(
        self, *, success: bool, content: str = "", error: str = "", stop: bool = False
    ) -> dict:
        """工具回执（宿主只认 success/content/error/stop_after_execution 四个公开字段）。"""
        payload: dict[str, Any] = {"name": _WHAT_TOOL, "success": success, "content": content}
        if error:
            payload["error"] = error
        if stop:
            payload["stop_after_execution"] = True
        return payload

    @staticmethod
    def _send_confirmed(sent: Any) -> bool:
        """发送回执是否「明确成功」：只认真 bool True 或 SDK 字典里的显式 True。

        旧实现把非 bool 一律当成功；宿主/SDK 在发送失败或超时时可能回 None、
        空字典或 {"success": False}，那样会让「没查到」提示在未确认的情况下
        被当成已发出并停轮（假成功）。字典只认 sent/success 的明确 True，
        其余（None、空字典、失败字典、异常对象）一律视为未确认。
        """
        if isinstance(sent, bool):
            return sent
        if isinstance(sent, dict):
            for key in ("sent", "success"):
                if key in sent:
                    return sent.get(key) is True
        return False

    async def _what_notice(self, text: str, stream_id: str) -> bool:
        """向原会话发一条短提示；返回是否确实发出（发送失败只告警，不改判定）。"""
        if not stream_id:
            return False
        try:
            sent = await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("资料提示发送异常: stream=%s err=%s", stream_id, exc)
            return False
        confirmed = self._send_confirmed(sent)
        if not confirmed:
            self.ctx.logger.warning(
                "资料提示发送未确认: stream=%s text=%s outcome=%r", stream_id, text, sent
            )
        return confirmed

    def _what_intent(self, raw: Any, query: str) -> tuple[Optional[Dict[str, Any]], str]:
        """校验并归一 planner 传入的结构化意图；返回 (intent, 错误文案)。

        纪律：不猜意图、不替换字段、不凭用户字符串解析 URL；未实现的日期条件
        明确拒绝而不是无声忽略（否则 planner 以为按日期筛过）。
        """
        if raw is None or raw == "":
            return None, (
                "缺少必填参数 intent：请按 mode/subject{name,kind}/shape/fields/levels/"
                "groups/conditions 传入结构化意图（计划 5.1）。"
            )
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None, "intent 必须是 JSON 对象，收到无法解析的字符串。"
        if not isinstance(raw, dict):
            return None, "intent 必须是对象。"

        intent: Dict[str, Any] = dict(raw)
        mode = str(intent.get("mode") or "").strip()
        if mode not in _WHAT_MODES:
            return None, f"intent.mode 必须是 {'/'.join(_WHAT_MODES)}，收到 {mode!r}。"

        date_window = intent.get("date_window")
        if date_window not in (None, "", {}, []):
            return None, (
                "intent.date_window 暂不支持：资料库尚无事件有效期列，"
                "按日期筛选会得到错误结论，请去掉该字段或改用 article 检索。"
            )

        subject = intent.get("subject")
        if subject is not None and not isinstance(subject, dict):
            return None, "intent.subject 必须是对象 {name,kind}。"
        shape = intent.get("shape")
        if shape not in (None, "") and shape not in _WHAT_SHAPES:
            return None, f"intent.shape 只能是 {'/'.join(_WHAT_SHAPES)}，收到 {shape!r}。"
        for key in ("fields", "levels", "groups"):
            value = intent.get(key)
            if value is not None and not isinstance(value, (list, tuple)):
                return None, f"intent.{key} 必须是数组。"
        conditions = intent.get("conditions")
        if conditions is not None and not isinstance(conditions, dict):
            return None, "intent.conditions 必须是对象（条件名→条件值）。"

        if mode == "article" and not any(
            intent.get(key) for key in ("query", "query_text", "text", "literal")
        ):
            # article 只有「按主体列出条目」和「按原文检索」两条路：没给检索文本时
            # 用 query 参数的问题原文做检索；有主体时保持 planner 原意（列条目）。
            subject_name = ""
            if isinstance(subject, dict):
                subject_name = str(subject.get("name") or "").strip()
            if not subject_name:
                intent["query"] = query
        return intent, ""

    def _what_image_cache_dir(self) -> Path:
        """资料图字节缓存（runtime_dir/cache/what_images，非持久可随时重建）。"""
        return Path(self.ctx.paths.runtime_dir) / "cache" / "what_images"

    def _what_load_image(self, url: str) -> bytes:
        """交付层取图回调（同步体，由交付层在线程里调用）：沿用 GameKee 图片下载的
        主机白名单、魔数校验与 webp 参数，不新增下载通道。"""
        return gamekee_download_image(
            url, self._what_image_cache_dir(), bool(self.config.gamekee.prefer_webp)
        )

    def _what_prepare_sync(
        self, intent: Dict[str, Any]
    ) -> tuple[Any, Any, Any]:
        """同步准备体（只在 to_thread 内跑）：开库 → 查询 → 交付准备 → 关库。

        三者必须同线程同代：sqlite 连接默认 check_same_thread，且「整次调用固定一个
        已发布代」的取证纪律要求 alias/fact/FTS 与交付准备读到同一份库。
        """
        import what_delivery
        import what_library
        import what_query

        library = what_library.WhatLibrary(self._what_root())
        with library.open_current() as (conn, generation):
            result = what_query.run_query(conn, intent)
            plan = what_delivery.prepare_delivery(conn, result, intent)
        return result, plan, generation

    def _what_update_active(self) -> bool:
        """资料同步是否真的在飞（「建库中」判定的唯一依据）。

        只认两个运行时事实：任务句柄存在且未结束，且没有已置位的取消信号
        （关闭开关/卸载会置位并清空句柄）。不看磁盘是否残留 staging：失败或
        取消留下的 staging 文件会长期存在，把它当建库中会让提示永远卡在建库中。
        首次同步在飞、尚未创建 staging 的窗口，正是这条判定要覆盖的情形。
        """
        task = self._what_update_task
        if task is None or task.done():
            return False
        stop = self._what_update_stop
        if stop is not None and stop.is_set():
            return False
        return True

    def _what_classify_error(self, exc: BaseException) -> tuple[str, str]:
        """库异常 → (QQ 短提示, planner-facing error)。

        未建库（pending）与库损坏（corrupt）必须分流：前者不是故障，后者才是读取失败；
        「未收录」绝不能从这两类故障里冒充出来。
        """
        try:
            import what_library
        except ImportError as import_exc:
            # 打包缺模块时也不能让分类本身再抛异常：原始错误照样完整暴露给 planner
            return _WORD_READ_FAIL, f"资料库模块不可用: {type(exc).__name__}: {exc}（{import_exc}）"

        if isinstance(exc, what_library.WhatLibraryUnavailable):
            if exc.kind == "pending":
                # 「建库中」只由**真实在飞**的同步任务决定：pending 本身不是证据。
                # 失败/取消留下的 staging 文件会长期存在，拿 reason=="staging_only"
                # 当建库中会让提示永远停在建库中；反过来首次同步在飞时可能还没创建
                # staging（reason=no_generation），这时才是真的建库中。
                word = _WORD_BUILDING if self._what_update_active() else _WORD_NO_LIBRARY
            else:
                word = _WORD_READ_FAIL
            # reason/detail 只进工具回执做诊断，不进 QQ 短语、也不据它改写判定
            return word, f"资料库不可用（{exc.reason}）: {exc.detail}"
        if isinstance(exc, what_library.WhatLibraryError):
            return _WORD_READ_FAIL, str(exc)
        return _WORD_READ_FAIL, f"{type(exc).__name__}: {exc}"

    async def _what_send_fail_result(self, report: Any, stream_id: str, *, default_error: str) -> dict:
        """明确发送失败的统一收尾：直发「发送失败」，回执 success=False 且不停轮。

        提示本身是否送达不改变「这次没发成」的判定——不假成功、也不把缺省错误
        改写成用户短语；提示没发出去时只在 error 里补一句，留给 planner 决定补话。
        """
        error = str(getattr(report, "error", "") or default_error)
        delivered = await self._what_notice(_WORD_SEND_FAIL, stream_id)
        if not delivered:
            error = f"{error}（发送失败提示未送达）"
        return self._what_result(success=False, error=error, content=_WORD_SEND_FAIL)

    async def _what_report(
        self, report: Any, result: Any, generation: Any, stream_id: str, stale_note: str
    ) -> dict:
        """交付结果 → 五状态回执（success / stop_after_execution / QQ 短提示）。

        成功交付与「没查到且提示确实发出」才停轮；部分不设停轮；送达未知绝不重发。
        交付层的发送结论只认 `report.success` 与图片计数：媒体一张都没发出去时
        交付层给的是 `nothing + success=False + images_failed>0`（不是 failed），
        这条必须按「发送失败」收尾——既不能转成「没查到」长句，也不能停轮。
        """
        status = str(getattr(report, "status", "") or "")
        packs = int(getattr(report, "packs_sent", 0) or 0)
        images = int(getattr(report, "images_sent", 0) or 0)
        failed_images = int(getattr(report, "images_failed", 0) or 0)
        # 兼容旧替身：没有 success 字段的回执视为「未声明发送问题」，只有显式 False
        # 或图片明确失败才按发送失败处理。
        send_problem = getattr(report, "success", True) is False or failed_images > 0
        as_of = f"资料截至 {generation.last_verified}" if generation.last_verified else ""

        if status == "sent":
            summary = f"资料已直接转发到聊天（{packs} 包 / {images} 张图）。"
            if as_of:
                summary += f"{as_of}。"
            if stale_note:
                summary += stale_note
            return self._what_result(
                success=True,
                content=f"{summary}用户已经可以看到完整答案，你不需要也不应该再调用 reply 工具，"
                "也不必重新查询或重试，本轮无需再回复。",
                stop=True,
            )

        if status == "nothing":
            if send_problem:
                # 媒体全挂：交付层按「合法结论」形态回 nothing，但 success=False 且
                # images_failed>0 —— 这不是「没查到」，不能发长句、更不能停轮。
                return await self._what_send_fail_result(
                    report, stream_id, default_error="资料未能发送"
                )
            phrase = _WORD_MISS if result.status == "not_found" else str(
                getattr(report, "user_text", "") or result.message or _WORD_MISS
            )
            delivered = await self._what_notice(phrase, stream_id)
            if not delivered:
                return self._what_result(
                    success=False,
                    error="提示发送失败（未送达，未重发）",
                    content=phrase,
                )
            return self._what_result(
                success=True,
                content=f"已向聊天发送提示（{phrase}）。用户已经看到结论，本轮无需再回复。",
                stop=True,
            )

        if status == "partial":
            await self._what_notice(_WORD_PARTIAL, stream_id)
            reasons = getattr(report, "skipped_reasons", None) or []
            detail = f"；未发原因: {'; '.join(str(r) for r in reasons[:3])}" if reasons else ""
            return self._what_result(
                success=True,
                content=f"部分已发（{packs} 包 / {images} 张图，{failed_images} 张明确未发{detail}）。"
                "尚未发完的内容由你决定是否补话，本轮不自动停。",
            )

        if status == "unknown":
            await self._what_notice(_WORD_UNKNOWN, stream_id)
            return self._what_result(
                success=False,
                error=str(getattr(report, "error", "") or "交付层返回送达未知"),
                content=f"{_WORD_UNKNOWN}：已发出部分未确认，不要重发或重试查询。",
            )

        # failed：发送前就明确失败（未命中已在 nothing 分支）。交付层的 user_text 在这里
        # 可能把「计划无效」写成「读取失败」，与真实原因不符，故不当作用户短语。
        # 图片明确一张都没发出去（images_failed>0）时与 nothing 分支同口径：WHAT 直发
        # 「发送失败」，回执 success=False、不停轮。images_failed=0 的失败（计划无效等
        # 非交付尝试失败）保持只回 planner，不打扰群。
        if failed_images > 0:
            return await self._what_send_fail_result(
                report, stream_id, default_error="资料交付失败"
            )
        return self._what_result(
            success=False,
            error=str(getattr(report, "error", "") or "资料交付失败"),
            content="",
        )

    def _team_code_node(self, index: Dict[str, Any], element: str) -> Optional[dict]:
        """取某属性页的队伍码合并转发文本节点；页无码（如未开攻略）返回 None。"""
        codes = (index.get("pages") or {}).get(element, {}).get("team_codes") or []
        if not codes:
            return None
        element_cn = _ELEMENT_CN_LABELS.get(element, element)
        lines = [f"【{element_cn}】队伍码（本页公共，按攻略组优先级排序）："]
        for entry in codes:
            lines.append(f"{entry['title']}：{entry['code']}")
        lines.append("来源：GameKee 星塔旅人 wiki（星塔攻略组）")
        return {"nickname": _FORWARD_NICKNAME, "segments": [{"type": "text", "content": "\n".join(lines)}]}

    # ===== Command 指令 =====

    @Command(
        "st_update",
        description="触发星塔旅人数据更新（操作员）：强刷当期两榜数据 + 探查 GameKee 攻略源更新，后台执行完成后报告各榜结果（1.5.0 起不再 reload 插件，数据即时生效）",
        pattern=r"^/st_update",
        permission="operator",
    )
    async def handle_update(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """/st_update 入口：鉴权 → 按路登记在飞闸门 → 后台执行。

        闸门登记**先于** await 发送进度消息（旧实现登记在 await 之后，连发两调用
        可能同时通过）。全程可超宿主组件 RPC 60s 超时，必须 create_task 后台化；
        返回拦截等级 2 使触发消息不进 planner。两个任务句柄兼作强引用。

        两路独立受理：原三路（攻略探查 + 两榜）与资料同步各有句柄。原三路在飞而
        资料同步空闲时，重复触发只启动资料同步（不重开抓两榜）；两路都在飞才整单
        拒绝——与 1.5.2 前的单任务语义保持一致（`[what]` 关闭时行为完全不变）。
        """
        if not await self._platform_allowed(**kwargs):
            return False, "该平台暂不支持星塔旅人更新指令。", 1
        if self._denied(**kwargs):
            return False, "当前聊天无权限执行星塔旅人更新指令。", 1

        routes_busy = self._update_task is not None and not self._update_task.done()
        what_busy = self._what_update_task is not None and not self._what_update_task.done()
        what_wanted = self._what_enabled()
        if routes_busy and (what_busy or not what_wanted):
            return False, "已有更新任务进行中，请稍候。", 1

        effective_stream_id = stream_id or self._resolve_stream_id(kwargs)
        started_routes = False
        started_what = False
        if not routes_busy:
            # 本次三路的短探查闸门：WHAT 长批先等它放行，避免长批独占同一 GameKee 主机预算
            self._what_probe_gate = asyncio.Event()
            self._update_task = asyncio.create_task(self._run_full_update(effective_stream_id))
            started_routes = True
        if what_wanted and not what_busy:
            gate = self._what_probe_gate
            if gate is None:
                # 三路不在飞（上一次已跑完）：没有探查可等，长批直接放行
                gate = asyncio.Event()
                gate.set()
                self._what_probe_gate = gate
            if self._what_update_stop.is_set():
                # 上一轮被关闭/卸载取消过：新任务必须有全新的取消信号
                self._what_update_stop = threading.Event()
            self._what_update_task = asyncio.create_task(
                self._run_what_update(effective_stream_id, gate)
            )
            started_what = True

        if routes_busy:
            note = (
                f"已有更新任务进行中；已开始资料库同步（{self._what_progress_text()}）。"
                if started_what
                else "已有更新任务进行中，请稍候。"
            )
        elif started_what:
            note = "正在后台更新星塔旅人数据（强刷两榜 + 探查攻略源）；资料库在后台同步。"
        elif what_busy:
            note = (
                "正在后台更新星塔旅人数据（强刷两榜 + 探查攻略源）；"
                f"资料库同步进行中（{self._what_progress_text()}）。"
            )
        else:
            note = "正在后台更新星塔旅人数据（强刷两榜 + 探查攻略源）..."
        await self._send_update_msg(note, effective_stream_id)
        summary = "已启动星塔旅人数据更新" if started_routes else "已启动资料库同步"
        return True, summary, 2

    def _what_progress_text(self) -> str:
        """在飞资料同步的进度短句（重复触发时回报当前阶段，不启动第二份）。"""
        progress = self._what_update_progress or {}
        stage = str(progress.get("stage") or "")
        done, total = progress.get("done"), progress.get("total")
        stage_cn = {
            "tree": "目录", "list": "清单", "detail": "详情",
            "build": "建库", "verify": "校验", "publish": "发布",
        }.get(stage, stage or "准备中")
        if isinstance(total, int) and total > 0 and isinstance(done, int):
            return f"{stage_cn} {done}/{total}"
        return stage_cn

    async def _run_what_update(self, stream_id: str, gate: asyncio.Event) -> None:
        """资料同步后台体：等短探查闸门 → 线程内刷新 → 单独终报（不等原三路）。"""
        stop_event = self._what_update_stop
        self._what_update_progress.clear()
        try:
            await gate.wait()
            if stop_event.is_set():
                self.ctx.logger.info("资料同步未开工即被取消（开关关闭/卸载）")
                return
            report = await asyncio.to_thread(self._what_update_sync, stop_event)
        except asyncio.CancelledError:
            # 取消只是让协程退出；worker 线程靠取消信号收敛，不在这里重写状态
            self.ctx.logger.info("资料同步任务被取消（worker 依取消信号收敛）")
            raise
        except Exception as exc:
            self.ctx.logger.exception("资料同步异常: %s", exc)
            # QQ 只报「更新失败 + 旧库状态」这一层；异常类型与栈留在日志与回执里
            await self._send_update_msg(
                "资料库同步失败（更新失败）；错误详情见日志。", stream_id
            )
            return
        self.ctx.logger.info("资料同步结果: %s", getattr(report, "status", "?"))
        if str(getattr(report, "status", "")) != "published":
            self.ctx.logger.warning(
                "资料同步未发布: stage=%s done=%s/%s code=%s error=%s reasons=%s",
                getattr(report, "stage", ""), getattr(report, "done", 0), getattr(report, "total", 0),
                getattr(report, "error_code", ""), getattr(report, "error", ""),
                _what_reason_text(getattr(report, "reasons", None) or []),
            )
        await self._send_update_msg(self._what_update_report_text(report), stream_id)

    def _what_update_sync(self, stop_event: threading.Event) -> Any:
        """刷新同步体（只在 to_thread 内跑）：库模块自己单写备库并原子切指针。"""
        import what_library

        library = what_library.WhatLibrary(self._what_root())
        return library.refresh(
            should_stop=stop_event.is_set,
            progress=self._what_progress_sink,
        )

    def _what_progress_sink(self, payload: Dict[str, Any]) -> None:
        """worker 线程的进度回调：只留最新一份（不得在此 await 或做 IO）。"""
        if isinstance(payload, dict):
            self._what_update_progress.update(payload)

    @staticmethod
    def _what_update_report_text(report: Any) -> str:
        """资料同步终报：成功给增量与发布时间；失败只给阶段、已完成数与旧库状态。

        错误码与逐条原因不进 QQ（技术细节留日志）：群里的读者只需要知道
        「更新失败、旧库还在、截至何时」这三件事。
        """
        status = str(getattr(report, "status", "") or "failed")
        stage = str(getattr(report, "stage", "") or "")
        done, total = getattr(report, "done", None), getattr(report, "total", None)
        stage_cn = {
            "tree": "目录", "list": "清单", "detail": "详情", "build": "建库",
            "verify": "校验", "publish": "发布", "cancel": "收尾",
        }.get(stage, stage or "未知阶段")
        position = f"已完成 {done}/{total}" if isinstance(done, int) and isinstance(total, int) else f"停在{stage_cn}阶段"
        old_ready = bool(getattr(report, "old_generation_ready", False))
        previous_verified = getattr(report, "previous_last_verified", None)
        fallback = (
            "旧库仍为上次发布版本" if old_ready else "首次建库尚未完成"
        ) + (f"（资料截至 {previous_verified}）" if previous_verified else "")

        if status == "published":
            lines = [
                "资料库同步完成：",
                f"· 新增 {getattr(report, 'added', 0)} / 重抓 {getattr(report, 'refetched', 0)}"
                f" / 复用 {getattr(report, 'reused', 0)} / 移除 {getattr(report, 'removed', 0)}",
            ]
            if getattr(report, "generation", None):
                lines.append(f"· 发布代：{report.generation}")
            if getattr(report, "last_verified", None):
                lines.append(f"· 资料截至 {report.last_verified}")
            return "\n".join(lines)
        if status == "cancelled":
            return f"资料库同步已取消（{position}）；{fallback}"
        return f"资料库同步失败（{stage_cn}阶段，{position}）；{fallback}"



    async def _send_update_msg(self, text: str, stream_id: str) -> None:
        """向触发流发送更新进度消息；失败仅告警（进度丢失不影响更新本身）。"""
        if not stream_id:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("发送更新进度消息异常: %s", exc)

    async def _run_full_update(self, stream_id: str) -> None:
        """/st_update 后台三路：闸门兜底放行后进入实体，异常不把资料长批挂死。"""
        gate = self._what_probe_gate or asyncio.Event()
        self._what_probe_gate = gate
        try:
            await self._run_routes_update(stream_id, gate)
        finally:
            # 探查段之后还有榜单/报告，任何异常或取消都必须放行：否则 WHAT 长批
            # 会一直等到卸载，等于把资料同步永久挂住（闸门 set 幂等，重复无副作用）
            gate.set()

    async def _run_routes_update(self, stream_id: str, gate: asyncio.Event) -> None:
        """原有三路实体（1.5.0 语义不变）：探查 → 两榜 → 三路独立报告。

        不再 reload 插件：字典（唯一进程单例）已删，榜单/索引数据落盘即时生效
        （裁决 e）。三路结果独立报告（K 节断言：一榜失败不吞另一榜与探查）。
        探查接管后台排程：手动触发时先 _reset_probe_waiter 让后台任务让路，
        完成后按结果重置计时（失败不重置——last_verified 只表示成功校验）。
        """
        lb_dir = self._lb_data_dir_ready()

        # 1. GameKee 探查（仅接管"启动延迟"中的后台任务；正常等待不动）
        probe_summary = "探查失败"
        try:
            if self._background_in_boot_delay():
                self._reset_probe_waiter()
            index = await self._run_gamekee_probe(trigger="manual_update")
            chars = len(index.get("characters") or [])
            probe_summary = f"索引已更新（{chars} 小节），每日探查计时已重置"
        except GameKeeProbeFailure as exc:
            probe_summary = f"探查失败（{str(exc)[:80]}），每日探查计时未重置"
        finally:
            # 探查段一结束就放行（不等到两榜/报告）：短探查优先，长批不独占请求预算
            gate.set()
        # 探查失败后若接管取消了后台排程，恢复一个唤醒任务（P2 排程恢复规则）
        self._reschedule_after_manual_probe()

        # 2. 强刷两榜（绕过内容 TTL；两榜独立报告）
        # 代理只作用于 ssleaderboard 上游：显式读取一次传下去（空串=强制直连）
        board_results = await asyncio.to_thread(
            refresh_board_for_update, lb_dir, proxy_url=self.config.leaderboard.proxy
        )
        bb, fe = board_results.get("bb", {}), board_results.get("fe", {})
        bb_text = self._format_board_update_result("联合讨伐", bb)
        fe_text = self._format_board_update_result("终焉绝响", fe)

        await self._send_update_msg(
            "星塔旅人数据更新完成：\n"
            f"· 攻略源：{probe_summary}\n"
            f"· {bb_text}\n"
            f"· {fe_text}",
            stream_id,
        )

    def _reschedule_after_manual_probe(self) -> None:
        """手动探查接管后恢复后台唤醒（P2 排程恢复规则，审查修正版）。

        仅当后台任务**已退出**（让路/取消）时才按结果重建：
        - 手动成功 → last_verified 已推进 → 正常 24h 调度；
        - 手动失败 → last_failure_at+30min 重试点。
        后台任务仍在等自己的到期点（正常等待期间）时**完全不动**——手动失败
        不得把还很远的 24h 截止点改成 30min 重试（计划 P2 恢复规则的限定条件）。

        重建走 `_ensure_probe_task_if_enabled()`：bili 单源下 /st_update 探查完成后
        不得悄悄把 P2 后台流量恢复回来。探查本身照常执行（用户显式操作）。
        """
        task = self._gamekee_probe_task
        if task is not None and not task.done():
            return  # 正常等待期间：保持原排程，手动失败不影响
        self._ensure_probe_task_if_enabled()

    @staticmethod
    def _format_board_update_result(mode_cn: str, result: Dict[str, Any]) -> str:
        """单榜强刷结果 → 一行用户可见文本（独立报告，不吞失败）。"""
        status = result.get("status", "error")
        detail = result.get("detail", "")
        if status == "ok":
            return f"{mode_cn}：强刷成功（{detail}）"
        if status == "not_found":
            return f"{mode_cn}：{detail}"
        if status == "no_pointer":
            return f"{mode_cn}：{detail}"
        return f"{mode_cn}：失败（{detail}）"

    @Command(
        "st_bb",
        description="查询星塔旅人当期联合讨伐（Boss Blitz）排行榜国服 Top100，触发时按当期赛季指针拉取最新",
        pattern=r"^/st_bb(?:\s+(?P<args>.+))?$",
    )
    async def handle_lb_bb(self, args: str = "", stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """查询当期联合讨伐排行榜国服 Top100（所有人可用，受黑白名单约束）。"""
        return await self._handle_lb_board("bb", stream_id=stream_id, **kwargs)

    @Command(
        "st_fe",
        description="查询星塔旅人当期终焉绝响（Finale Echoing）排行榜国服 Top100，触发时按当期赛季指针拉取最新",
        pattern=r"^/st_fe(?:\s+(?P<args>.+))?$",
    )
    async def handle_lb_fe(self, args: str = "", stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """查询当期终焉绝响排行榜国服 Top100（所有人可用，受黑白名单约束）。"""
        return await self._handle_lb_board("fe", stream_id=stream_id, **kwargs)

    async def _handle_lb_board(self, mode: str, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """排行榜双命令共用体：鉴权+平台 → 拉数据 → 按 render_images 开关分流输出。

        kwargs 必须从命令 handler 透传（宿主注入的 group_id/user_id 为 _denied
        依据，漏传即空身份导致白名单模式全拒）。宿主对命令返回值只记日志不发送
        （self-send 约定），返回值仅作执行摘要。数据层异常（指针缺失/未开榜/
        拉取失败）走短文本。开关只切换输出形态（render_images=True 图片榜 /
        False 文字榜），不切换取数与错误处理；渲染失败提示失败不回退文字榜
        （错误完整暴露，不滥兜底）。
        """
        if not await self._platform_allowed(**kwargs):
            return False, "该平台暂不支持星塔旅人排行榜查询。", 1
        if self._denied(**kwargs):
            return False, "当前聊天无权限执行星塔旅人排行榜查询。", 1

        effective_stream_id = stream_id or self._resolve_stream_id(kwargs)
        ttl = int(self.config.leaderboard.ttl_minutes)
        render_images = bool(self.config.leaderboard.render_images)
        # 榜单代理只作用于 ssleaderboard 上游（空串=强制直连，不读环境变量）
        board_proxy = self.config.leaderboard.proxy
        lb_dir = self._lb_data_dir_ready()
        try:
            # 1. 指针 + 数据（同步网络/磁盘重活进线程池；异常消息即用户可见文案）
            prefix, sid = await asyncio.to_thread(
                resolve_current_board, mode, lb_dir, proxy_url=board_proxy
            )
            cn_data, header, age = await asyncio.to_thread(
                load_board_for_render, prefix, sid, ttl, lb_dir, proxy_url=board_proxy
            )
        except ValueError as exc:
            return False, str(exc), 1
        except BoardUnavailable as exc:
            notice = str(exc)
            self.ctx.logger.info("排行榜不可用: %s", notice)
            try:
                await self.ctx.send.text(notice, effective_stream_id)
            except Exception as send_exc:
                self.ctx.logger.exception("排行榜提示发送异常: %s", send_exc)
                return False, f"星塔旅人排行榜提示发送失败: {send_exc}", 1
            return True, "星塔旅人排行榜提示已发送", 2
        except Exception as exc:
            self.ctx.logger.exception("排行榜查询异常: %s", exc)
            return False, f"星塔旅人排行榜查询失败: {exc}", 1

        # 2. 按 render_images 开关组节点：图片榜（HTML 渲染）或文字榜（1.4.x 格式）
        if render_images:
            # 复用的依据是"渲染输入完全相同"：sid + 精确时间文本 + 数据指纹。
            # 时间文本必须入键——TTL 过期重拉时抓取时刻会变（即便上游数据没变），
            # 图上的"更新于"就得跟着更新，否则显示的是上一次的旧时刻。
            fetched_at = time.time() - age
            time_text = format_fetch_time(fetched_at)
            cache_key = f"{prefix}:{sid}:{time_text}:{board_fingerprint(cn_data)}"
            cached = self._lb_render_cache
            if cached is not None and cached[0] == cache_key:
                images = cached[1]
                self.ctx.logger.info("排行榜渲染复用（渲染输入未变）: %s", cache_key)
            else:
                try:
                    # 同步 CPU 重活进线程池；渲染本身是宿主 async 能力
                    pages = await asyncio.to_thread(
                        build_board_pages, prefix, sid, cn_data, time_text
                    )
                    images = await render_board_pages(self, pages)
                except Exception as exc:
                    self.ctx.logger.exception("排行榜渲染失败: %s", exc)
                    try:
                        await self.ctx.send.text("排行榜图片渲染失败，请稍后再试。", effective_stream_id)
                    except Exception as send_exc:
                        self.ctx.logger.exception("排行榜渲染提示发送异常: %s", send_exc)
                        return False, f"星塔旅人排行榜渲染失败: {exc}", 1
                    return True, "星塔旅人排行榜渲染失败提示已发送", 2
                self._lb_render_cache = (cache_key, images)
            # 节点只含图片段：赛季标题/总人数/更新时间/页码全部渲染进图本身
            # （leaderboard_render 的 title + meta + footer），再挂一段文字是重复。
            nodes = [
                {
                    "nickname": _FORWARD_NICKNAME,
                    "segments": [
                        {"type": "image", "content": image_b64,
                         "filename": f"{prefix}_{sid}_p{i + 1}.png"},
                    ],
                }
                for i, image_b64 in enumerate(images)
            ]
        else:
            nodes = await asyncio.to_thread(render_text_board_nodes, cn_data, header, age)

        # 3. 统一合并转发（单页/多页都走 forward）
        try:
            sent = await self.ctx.send.forward(nodes, effective_stream_id)
        except Exception as exc:
            self.ctx.logger.exception("排行榜发送异常: %s", exc)
            return False, f"星塔旅人排行榜发送失败: {exc}", 1
        if not sent:
            self.ctx.logger.error("排行榜发送失败: stream=%s", effective_stream_id)
            return False, "星塔旅人排行榜发送失败", 1
        return True, "星塔旅人排行榜已发送", 2


def create_plugin() -> StellaSoraPlugin:
    """创建插件实例（MaiBot 插件入口约定）。"""
    return StellaSoraPlugin()
