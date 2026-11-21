"""星塔旅人（Stella Sora）攻略与排行榜插件

GameKee 中文源（一图流攻略原图直发）+ ssleaderboard 排行榜图片化：
  - stellasora_how  Tool  一图流攻略查询（六属性 24 角色小节，合并转发）
  - /st_bb・/st_fe   Command  当期国服 Top100 图片榜单
  - /st_update      Command  强刷两榜 + 探查攻略源（操作员）

安全：白名单/黑名单模式可配置（config.toml），群聊按群号、私聊按用户号鉴权；
仅 QQ 平台（Hook 可见性 + 执行层双闸，未知平台保守拒绝）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional, cast
import asyncio
import base64
import json
import logging
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
    match_guides as gamekee_match_guides,
    probe_updates as gamekee_probe_updates,
)
from leaderboard import (  # noqa: E402
    BoardUnavailable,
    format_age as format_age_text,
    load_board_for_render,
    refresh_board_for_update,
    resolve_current_board,
)
from leaderboard_render import build_board_pages, render_board_pages
from access import check_permission  # noqa: E402

logger = logging.getLogger("stellasora.plugin")

class GameKeeProbeFailure(Exception):
    """GameKee 探查/重建失败（网络/结构漂移/代际过期），由调用方区分旧快照降级或向上暴露。"""



# 本插件暴露给 planner 的工具名集合：Hook 前置过滤单一清单（1.5.0 仅剩 how）
_TOOL_NAMES = frozenset({"stellasora_how"})


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（Runner 强制要求 plugin.config_version）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.4.0", description="配置版本（1.4.0=1.5.0 重写：移除 [query]，新增 [gamekee]）")


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
        self._gamekee_probe_task: asyncio.Task[None] | None = None  # 后台到期探查任务
        self._gamekee_probe_waiter: asyncio.Future | None = None  # 启动延迟可被手动更新取消
        self._gamekee_waiter_phase: str = ""  # waiter 阶段标记（boot=可接管 / normal=保持排程）
        self._gamekee_commit_lock = threading.Lock()  # 提交与卸载互斥（晚写守卫，P2）
        self._gamekee_probe_generation = 0  # 卸载/重载代际标记：旧线程结果不得发布

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
        """插件加载：配置数据/缓存目录，启动 GameKee 探查后台任务。

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
        self._gamekee_probe_task = asyncio.create_task(self._schedule_gamekee_probe())
        self.ctx.logger.info("星塔旅人插件已加载，数据目录: %s", self._data_dir)

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
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            await asyncio.gather(self._update_task, return_exceptions=True)
        self._update_task = None
        self.ctx.logger.info("星塔旅人插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        """配置热重载：黑白名单与 GameKee 参数即时生效，无需重启。"""
        self.ctx.logger.info(
            "配置已更新: scope=%s version=%s（黑白名单与自定义覆盖即时生效）", scope, version
        )
        self._apply_overrides_config()


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
        """执行一次 GameKee 探查/重建（三入口统一体，P2 全纪律，single-flight）。

        - 合并请求（single-flight）：并发入口共享同一 asyncio.Future——首个入口
          创建批次并真实探查，其余入口 await 同一 future 拿同一结果（成功复用索引、
          失败复用异常），不重复发起网络请求；
        - batch 终态统一由本方法负责：成功 set_result、异常 set_exception、
          创建后未及执行被取消时兜底 set_exception——等待者永不挂起。
        """
        while True:
            batch = self._gamekee_batch
            if batch is not None:
                # 已有在飞批次：shield 包裹——等待者被取消不影响共享批次
                #（batch 继续由首批 executor 驱动到终态），其他等待者不受波及。
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
            try:
                result = await self._probe_batch_execute(trigger)
                if not batch.done():
                    batch.set_result(result)
                return result
            except asyncio.CancelledError:
                if not batch.done():
                    batch.set_exception(GameKeeProbeFailure("探查被取消"))
                    batch.exception()  # 创建者消费，防悬挂告警
                raise
            except BaseException as exc:
                if not batch.done():
                    failure = exc if isinstance(exc, GameKeeProbeFailure) else GameKeeProbeFailure(str(exc))
                    batch.set_exception(failure)
                    # 无并发等待者时 batch 异常无人取回会触发 asyncio 告警——
                    # 创建者自己消费一次（异常本体已从 executor 抛出并被捕获传播）
                    batch.exception()
                raise
            finally:
                if self._gamekee_batch is batch:
                    self._gamekee_batch = None

    async def _probe_batch_execute(self, trigger: str) -> Dict[str, Any]:
        """single-flight 批次主体：轻量探查 → 无变化仅推进时间戳，有变化才完整重建。

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
            if not probe.get("changed"):
                # 六页无变化：仅推进 last_verified，不重建索引（P2 探查语义）。
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
            # 2. 有变化：完整重建（IO 全部在 to_thread）
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

        索引不可用（None）或 last_verified 距今超过 index_ttl_minutes → 先探查
        再返回；gate 保证并发查询共享同一次探查。探查失败时：有旧索引则带旧索引
        降级服务（并发等来的失败结果同样走这条旧快照路径，不隐式重试）；无旧索引
        则向上抛（handler 转用户可见"攻略源暂不可用"）。
        """
        ttl_seconds = max(1, int(self.config.gamekee.index_ttl_minutes)) * 60
        last_verified = float(self._gamekee_state.get("last_verified") or 0)
        needs_refresh = (
            self._gamekee_index is None
            or not last_verified
            or (self._now() - last_verified) > ttl_seconds
        )
        if not needs_refresh:
            return self._gamekee_index  # type: ignore[return-value]
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
        return {"action": "continue"}

    def _strip_tool_definitions(self, defs: list, kwargs: dict, session_id: str) -> dict:
        """从工具定义清单剔除本插件工具并回填 modified_kwargs（整体替换语义）。"""
        kept = [
            d for d in defs
            if not isinstance(d, dict) or d.get("function", {}).get("name") not in _TOOL_NAMES
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
                    "（GameKee 攻略组授权转载）。'XX攻略/XX怎么玩/风队怎么玩'调用本工具。"
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
        """一图流攻略查询（1.5.0 GameKee 源）：匹配 → 下载 → 合并转发图片。

        P3 两层鉴权：执行层在 _denied（黑白名单）之外先核平台——非 QQ 或
        平台反查失败（未知）保守拒绝且不发起任何抓取（新链路每次调用有
        真实网络成本，宁可漏不可错）。
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
        self.ctx.logger.info("how 查询: %s (group=%s user=%s)", query, kwargs.get("group_id", ""), kwargs.get("user_id", ""))

        stream_id = self._resolve_stream_id(kwargs)
        if not stream_id:
            return {"name": "stellasora_how", "content": "缺少发送目标（stream_id）。"}

        # 懒刷新拿索引（P2 入口③：fresh 零探查；stale 先刷新；失败带旧快照/上抛）
        try:
            index = await self.ensure_gamekee_index()
        except GameKeeProbeFailure as exc:
            self.ctx.logger.error("GameKee 索引不可用: %s", exc)
            await self.ctx.send.text("攻略源暂时不可用，请稍后再试。", stream_id)
            return {"name": "stellasora_how", "content": "已向聊天发送'攻略源暂时不可用'提示，请调用 stop 结束本轮。"}

        # 匹配（同步轻活，直接执行）
        hits, miss_reason = gamekee_match_guides(index, query, aliases=self._resolve_guide_aliases())
        if not hits:
            self.ctx.logger.info("how 未命中: query=%s reason=%s", query, miss_reason)
            await self.ctx.send.text("攻略库里暂时没有该角色/属性的图流攻略，换个说法或换个角色试试～", stream_id)
            return {
                "name": "stellasora_how",
                "content": "已向聊天发送'未找到相关攻略'提示。你不需要也不应该再调用 reply 工具，请立即调用 stop 工具结束本轮。",
            }

        # 下载图片（同步重活进线程池；webp 可配）
        prefer_webp = bool(self.config.gamekee.prefer_webp)
        cache_dir = Path(self.ctx.paths.runtime_dir) / "cache" / "gamekee_images"
        images: list[dict] = []
        download_errors: list[str] = []
        for hit in hits:
            src = hit["image_urls"][-1]  # 末张 = 完整版长图
            try:
                data = await asyncio.to_thread(
                    gamekee_download_image, src, cache_dir, prefer_webp
                )
                images.append({
                    "name": hit["name"],
                    "note": hit.get("note") or "",
                    "element_cn": _ELEMENT_CN_LABELS.get(hit.get("element"), hit.get("element", "")),
                    "base64": base64.b64encode(data).decode("ascii"),
                })
            except GameKeeError as exc:
                download_errors.append(f"{hit['name']}: {str(exc)[:120]}")
        if not images:
            self.ctx.logger.error("how 图片全部下载失败: %s", "; ".join(download_errors))
            await self.ctx.send.text("攻略图片下载失败，请稍后再试。", stream_id)
            return {"name": "stellasora_how", "content": "已向聊天发送'图片下载失败'提示，请调用 stop 结束本轮。"}
        if download_errors:
            self.ctx.logger.warning("how 部分图片下载失败（跳过节点）: %s", "; ".join(download_errors))

        # 统一合并转发（用户裁决：单角色/属性泛查都走 forward；单角色=单节点图文混排，
        # 多角色=每角色一节点；节点内备注 + 来源）
        nodes = []
        for img in images:
            segments = [
                {
                    "type": "image",
                    "content": img["base64"],
                    "filename": f"{img['name']}_guide.webp",
                }
            ]
            note_parts = [f"【{img['element_cn']}】{img['name']}"]
            if img["note"]:
                note_parts.append(img["note"])
            note_parts.append("来源：GameKee 星塔旅人 wiki（星塔攻略组）")
            segments.append({"type": "text", "content": "\n".join(note_parts)})
            nodes.append({"nickname": _FORWARD_NICKNAME, "segments": segments})
        try:
            sent = await self.ctx.send.forward(nodes, stream_id)
        except Exception:
            self.ctx.logger.exception("how 合并转发发送异常: stream=%s", stream_id)
            return {"name": "stellasora_how", "content": "攻略图片发送失败，请调用 stop 结束本轮。"}
        if not sent:
            self.ctx.logger.error("how 合并转发返回失败: stream=%s", stream_id)
            return {"name": "stellasora_how", "content": "攻略图片发送失败，请调用 stop 结束本轮。"}
        return {
            "name": "stellasora_how",
            "content": (
                f"攻略图已直接发送到聊天（{len(images)} 张）。用户已经可以看到完整答案，"
                "你不需要也不应该再调用 reply 工具。请立即调用 stop 工具结束本轮。"
            ),
        }

    # ===== Command 指令 =====

    @Command(
        "st_update",
        description="触发星塔旅人数据更新（操作员）：强刷当期两榜数据 + 探查 GameKee 攻略源更新，后台执行完成后报告各榜结果（1.5.0 起不再 reload 插件，数据即时生效）",
        pattern=r"^/st_update",
        permission="operator",
    )
    async def handle_update(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """/st_update 入口（1.5.0）：鉴权 → 提前登记在飞闸门 → 后台执行。

        闸门登记**先于** await 发送进度消息（审查修复：旧实现登记在 await 之后，
        连发两调用可能同时通过）。全程可超宿主组件 RPC 60s 超时，必须 create_task
        后台化；返回拦截等级 2 使触发消息不进 planner。_update_task 兼作任务强引用。
        """
        if not await self._platform_allowed(**kwargs):
            return False, "该平台暂不支持星塔旅人更新指令。", 1
        if self._denied(**kwargs):
            return False, "当前聊天无权限执行星塔旅人更新指令。", 1

        if self._update_task is not None and not self._update_task.done():
            return False, "已有更新任务进行中，请稍候。", 1

        effective_stream_id = stream_id or self._resolve_stream_id(kwargs)
        # 闸门先行：登记任务句柄后再 await 发送（防连发竞态双写数据）
        self._update_task = asyncio.create_task(self._run_full_update(effective_stream_id))
        await self._send_update_msg(
            "正在后台更新星塔旅人数据（强刷两榜 + 探查攻略源）...", effective_stream_id
        )
        return True, "已启动星塔旅人数据更新", 2

    async def _send_update_msg(self, text: str, stream_id: str) -> None:
        """向触发流发送更新进度消息；失败仅告警（进度丢失不影响更新本身）。"""
        if not stream_id:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("发送更新进度消息异常: %s", exc)

    async def _run_full_update(self, stream_id: str) -> None:
        """/st_update 后台全流程（1.5.0）：强刷两榜 + GameKee 探查 + 重置每日计时。

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
        # 探查失败后若接管取消了后台排程，恢复一个唤醒任务（P2 排程恢复规则）
        self._reschedule_after_manual_probe()

        # 2. 强刷两榜（绕过内容 TTL；两榜独立报告）
        board_results = await asyncio.to_thread(refresh_board_for_update, lb_dir)
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
        """
        task = self._gamekee_probe_task
        if task is not None and not task.done():
            return  # 正常等待期间：保持原排程，手动失败不影响
        self._gamekee_probe_task = asyncio.create_task(self._schedule_gamekee_probe())

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
        """排行榜双命令共用体（1.5.0 图片化）：鉴权+平台 → 拉数据 → HTML 渲染 → 合并转发。

        kwargs 必须从命令 handler 透传（宿主注入的 group_id/user_id 为 _denied
        依据，漏传即空身份导致白名单模式全拒）。宿主对命令返回值只记日志不发送
        （self-send 约定），返回值仅作执行摘要。数据层异常（指针缺失/未开榜/
        拉取失败）走短文本；渲染/发送失败记日志给短提示——**不回退文本榜**
        （文本榜排版已被聊天侧否决）。
        """
        if not await self._platform_allowed(**kwargs):
            return False, "该平台暂不支持星塔旅人排行榜查询。", 1
        if self._denied(**kwargs):
            return False, "当前聊天无权限执行星塔旅人排行榜查询。", 1

        effective_stream_id = stream_id or self._resolve_stream_id(kwargs)
        ttl = int(self.config.leaderboard.ttl_minutes)
        lb_dir = self._lb_data_dir_ready()
        try:
            # 1. 指针 + 数据（同步网络/磁盘重活进线程池；异常消息即用户可见文案）
            prefix, sid = await asyncio.to_thread(resolve_current_board, mode, lb_dir)
            cn_data, header, age = await asyncio.to_thread(
                load_board_for_render, prefix, sid, ttl, lb_dir
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

        # 2. HTML 分页 + 渲染（同步 CPU 重活进线程池；渲染本身是宿主 async 能力）
        try:
            pages = await asyncio.to_thread(
                build_board_pages, prefix, sid, cn_data, format_age_text(age)
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

        # 3. 统一合并转发（用户裁决：单页/多页都走 forward，每页一节点）
        nodes = []
        for page, image_b64 in zip(pages, images):
            nodes.append({
                "nickname": _FORWARD_NICKNAME,
                "segments": [
                    {"type": "image", "content": image_b64, "filename": f"{prefix}_{sid}_p{page['start'] // 25 + 1}.png"},
                    {"type": "text", "content": f"{header} {page['label']} · 更新于 {format_age_text(age)}"},
                ],
            })
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
