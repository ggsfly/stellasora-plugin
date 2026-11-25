# -*- coding: utf-8 -*-
"""stellasora_what 资料库：分代只读访问 + 手动增量刷新（1.5.3）。

本模块只做两件事，其余（后台任务、单飞、进度播报、发送）都在接线层：

1. **只读**：`open_current()` 一次调用固定一个已发布代，供查询链路整次复用同一
   代（别名表 / 结构化值 / FTS 必须同库）。没有已发布代与「库损坏」是两种事实，
   分别抛 `WhatLibraryUnavailable(kind="pending"/"corrupt")`——上层文案不能把
   「还没建库」说成「读取失败」。
2. **刷新**：`refresh()` 同步、串行、可取消。先把整批抓到内存并写入**新** staging
   库，校验完整性/外键/FTS 回读，关闭 writer 之后才把 staging 交付成独立代文件，
   最后在取消闸门下原子替换 `current.json`。任何一步失败或取消都不动旧代与旧指针。

为什么取消只能在检查点生效：`to_thread` 里已经发出的 HTTP 请求收不回来。所以取消
语义是「不再发下一篇 + 不推进指针」，而不是「撤回已发的请求」。取消可能留下一个
已交付但未发布的代文件（`reasons.unpublished_db`），但指针绝不前进。

为什么旧代不回收：本轮不删任何在用库——读者可能仍持有旧代连接，删除在用 SQLite
文件会让正在进行的查询报错。多份 `kb-*.sqlite3` 会累积，保留策略由接入层/运维决定。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import email.utils
import gzip
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import net_common  # noqa: E402  代理语义与原子写的单一事实源
import what_db  # noqa: E402
import what_parse  # noqa: E402

# ---------------------------------------------------------------- 上游契约

API_BASE = "https://www.gamekee.com"
# 出站主机白名单是硬约束：初始 URL 与每一次跳转目标都必须落在其中
API_HOSTS = ("www.gamekee.com",)

# 请求头实机验证项（缺 game-alias 会拿到别的游戏的数据）
API_HEADERS = {
    "game-alias": "stellasora",
    "Lang": "zh-cn",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0",
}

TREE_PATH = "/v1/entry/treesByPidV1"
LIST_PATH = "/v1/content/pageList"
DETAIL_PATH = "/v1/content/detail/{cid}"

# 响应体积上限（计划 §2.3）：树/列表 4 MiB、详情 1 MiB
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_DETAIL_BYTES = 1 * 1024 * 1024

# 抓取口径：pageList 里 type ∈ {2, 4} 才是正文型条目
TYPE_ALLOWED = (2, 4)

DEFAULT_REQUEST_INTERVAL = 0.12   # 历史可行值，不是上游配额
DEFAULT_HTTP_TIMEOUT = 20.0
DEFAULT_RETRY_SLEEPS = (4.0, 12.0)  # 5xx/网络：最多两次重试
NO_RETRY_AFTER_COOLDOWN = 1800.0    # 429 无/非法 Retry-After 时的最短冷却（30 分钟）
# 注意：**不设「最长冷却」策略上限**——上游给 48h 就是 48h。只有「非有限」或
# 「换算后 datetime 表示不了」才算无效值、才回落 30 分钟。若将来要缩短长期期限，
# 那是运维决定，必须显式配置，不能在代码里偷偷封顶。
DEFAULT_VERIFY_RATIO = 0.05         # 等戳轮换抽验比例
DEFAULT_PAGE_LIMIT = 100
MAX_PAGES = 50                      # 分页数量运行时计算，超过即视为契约漂移
MAX_REDIRECTS = 3

POINTER_NAME = "current.json"
STATE_NAME = "refresh-state.json"
STAGING_DIRNAME = "staging"
GENERATION_PREFIX = "kb-"
POINTER_SCHEMA = "what-library-1"

STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 解析结果闸门（计划 §2.3 失败纪律）：只有这三类算「入库有效」
# `media_only`（无文字但有媒体）是合法状态，必须保留；
# 坏 JSON / 未知结构 / 正文关键缺失一律不推进代与指针。
PARSE_RETAINED_STATUSES = (
    what_parse.STATUS_OK,
    what_parse.STATUS_MEDIA_ONLY,
    what_parse.STATUS_PARTIAL,
)

_GENERATION_FILE_RE = re.compile(r"^kb-[0-9A-Za-z._\-]+\.sqlite3$")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{3,}")

_USER_TEXT = {
    "pending": "资料库还没建好，请让有权限的操作员执行 /st_update",
    "corrupt": "读取失败",
}


# ---------------------------------------------------------------- 异常


class WhatLibraryError(Exception):
    """库侧错误基类。"""


class WhatLibraryUnavailable(WhatLibraryError):
    """没有可读的已发布代。`kind` 决定上层给哪一句 QQ 文案。"""

    def __init__(self, kind: str, detail: str = "", *, reason: str = "") -> None:
        self.kind = kind          # pending（还没建好）/ corrupt（读不出来）
        self.reason = reason      # 机器可判的原因码
        self.detail = detail
        super().__init__(f"{kind}/{reason or '-'}: {detail}" if detail else f"{kind}/{reason or '-'}")

    @property
    def user_text(self) -> str:
        return _USER_TEXT[self.kind]


class FetchError(WhatLibraryError):
    """默认传输层的网络失败（注入的 fetch 也可抛它）。"""


class _Cancelled(Exception):
    """内部：观察到取消信号。"""


class _Stop(Exception):
    """内部：本批必须停止（不发布）。"""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        stage: str = "",
        content_id: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.stage = stage
        self.content_id = content_id
        self.retry_after = retry_after
        super().__init__(f"{code}: {detail}" if detail else code)


class _VerifyFailed(Exception):
    """内部：新库未通过发布前校验。"""


# ---------------------------------------------------------------- 取消


class RefreshCancel:
    """刷新取消令牌：包一层 `threading.Event`，供 asyncio 侧 `call_soon_threadsafe` 置位。"""

    __slots__ = ("event",)

    def __init__(self, event: Optional[threading.Event] = None) -> None:
        self.event = event if event is not None else threading.Event()

    def cancel(self) -> None:
        self.event.set()

    def is_cancelled(self) -> bool:
        return self.event.is_set()


def _as_cancel(obj: Any) -> RefreshCancel:
    if obj is None:
        return RefreshCancel()
    if isinstance(obj, RefreshCancel):
        return obj
    if isinstance(obj, threading.Event):
        return RefreshCancel(obj)
    if isinstance(obj, (bool, int)):
        token = RefreshCancel()
        if bool(obj):
            token.cancel()
        return token
    raise TypeError(f"cancel 只接受 RefreshCancel / threading.Event / None，收到 {type(obj).__name__}")


# ---------------------------------------------------------------- 代信息


@dataclass(frozen=True)
class Generation:
    """一个已发布代（指针里的一份快照）。"""

    name: str
    db_path: Path
    built_at: Optional[str] = None
    last_verified: Optional[str] = None
    articles: Optional[int] = None
    listed: Optional[int] = None
    coverage: Dict[str, Any] = field(default_factory=dict)

    def as_meta(self) -> Dict[str, Any]:
        return {
            "generation": self.name,
            "built_at": self.built_at,
            "last_verified": self.last_verified,
            "article_count": self.articles,
        }

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        """距 last_verified（缺失时退回 built_at）的小时数；无法判定返回 None。"""
        stamp = self.last_verified or self.built_at
        if not stamp:
            return None
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (current - moment).total_seconds() / 3600.0

    def is_stale(self, hours: float, now: Optional[datetime] = None) -> bool:
        """只回答「是否需要提示」，不触发任何刷新（采集只走 /st_update）。"""
        age = self.age_hours(now)
        return age is not None and age > float(hours)


@dataclass
class RefreshResult:
    """一次刷新的结果。字段都短，`/st_update` 直接照抄即可。"""

    status: str = STATUS_FAILED
    stage: str = ""
    generation: Optional[str] = None
    previous_generation: Optional[str] = None
    listed: int = 0                 # 本次清单目标篇数（type∈{2,4} 且排除 how_owned）
    articles: int = 0               # 新代入库篇数
    added: int = 0
    refetched: int = 0
    reused: int = 0
    removed: int = 0
    failed: int = 0                 # 本次失败篇数（清单级失败为 0）
    verified: int = 0               # 等戳抽验命中篇数
    done: int = 0
    total: int = 0
    last_verified: Optional[str] = None
    previous_last_verified: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    elapsed_s: float = 0.0
    error: Optional[str] = None
    error_code: Optional[str] = None
    retry_after: Optional[float] = None
    next_allowed_at: Optional[str] = None
    old_generation_ready: bool = False
    previous_unreadable: bool = False  # 指针在，但旧库打不开/篇数不符（既非首建也非「旧库可用」）
    first_build: bool = False
    media_only: int = 0
    partial_parse: int = 0
    reasons: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def published_generation(self) -> Optional[str]:
        """接线层习惯叫法（= generation）。"""
        return self.generation

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "generation": self.generation,
            "previous_generation": self.previous_generation,
            "listed": self.listed,
            "articles": self.articles,
            "added": self.added,
            "refetched": self.refetched,
            "reused": self.reused,
            "removed": self.removed,
            "failed": self.failed,
            "verified": self.verified,
            "done": self.done,
            "total": self.total,
            "last_verified": self.last_verified,
            "previous_last_verified": self.previous_last_verified,
            "last_attempt_at": self.last_attempt_at,
            "last_failure_at": self.last_failure_at,
            "elapsed_s": round(self.elapsed_s, 2),
            "error": self.error,
            "error_code": self.error_code,
            "retry_after": self.retry_after,
            "next_allowed_at": self.next_allowed_at,
            "old_generation_ready": self.old_generation_ready,
            "previous_unreadable": self.previous_unreadable,
            "first_build": self.first_build,
            "media_only": self.media_only,
            "partial_parse": self.partial_parse,
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------- 传输


@dataclass
class FetchResponse:
    """注入式传输层的返回：状态码 + 原始字节 + 小写头名。"""

    status: int
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跳转：每跳先校验主机再发请求。

    `net_common` 里同款处理是私有名，本模块不改动 net_common，故此处保留等价实现。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        return False
    if (parsed.hostname or "").lower() not in API_HOSTS:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return port in (None, 443)


def _header(name: str, headers: Dict[str, str]) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _retry_after_valid(seconds: Optional[float], now: datetime) -> Optional[float]:
    """冷却秒数是否可用：有限、非负，且 `now + seconds` 的 datetime 表示得出来。

    只有这两类算无效（→ `None`，调用方回落 30 分钟）。**没有策略上限**：
    上游给 48h 就保留 48h，代码不替上游缩短期限。
    """
    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    try:
        now + timedelta(seconds=value)
    except (OverflowError, ValueError):
        return None
    return value


def _retry_after_seconds(
    headers: Dict[str, str], *, now: Optional[datetime] = None
) -> Optional[float]:
    """把 Retry-After 换成冷却秒数：数字秒与合法 HTTP-date **同等尊重**。

    - 数字秒：有限、非负、换算后 datetime 表示得出来就采用（48h/172800 保留）
    - HTTP-date（RFC 1123/822，上游真实写法）：按 `now`（注入时钟）换算成剩余秒；
      过去的日期给 0（立即可再试），不是「无穷冷却」
    - 脏文本 / NaN / Infinity / 超出 datetime 可表示范围 → None，调用方回落到
      `NO_RETRY_AFTER_COOLDOWN`，绝不把非法值写进 `next_allowed_at`
    """
    raw = _header("retry-after", headers)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    try:
        numeric = float(text)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None:
        return _retry_after_valid(numeric, current)

    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:  # HTTP-date 一律按 GMT 解释
        moment = moment.replace(tzinfo=timezone.utc)
    remaining = (moment - current).total_seconds()
    if not math.isfinite(remaining):
        return None
    if remaining <= 0:
        return 0.0
    return _retry_after_valid(remaining, current)


# ---------------------------------------------------------------- 刷新锁


_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.Lock] = {}


def _root_lock(root: Path) -> threading.Lock:
    """同一进程内、同一 root 只允许一个刷新；不同 root 互不影响。

    用不可重入锁：同一线程里再调 `refresh()`（例如插件回调里误重入）也必须被拒，
    否则两层刷新会同时写指针。
    """
    key = os.path.normcase(str(root.absolute()))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


_how_owned_cache: Optional[frozenset] = None


def how_owned_ids() -> frozenset:
    """how 已在消费的六属性页 id（复用 `gamekee.ELEMENT_PAGES`，不复制常量）。"""
    global _how_owned_cache
    if _how_owned_cache is None:
        import gamekee  # 延迟导入：本模块装载时不碰同目录其他模块的默认值

        _how_owned_cache = frozenset(int(v) for v in gamekee.ELEMENT_PAGES.values())
    return _how_owned_cache


# ---------------------------------------------------------------- 主类


class WhatLibrary:
    """插件私有 `data_dir/what` 的代际资料库。

    `fetch` / `clock` / `sleep` 都是可注入依赖（测试用来替代网络与时间）；
    默认传输层走 `net_common.build_opener` 的代理链，失败不换代理。
    """

    def __init__(
        self,
        root: Any,
        *,
        fetch: Optional[Callable[..., FetchResponse]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        request_interval: float = DEFAULT_REQUEST_INTERVAL,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        retry_sleeps: Tuple[float, ...] = DEFAULT_RETRY_SLEEPS,
        verify_ratio: float = DEFAULT_VERIFY_RATIO,
        page_limit: int = DEFAULT_PAGE_LIMIT,
    ) -> None:
        self.root = Path(root)
        self._fetch = fetch
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep
        self.request_interval = float(request_interval)
        self.http_timeout = float(http_timeout)
        self.retry_sleeps = tuple(float(x) for x in retry_sleeps)
        self.verify_ratio = float(verify_ratio)
        self.page_limit = int(page_limit)

        self._lock = _root_lock(self.root)
        self._opener: Optional[urllib.request.OpenerDirector] = None
        self._requests = 0

    # ------------------------------------------------------------ 只读

    @contextmanager
    def open_current(self) -> Iterator[Tuple[sqlite3.Connection, Generation]]:
        """固定一个已发布代的只读连接。

        调用方必须在**同一个线程**里 open → 查询 → 关闭（`sqlite3` 默认
        `check_same_thread=True`）；跨线程持有连接会直接报错。
        """
        pointer, pointer_state = self._read_pointer()
        if pointer_state == "missing":
            reason = "staging_only" if self._staging_present() else "no_generation"
            raise WhatLibraryUnavailable("pending", "还没有已发布的资料代", reason=reason)
        if pointer is None:
            raise WhatLibraryUnavailable("corrupt", "current.json 不可读", reason="pointer_invalid")

        generation = self._generation_from_pointer(pointer)
        try:
            conn = what_db.open_db(str(generation.db_path), readonly=True)
        except (sqlite3.Error, OSError) as exc:
            raise WhatLibraryUnavailable(
                "corrupt", f"库打不开: {generation.db_path.name}: {exc}", reason="open_failed"
            ) from exc

        try:
            self._check_generation(conn, expected_articles=generation.articles)
        except WhatLibraryError:
            conn.close()
            raise
        try:
            yield conn, generation
        finally:
            conn.close()

    def _check_generation(
        self, conn: sqlite3.Connection, *, expected_articles: Optional[int] = None
    ) -> None:
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            raise WhatLibraryUnavailable("corrupt", f"PRAGMA 读取失败: {exc}", reason="pragma_failed") from exc
        try:
            version = int(row[0]) if row else -1
        except (TypeError, ValueError):
            version = -1
        if version != what_db.SCHEMA_VERSION:
            raise WhatLibraryUnavailable(
                "corrupt",
                f"库 schema 版本 {version} != 期望 {what_db.SCHEMA_VERSION}",
                reason="schema_version",
            )
        if expected_articles is None:
            return
        try:
            count = int(conn.execute("SELECT COUNT(*) FROM article").fetchone()[0])
        except sqlite3.Error as exc:
            raise WhatLibraryUnavailable("corrupt", f"库读取失败: {exc}", reason="read_failed") from exc
        if count != int(expected_articles):
            # 指针说 N 篇、库里只有 M 篇：文件被截断或指针与库不配套
            raise WhatLibraryUnavailable(
                "corrupt",
                f"库内篇数 {count} != 指针声明 {expected_articles}",
                reason="article_count",
            )

    # ------------------------------------------------------------ 刷新

    def refresh(
        self,
        *,
        cancel: Any = None,
        should_stop: Optional[Callable[[], bool]] = None,
        progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        full: bool = False,
    ) -> RefreshResult:
        """同步刷新一个代：抓清单 → 按 `updated_at` 复用/重抓 → 建新代 → 发布。

        取消：`cancel` 接受 `RefreshCancel` / `threading.Event` / None；`should_stop`
        是等价的回调形式。两者都在每篇请求前与发布前判定，只阻止后续请求与指针推进。
        """
        started = time.monotonic()
        attempt_at = self._now_iso()
        token = _as_cancel(cancel)
        emit = progress or (lambda _event: None)

        if not self._lock.acquire(blocking=False):
            pointer, pointer_state = self._read_pointer()
            return RefreshResult(
                status=STATUS_FAILED,
                stage="lock",
                previous_generation=self._pointer_generation(pointer) if pointer else None,
                old_generation_ready=pointer_state == "ok",
                first_build=pointer_state == "missing",
                last_attempt_at=attempt_at,
                error="已有刷新在进行",
                error_code="refresh_in_progress",
                elapsed_s=time.monotonic() - started,
            )
        try:
            return self._refresh_locked(
                token=token,
                should_stop=should_stop,
                emit=emit,
                full=full,
                attempt_at=attempt_at,
                started=started,
            )
        finally:
            self._lock.release()

    # ------------------------------------------------------------ 刷新主体

    def _refresh_locked(
        self,
        *,
        token: RefreshCancel,
        should_stop: Optional[Callable[[], bool]],
        emit: Callable[[Dict[str, Any]], None],
        full: bool,
        attempt_at: str,
        started: float,
    ) -> RefreshResult:
        result = RefreshResult(
            status=STATUS_FAILED,
            last_attempt_at=attempt_at,
            first_build=True,
        )
        self._requests = 0
        state = self._read_state()
        reasons: List[Dict[str, Any]] = []
        previous: Optional[Generation] = None
        existing: Dict[int, Dict[str, Any]] = {}
        self._parts = _RefreshParts(token=token, should_stop=should_stop, emit=emit, reasons=reasons)

        try:
            previous, existing, result.old_generation_ready = self.load_previous()
            result.previous_generation = previous.name if previous else None
            result.previous_last_verified = previous.last_verified if previous else None
            result.previous_unreadable = previous is not None and not result.old_generation_ready
            result.first_build = previous is None

            self._touch_state(state, last_attempt_at=attempt_at)

            # 0) 冷却闸门：上一次 429 落盘的 next_allowed_at 未到点 → 一个请求都不发。
            #    判的是持久化时刻（refresh-state.json），不是内存里的临时变量，
            #    所以进程重启后同样生效。
            until, remaining = self._cooldown_until(state)
            if until is not None and remaining > 0:
                raise _Stop(
                    "cooldown",
                    f"上次限流冷却未到点（最早 {until.isoformat()}，还差 {remaining:.0f}s）",
                    stage="cooldown",
                    retry_after=remaining,
                )

            # 1) 目录树 + 完整清单（每次刷新都重取：这是全库盘点，不是增量信号）
            result.stage = "tree"
            tree_rows, tree_by_cid = self._fetch_tree()
            result.stage = "list"
            listing = self._fetch_list()
            how_owned = how_owned_ids()
            typed = [
                cid for cid, row in listing["rows"].items() if int(row["type"]) in TYPE_ALLOWED
            ]
            excluded_type = len(listing["rows"]) - len(typed)
            how_hit = sorted(set(typed) & set(how_owned))
            targets = sorted(cid for cid in typed if cid not in how_owned)
            if not targets:
                raise _Stop("no_targets", "清单里没有可入库条目（type∈{2,4} 全被排除）", stage="list")

            result.listed = len(targets)
            self._parts.listing = {
                "list_pages": listing["pages"],
                "list_total": listing["total"],
                "target_count": len(targets),
                "excluded_type": excluded_type,
                "excluded_how_owned": len(how_hit),
                "only_in_tree": sorted(set(tree_by_cid) - set(listing["rows"])),
                "list_page_meta": listing["page_meta"],
            }
            reasons.append(
                {
                    "code": "manifest",
                    "list_pages": listing["pages"],
                    "list_total": listing["total"],
                    "targets": len(targets),
                    "excluded_type": excluded_type,
                    "excluded_how_owned": how_hit,
                }
            )

            # 2) 增量计划：清单 updated_at 是唯一观测信号
            plan: Dict[int, str] = {}
            for cid in targets:
                stored = existing.get(cid)
                observed = listing["rows"][cid].get("updated_at")
                if stored is None or full:
                    plan[cid] = "added" if stored is None else "refetched"
                    continue
                if observed is None:
                    plan[cid] = "refetched"
                    continue
                if _as_int(stored.get("list_updated_at")) != _as_int(observed):
                    plan[cid] = "refetched"
                    continue
                plan[cid] = "reuse"
            result.removed = len(set(existing) - set(targets))

            # 3) 复用：从旧代 raw_gzip 重新解析（解析器升级也自动生效，零网络）
            reused_articles: Dict[int, Any] = {}
            for cid in [c for c, kind in plan.items() if kind == "reuse"]:
                try:
                    article = self.reuse_article(existing[cid], tree_by_cid, listing["rows"][cid])
                    self._guard_article(article, cid=cid, stage="reuse")
                except Exception as exc:  # 旧代里这一行坏了/不可入库 → 该篇改走重抓
                    plan[cid] = "refetched"
                    reasons.append(
                        {"code": "reuse_failed", "content_id": cid,
                         "detail": f"{type(exc).__name__}: {exc}"}
                    )
                    continue
                reused_articles[cid] = article

            # 4) 等戳轮换抽验：比例由注入参数决定，游标跨刷新轮换
            candidates = sorted(cid for cid, kind in plan.items() if kind == "reuse")
            sample_ids, next_cursor = self.sample_slice(candidates, _as_int(state.get("rotation_cursor")))
            state["rotation_cursor"] = next_cursor

            to_fetch = sorted(set(cid for cid, kind in plan.items() if kind != "reuse") | set(sample_ids))
            self._parts.plan = plan
            self._parts.sample_ids = set(sample_ids)
            self._parts.to_fetch = to_fetch
            self._parts.reused_articles = reused_articles

            result.total = len(to_fetch)
            fetched: Dict[int, Any] = {}

            # 5) 逐篇串行抓取（每篇前判取消；失败即整批停，不发布）
            result.stage = "detail"
            emit({"stage": "detail", "done": 0, "total": result.total, "label": "detail"})

            def fetch_into(ids: List[int]) -> None:
                """按顺序抓进 `fetched`；同一 id 只请求一次（已抓过的不重复抓）。"""
                for cid in ids:
                    if cid in fetched:
                        continue
                    self._check_cancel()
                    try:
                        data = self._fetch_detail(cid)
                    except _Stop as stop:
                        # 详情级失败必须带上 content_id：终报要能说「第几篇、什么错」，
                        # 而清单/树级失败没有单篇归属。
                        if stop.content_id is None:
                            stop.content_id = cid
                        raise
                    article = what_parse.parse_raw_dir_entry(
                        {"code": 0, "msg": "", "data": data},
                        tree_path=(tree_by_cid.get(cid) or {}).get("path"),
                        list_item=listing["rows"].get(cid),
                        list_updated_at=(listing["rows"].get(cid) or {}).get("updated_at"),
                    )
                    if int(article.content_id) != int(cid):
                        raise _Stop(
                            "detail_id_mismatch",
                            f"清单 {cid} 返回的 content_id={article.content_id}",
                            stage="detail",
                            content_id=cid,
                        )
                    # 解析闸门：坏 JSON / 未知结构 / 关键缺失不进代，指针不推进
                    self._guard_article(article, cid=cid, stage="detail")
                    fetched[cid] = article
                    result.done = len(fetched)
                    emit({"stage": "detail", "done": result.done, "total": result.total, "label": f"detail {cid}"})

            fetch_into(to_fetch)

            # 5b) 等戳异文升级（计划 §2.4）：抽验命中「戳没变但正文 hash 变了」→
            #     本轮把**其余等戳条目也抓齐**再发布，不偷偷只换这一篇就推进指针；
            #     升级抓取里任何一篇失败都整批停（已发布代与指针原样保留）。
            mismatched = sorted(
                cid for cid in sample_ids
                if cid in fetched
                and fetched[cid].raw_sha256 != (existing.get(cid) or {}).get("raw_sha256")
            )
            escalated: List[int] = []
            if mismatched:
                escalated = sorted(
                    cid for cid, kind in plan.items()
                    if kind == "reuse" and cid not in fetched
                )
                reasons.append(
                    {
                        "code": "equal_stamp_hash_mismatch",
                        "content_id": mismatched[0],
                        "mismatched": mismatched,
                        "promoted": escalated,
                        "detail": f"等戳异文 {len(mismatched)} 篇 → 同批抓齐其余 {len(escalated)} 篇等戳条目",
                    }
                )
                if escalated:
                    result.total += len(escalated)
                    self._parts.escalated = escalated
                    emit({"stage": "detail", "done": result.done, "total": result.total,
                          "label": f"verify-promote {len(escalated)}"})
                    fetch_into(escalated)

            # 6) 组装整代（顺序 = 清单顺序，便于比对）
            articles: List[Any] = []
            for cid in targets:
                article = fetched.get(cid)
                if article is None:
                    article = reused_articles.get(cid)
                if article is None:
                    raise _Stop("internal", f"目标 {cid} 既未抓取也无可复用行", stage="assemble", content_id=cid)
                articles.append(article)

            self._count_buckets(result, plan, sample_ids, fetched, existing, escalated)
            if result.added + result.refetched + result.reused + result.verified != len(targets):
                raise _Stop(
                    "internal",
                    "入库分桶与清单数不一致"
                    f"（added={result.added} refetched={result.refetched} reused={result.reused}"
                    f" verified={result.verified} targets={len(targets)}）",
                    stage="assemble",
                )

            result.media_only = sum(1 for a in articles if a.status == what_parse.STATUS_MEDIA_ONLY)
            result.partial_parse = sum(1 for a in articles if a.status == what_parse.STATUS_PARTIAL)

            # 7) 建新代 → 校验 → 发布
            result.stage = "build"
            emit({"stage": "build", "done": 0, "total": len(articles), "label": "build"})
            built_at = self._now_iso()
            self._parts.built_at = built_at
            try:
                staging_path, expected_chunks = self.build_staging(
                    articles, tree_rows, listing, len(targets), built_at, full, result.first_build
                )
            except (sqlite3.Error, OSError) as exc:
                raise _Stop("build_failed", f"写 staging 库失败: {type(exc).__name__}: {exc}", stage="build")
            result.stage = "verify"
            emit({"stage": "verify", "done": 0, "total": 1, "label": "verify"})
            self.verify_staging(staging_path, expected_articles=len(articles), expected_chunks=expected_chunks)

            # 8) 发布闸门：三次判停，指针只在最后一次之后推进
            self._check_cancel()
            generation, published_path = self._deliver_generation(staging_path)
            self._check_cancel()
            last_verified = self._now_iso()
            self._write_pointer(
                generation=generation,
                db_name=published_path.name,
                built_at=built_at,
                last_verified=last_verified,
                articles=len(articles),
                listed=len(targets),
                coverage=self._parts.listing,
            )
            emit({"stage": "publish", "done": 1, "total": 1, "label": generation})

            result.status = STATUS_PUBLISHED
            result.stage = "publish"
            result.generation = generation
            result.articles = len(articles)
            result.last_verified = last_verified
            result.done = result.total
            self._touch_state(
                state,
                last_verified=last_verified,
                last_failure_at=None,
                last_error=None,
                next_allowed_at=None,
            )
            self._finish(result, reasons, started)
            return result

        except _Cancelled as cancel:
            result.status = STATUS_CANCELLED
            result.error = "已取消，未发布新代"
            result.error_code = "cancelled"
            result.last_verified = previous.last_verified if previous else None
            if getattr(self._parts, "delivered_db", None):
                reasons.append(
                    {"code": "unpublished_db", "db": str(self._parts.delivered_db),
                     "detail": "代文件已交付但指针未推进，可人工清理"}
                )
            result.stage = result.stage or "cancel"
            self._touch_state(state, last_failure_at=None)
            self._finish(result, reasons, started)
            return result

        except _Stop as stop:
            result.status = STATUS_FAILED
            result.stage = stop.stage or (result.stage or "unknown")
            result.error_code = stop.code
            result.error = stop.detail
            result.failed = 1 if stop.content_id is not None else 0
            reasons.append(
                {
                    "code": stop.code,
                    "detail": stop.detail,
                    "stage": stop.stage,
                    "content_id": stop.content_id,
                }
            )
            # 非法/非有限/超出 datetime 可表示范围的值绝不写进 next_allowed_at
            # （合法长期限——例如 48h——原样保留，不在这里封顶）
            retry_after = _retry_after_valid(stop.retry_after, self._now())
            if retry_after is not None:
                result.retry_after = retry_after
                result.next_allowed_at = self._iso_in(retry_after)
            elif stop.code == "rate_limited":
                result.next_allowed_at = self._iso_in(NO_RETRY_AFTER_COOLDOWN)
            result.last_failure_at = self._now_iso()
            result.last_verified = previous.last_verified if previous else None
            self._touch_state(
                state,
                last_failure_at=result.last_failure_at,
                last_error=stop.code,
                next_allowed_at=result.next_allowed_at,
            )
            self._finish(result, reasons, started)
            return result

        except _VerifyFailed as bad:
            result.status = STATUS_FAILED
            result.stage = "verify"
            result.error_code = "verify_failed"
            result.error = str(bad)
            result.last_failure_at = self._now_iso()
            result.last_verified = previous.last_verified if previous else None
            reasons.append({"code": "verify_failed", "detail": str(bad)})
            self._touch_state(
                state, last_failure_at=result.last_failure_at, last_error=result.error_code
            )
            self._finish(result, reasons, started)
            return result

        except Exception as exc:  # 未预期异常也要以结果形式暴露，接线层负责记日志
            result.status = STATUS_FAILED
            result.error_code = "internal"
            result.error = f"{type(exc).__name__}: {exc}"
            result.last_failure_at = self._now_iso()
            result.last_verified = previous.last_verified if previous else None
            reasons.append({"code": "internal", "detail": result.error})
            self._finish(result, reasons, started)
            return result

    # ------------------------------------------------------------ 刷新：清单

    def _fetch_tree(self) -> Tuple[List[Tuple[Any, ...]], Dict[int, Dict[str, Any]]]:
        payload = self._get_json(TREE_PATH, max_bytes=MAX_MANIFEST_BYTES, stage="tree")
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise _Stop("contract_drift", "treesByPidV1 的 data 不是非空数组", stage="tree")

        rows: List[Tuple[Any, ...]] = []
        by_cid: Dict[int, Dict[str, Any]] = {}

        def walk(nodes: Any, path: str) -> None:
            if not isinstance(nodes, list):
                raise _Stop("contract_drift", f"目录树 child 不是数组（{path or '/'}）", stage="tree")
            for node in nodes:
                if not isinstance(node, dict):
                    raise _Stop("contract_drift", f"目录树节点不是对象（{path or '/'}）", stage="tree")
                name = node.get("name")
                if not isinstance(name, str) or not name:
                    raise _Stop("contract_drift", f"目录树节点缺少 name（{path or '/'}）", stage="tree")
                here = f"{path}/{name}"
                node_id = _as_int(node.get("id"))
                if node_id is None:
                    raise _Stop("contract_drift", f"目录树节点缺少 id（{here}）", stage="tree")
                cid = _as_int(node.get("content_id"))
                parent = _as_int(node.get("pid"))
                rows.append((node_id, parent, name, here, cid))
                if cid is not None:
                    by_cid.setdefault(cid, {"node_id": node_id, "name": name, "path": here})
                walk(node.get("child") or [], here)

        walk(data, "")
        self._parts.emit({"stage": "tree", "done": 1, "total": 1, "label": f"{len(by_cid)} 篇"})
        return rows, by_cid

    def _fetch_list(self) -> Dict[str, Any]:
        page_no = 1
        meta_expected: Optional[Tuple[int, int]] = None
        rows: Dict[int, Dict[str, Any]] = {}
        duplicates: List[int] = []
        pages = 0
        total: Optional[int] = None
        while True:
            payload = self._get_json(
                f"{LIST_PATH}?page_no={page_no}&limit={self.page_limit}",
                max_bytes=MAX_MANIFEST_BYTES,
                stage="list",
            )
            pagination = (payload.get("meta") or {}).get("pagination")
            if not isinstance(pagination, dict):
                raise _Stop("contract_drift", f"第 {page_no} 页缺少 meta.pagination", stage="list")
            page_meta = {
                "page_no": _as_int(pagination.get("page_no")),
                "page_total": _as_int(pagination.get("page_total")),
                "total": _as_int(pagination.get("total")),
                "limit": _as_int(pagination.get("limit")),
            }
            if page_meta["page_no"] != page_no:
                raise _Stop(
                    "contract_drift",
                    f"请求第 {page_no} 页但 pagination.page_no={page_meta['page_no']}",
                    stage="list",
                )
            if page_meta["page_total"] is None or page_meta["total"] is None:
                raise _Stop("contract_drift", f"第 {page_no} 页 pagination 缺字段", stage="list")
            if meta_expected is None:
                meta_expected = (page_meta["page_total"], page_meta["total"])
                total = page_meta["total"]
            elif meta_expected != (page_meta["page_total"], page_meta["total"]):
                raise _Stop(
                    "contract_drift",
                    f"分页元信息中途漂移：{meta_expected} → "
                    f"({page_meta['page_total']}, {page_meta['total']})",
                    stage="list",
                )

            data = payload.get("data")
            if not isinstance(data, list):
                raise _Stop("contract_drift", f"第 {page_no} 页 data 不是数组", stage="list")
            if not data:
                raise _Stop(
                    "page_gap",
                    f"第 {page_no}/{page_meta['page_total']} 页为空（缺页）",
                    stage="list",
                )
            for item in data:
                row = self._validate_list_row(item, page_no)
                cid = int(row["id"])
                if cid in rows:
                    duplicates.append(cid)
                    continue
                rows[cid] = row
            pages += 1
            self._parts.emit(
                {"stage": "list", "done": page_no, "total": page_meta["page_total"], "label": f"page {page_no}"}
            )
            if page_no >= page_meta["page_total"]:
                break
            page_no += 1
            if page_no > MAX_PAGES:
                raise _Stop("contract_drift", f"分页超过 {MAX_PAGES} 页上限", stage="list")

        if duplicates:
            raise _Stop(
                "duplicate_list_ids",
                f"清单出现重复 id: {sorted(set(duplicates))[:10]}",
                stage="list",
            )
        if total is not None and len(rows) != total:
            raise _Stop(
                "list_total_mismatch",
                f"去重后唯一 id {len(rows)} != 声明 total {total}",
                stage="list",
            )
        return {
            "rows": rows,
            "pages": pages,
            "total": total,
            "page_meta": meta_expected,
        }

    def _validate_list_row(self, item: Any, page_no: int) -> Dict[str, Any]:
        if not isinstance(item, dict):
            raise _Stop("invalid_list_row", f"第 {page_no} 页存在非对象行", stage="list")
        cid = _as_int(item.get("id"))
        if cid is None or cid <= 0:
            raise _Stop("invalid_list_row", f"第 {page_no} 页行 id 非法: {item.get('id')!r}", stage="list")
        row_type = _as_int(item.get("type"))
        if row_type is None:
            raise _Stop("invalid_list_row", f"条目 {cid} 缺少 type", stage="list", content_id=cid)
        title = item.get("title")
        if not isinstance(title, str):
            raise _Stop("invalid_list_row", f"条目 {cid} 的 title 不是字符串", stage="list", content_id=cid)
        updated_at = item.get("updated_at")
        if updated_at is not None and _as_int(updated_at) is None:
            raise _Stop(
                "invalid_list_row", f"条目 {cid} 的 updated_at 非法: {updated_at!r}", stage="list", content_id=cid
            )
        return {
            "id": cid,
            "type": row_type,
            "title": title,
            "updated_at": _as_int(updated_at),
            "entry_id": _as_int(item.get("entry_id")),
            "summary": item.get("summary") if isinstance(item.get("summary"), str) else None,
            "image_num": _as_int(item.get("image_num")),
            "word_count": _as_int(item.get("word_count")),
        }

    # ------------------------------------------------------------ 刷新：详情

    def _fetch_detail(self, content_id: int) -> Dict[str, Any]:
        payload = self._get_json(
            DETAIL_PATH.format(cid=content_id), max_bytes=MAX_DETAIL_BYTES, stage="detail"
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _Stop(
                "invalid_detail", f"detail {content_id} 缺少 data 段", stage="detail", content_id=content_id
            )
        raw_cid = _as_int(data.get("content_id"))
        if raw_cid is None:
            raw_cid = _as_int(data.get("id"))
        if raw_cid != content_id:
            raise _Stop(
                "detail_id_mismatch",
                f"请求 {content_id} 但 detail 返回 {raw_cid}",
                stage="detail",
                content_id=content_id,
            )
        if not isinstance(data.get("content_json"), str):
            raise _Stop(
                "invalid_detail",
                f"detail {content_id} 的 content_json 不是字符串",
                stage="detail",
                content_id=content_id,
            )
        return data

    # ------------------------------------------------------------ 刷新：解析闸门

    def _guard_article(self, article: Any, *, cid: int, stage: str) -> None:
        """解析结果闸门：只放行 ok / media_only / partial_parse。

        坏 JSON、未知结构、正文关键缺失（空 `content_json`、结构可枚举但无任何
        可入库证据）一律抛 `_Stop`：本批不建代、不动指针，旧代继续服务。
        `media_only`（无文字但有媒体）是合法状态，必须保留。
        """
        status = getattr(article, "status", None)
        if status not in PARSE_RETAINED_STATUSES:
            kind = (
                "正文关键缺失"
                if status in (what_parse.STATUS_EMPTY, what_parse.STATUS_NO_TEXT)
                else "不可入库结构"
            )
            raise _Stop(
                "bad_article",
                f"detail {cid} 解析状态 {status!r}（{kind}），本批不发布",
                stage=stage,
                content_id=cid,
            )
        if getattr(article, "entity_name_source", None) == "missing":
            raise _Stop(
                "missing_identity",
                f"detail {cid} 既无目录归属也无题名，无法在库里定位",
                stage=stage,
                content_id=cid,
            )

    # ------------------------------------------------------------ 刷新：冷却闸门

    def _cooldown_until(self, state: Dict[str, Any]) -> Tuple[Optional[datetime], float]:
        """读持久化的 `next_allowed_at`：返回（最早可再试时刻，剩余秒数）。

        非法/缺失/无法解析一律回 (None, 0.0)，即「没有冷却」——不凭空造一个
        冷却来掩盖上游问题，也不因脏值永久锁死刷新。
        """
        raw = state.get("next_allowed_at")
        if not isinstance(raw, str) or not raw.strip():
            return None, 0.0
        try:
            until = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None, 0.0
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until, (until - self._now()).total_seconds()

    # ------------------------------------------------------------ 传输：请求

    def _get_json(self, path: str, *, max_bytes: int, stage: str) -> Dict[str, Any]:
        url = API_BASE + path
        body = self._request_bytes(url, max_bytes=max_bytes, stage=stage)
        try:
            payload = json.loads(body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise _Stop("verification_page", f"{path} 非 UTF-8 文本（疑似验证页）: {exc}", stage=stage)
        except json.JSONDecodeError as exc:
            head = body[:96].decode("utf-8", "replace")
            stripped = head.lstrip()
            if not stripped.startswith(("{", "[")):
                raise _Stop(
                    "verification_page", f"{path} 非 JSON 响应（疑似验证页）: {head!r}", stage=stage
                )
            raise _Stop("contract_drift", f"{path} JSON 解析失败: {exc}", stage=stage)
        if not isinstance(payload, dict):
            raise _Stop("contract_drift", f"{path} 顶层不是对象", stage=stage)
        if _as_int(payload.get("code")) != 0:
            raise _Stop("contract_drift", f"{path} 返回 code={payload.get('code')!r}", stage=stage)
        return payload

    def _request_bytes(self, url: str, *, max_bytes: int, stage: str) -> bytes:
        attempts = 0
        while True:
            self._check_cancel()
            if self._requests:
                self._sleep(self.request_interval)
            self._requests += 1
            try:
                response = self._call_fetch(url, max_bytes)
            except FetchError as exc:
                if attempts < len(self.retry_sleeps):
                    self._sleep(self.retry_sleeps[attempts])
                    attempts += 1
                    continue
                raise _Stop("network_error", f"{url}: {exc}", stage=stage) from exc

            status = _as_int(response.status)
            if status is None:
                raise _Stop("internal", f"传输层返回非法状态码: {response.status!r}", stage=stage)
            if status == 200:
                if len(response.body) > max_bytes:
                    raise _Stop(
                        "oversize",
                        f"{url} 响应 {len(response.body)} 字节 > 上限 {max_bytes}",
                        stage=stage,
                    )
                return response.body
            if status == 429:
                # 尊重 Retry-After：本批立即停止，冷却时间只作为「何时可再试」的说明，
                # 不在这里 sleep 等待，也不换代理、不加压重试。
                retry_after = _retry_after_seconds(response.headers or {}, now=self._now())
                raise _Stop(
                    "rate_limited",
                    f"{url} HTTP 429（Retry-After={retry_after}）",
                    stage=stage,
                    retry_after=retry_after,
                )
            if status in (401, 403):
                raise _Stop("auth_denied", f"{url} HTTP {status}（立即停批）", stage=stage)
            if 500 <= status < 600 and attempts < len(self.retry_sleeps):
                self._sleep(self.retry_sleeps[attempts])
                attempts += 1
                continue
            raise _Stop("http_status", f"{url} HTTP {status}", stage=stage)

    def _call_fetch(self, url: str, max_bytes: int) -> FetchResponse:
        if self._fetch is None:
            return self._default_fetch(url, max_bytes=max_bytes, timeout=self.http_timeout)
        response = self._fetch(url, max_bytes=max_bytes, timeout=self.http_timeout)
        if not isinstance(response, FetchResponse):
            raise _Stop("internal", "注入的 fetch 必须返回 FetchResponse", stage="fetch")
        return response

    def _default_fetch(self, url: str, *, max_bytes: int, timeout: float) -> FetchResponse:
        """默认传输层：`net_common` 的代理链 + 逐跳主机校验（不换代理规避限流）。"""
        if self._opener is None:
            opener = net_common.build_opener(None)
            opener.add_handler(_NoRedirect())
            self._opener = opener
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            if not _url_allowed(current):
                raise FetchError(f"请求主机不在白名单: {urllib.parse.urlparse(current).hostname or current[:80]}")
            request = urllib.request.Request(current, headers=dict(API_HEADERS))
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    status = int(response.getcode())
                    headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
                    body = response.read(max_bytes + 1)
            except urllib.error.HTTPError as exc:
                headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
                # 待核（本轮不改行为）：`_NoRedirect` 让 3xx 以 HTTPError 形式抛出，
                # 这里直接 return，下面的逐跳白名单分支实际不可达 —— 现状是
                # 「任何跳转都拒（上层记 http_status）」，而不是「逐跳校验后再跟」。
                # 修法：3xx 时把 status 置为 exc.code 并落到 1221 的跳转校验分支。
                return FetchResponse(status=int(exc.code), body=b"", headers=headers)
            except Exception as exc:
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
            if status in (301, 302, 303, 307, 308):
                location = _header("location", headers)
                if not location:
                    return FetchResponse(status=status, body=b"", headers=headers)
                target = urllib.parse.urljoin(current, location)
                if not _url_allowed(target):
                    raise FetchError(
                        f"跳转目标主机不在白名单: {urllib.parse.urlparse(target).hostname}"
                    )
                current = target
                continue
            return FetchResponse(status=status, body=body, headers=headers)
        raise FetchError(f"跳转次数超过上限（{MAX_REDIRECTS}）: {url[:120]}")

    # ------------------------------------------------------------ 刷新：取消

    def _check_cancel(self) -> None:
        if self._parts.token.is_cancelled():
            raise _Cancelled()
        if self._parts.should_stop is not None and bool(self._parts.should_stop()):
            raise _Cancelled()

    # ------------------------------------------------------------ 刷新：建库

    def load_previous(self) -> Tuple[Optional[Generation], Dict[int, Dict[str, Any]], bool]:
        """读上一代指针与行（本地操作，不触网）。

        旧代读不出来时返回空行集合并标 `ready=False`：本次会当作全量新建，
        用新代修复——但**不动**旧文件（旧代可能是别人正在读的库）。
        """
        pointer, pointer_state = self._read_pointer()
        if pointer is None or pointer_state != "ok":
            return None, {}, False
        generation = self._generation_from_pointer(pointer)
        try:
            conn = what_db.open_db(str(generation.db_path), readonly=True)
        except (sqlite3.Error, OSError) as exc:
            self._parts.reasons.append(
                {"code": "previous_unreadable", "detail": f"{type(exc).__name__}: {exc}"}
            )
            return generation, {}, False
        try:
            self._check_generation(conn)
            columns = (
                "content_id,title,summary,type,word_count,image_num,list_entry_id,"
                "list_updated_at,detail_updated_at,availability,raw_sha256,raw_gzip,tree_path"
            )
            rows: Dict[int, Dict[str, Any]] = {}
            for row in conn.execute(f"SELECT {columns} FROM article"):
                values = {key: row[key] for key in row.keys()}
                rows[int(values["content_id"])] = values
        except (sqlite3.Error, WhatLibraryUnavailable) as exc:
            self._parts.reasons.append(
                {"code": "previous_unreadable", "detail": f"{type(exc).__name__}: {exc}"}
            )
            return generation, {}, False
        finally:
            conn.close()
        return generation, rows, True

    def reuse_article(
        self,
        stored: Dict[str, Any],
        tree_by_cid: Dict[int, Dict[str, Any]],
        list_row: Dict[str, Any],
    ) -> Any:
        """用旧代存的 `content_json` 原文重新解析（零网络，正文复用）。"""
        content_id = int(stored["content_id"])
        raw = stored.get("raw_gzip")
        if not raw:
            raise ValueError("旧代缺少 raw_gzip")
        content_json = gzip.decompress(raw).decode("utf-8")
        payload = {
            "code": 0,
            "msg": "",
            "data": {
                "id": content_id,
                "content_id": content_id,
                "title": stored.get("title") or "",
                "summary": stored.get("summary") or "",
                "type": stored.get("type"),
                "entry_id": stored.get("list_entry_id"),
                "image_num": stored.get("image_num"),
                "word_count": stored.get("word_count"),
                "updated_at": stored.get("detail_updated_at"),
                "content_json": content_json,
            },
        }
        article = what_parse.parse_raw_dir_entry(
            payload,
            tree_path=(tree_by_cid.get(content_id) or {}).get("path"),
            list_item=list_row,
            list_updated_at=list_row.get("updated_at"),
        )
        if int(article.content_id) != content_id:
            raise ValueError(f"复用解析出的 content_id 不一致: {article.content_id} != {content_id}")
        if stored.get("raw_sha256") and article.raw_sha256 != stored["raw_sha256"]:
            raise ValueError("复用解析出的原文 hash 与旧代不一致")
        return article

    def sample_slice(self, candidates: List[int], cursor: Optional[int]) -> Tuple[List[int], int]:
        """等戳轮换抽验：按比例取一段，游标前移，下一轮从下一段开始。"""
        size = len(candidates)
        if not size or self.verify_ratio <= 0:
            return [], 0
        take = int(size * self.verify_ratio)
        if take <= 0:
            take = 1
        take = min(take, size)
        start = (cursor or 0) % size
        picked = [candidates[(start + i) % size] for i in range(take)]
        return picked, (start + take) % size

    def build_staging(
        self,
        articles: List[Any],
        tree_rows: List[Tuple[Any, ...]],
        listing: Dict[str, Any],
        listed: int,
        built_at: str,
        full: bool,
        first_build: bool,
    ) -> Tuple[Path, int]:
        """写入本次专用 staging 库（新文件；已存在即报错，绝不覆盖）。"""
        staging_dir = self.root / STAGING_DIRNAME
        staging_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._stamp()
        staging_path = staging_dir / f"build-{stamp}.sqlite3"
        attempt = 1
        while staging_path.exists():
            attempt += 1
            staging_path = staging_dir / f"build-{stamp}-{attempt}.sqlite3"

        meta = {
            "library_schema": POINTER_SCHEMA,
            "built_at": built_at,
            "listed": listed,
            "build_reason": "full" if full else ("first_build" if first_build else "incremental"),
            "list_pages": listing["pages"],
            "list_total": listing["total"],
            "schema": "what-1",
            "seg_separator": "U+0001",
        }
        stats = what_db.build_from_articles(staging_path, articles, entries=tree_rows, meta=meta)
        expected_chunks = int(stats.chunks)
        self._parts.staging_path = staging_path
        return staging_path, expected_chunks

    def verify_staging(self, path: Path, *, expected_articles: int, expected_chunks: int) -> None:
        """发布前校验：SQLite 完整性、外键、篇数、FTS5 索引回读与分词对称。"""
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            try:
                conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('integrity-check')")
            except sqlite3.Error as exc:
                raise _VerifyFailed(f"FTS5 integrity-check 失败: {exc}") from exc

            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
            if integrity != ["ok"]:
                raise _VerifyFailed(f"integrity_check: {integrity[:3]}")

            foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                raise _VerifyFailed(f"外键问题 {len(foreign)} 行")

            articles = int(conn.execute("SELECT COUNT(*) FROM article").fetchone()[0])
            if articles != expected_articles:
                raise _VerifyFailed(f"入库篇数 {articles} != 清单 {expected_articles}")

            chunks = int(conn.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])
            if chunks != expected_chunks:
                raise _VerifyFailed(f"chunk 数 {chunks} != 解析结果 {expected_chunks}")

            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != what_db.SCHEMA_VERSION:
                raise _VerifyFailed(f"user_version {version} != {what_db.SCHEMA_VERSION}")

            self._verify_fts_probe(conn)
        finally:
            conn.close()

    def _verify_fts_probe(self, conn: sqlite3.Connection) -> None:
        """用一条真实 chunk 的连续汉字做短语，确认索引写得进、读得回、与原文一致。"""
        probe = conn.execute(
            "SELECT chunk_id, body FROM chunk WHERE length(body) >= 3 ORDER BY chunk_id LIMIT 1"
        ).fetchone()
        if probe is None:
            return
        match = _CJK_RUN_RE.search(probe["body"])
        if not match:
            return
        literal = match.group(0)[:3]
        query = what_db.fts_query(literal)
        if not query:
            return
        rows = conn.execute(
            "SELECT c.chunk_id FROM chunk_fts JOIN chunk c ON c.chunk_id=chunk_fts.rowid"
            " WHERE chunk_fts MATCH ?",
            (query,),
        ).fetchall()
        hit_ids = [int(row[0]) for row in rows]
        if int(probe["chunk_id"]) not in hit_ids:
            raise _VerifyFailed(f"FTS 回读未命中 chunk {probe['chunk_id']}（{literal!r}）")
        instr = int(
            conn.execute("SELECT COUNT(*) FROM chunk WHERE instr(body, ?) > 0", (literal,)).fetchone()[0]
        )
        if len(hit_ids) > instr:
            raise _VerifyFailed(f"FTS 命中 {len(hit_ids)} 多于原文含该串的 {instr} 条（分词不对称）")

    # ------------------------------------------------------------ 发布

    def _deliver_generation(self, staging_path: Path) -> Tuple[str, Path]:
        """把 staging 交付成独立代文件名（no-clobber：绝不覆盖已有代文件）。"""
        base = f"{GENERATION_PREFIX}{self._stamp()}"
        attempt = 0
        while True:
            name = base if attempt == 0 else f"{base}-{attempt + 1}"
            target = self.root / f"{name}.sqlite3"
            attempt += 1
            try:
                os.link(staging_path, target)
            except FileExistsError:
                if attempt > 8:
                    raise _Stop("publish_failed", f"代文件名连续冲突: {base}", stage="publish")
                continue
            except OSError as exc:
                raise _Stop(
                    "publish_failed", f"代文件交付失败: {type(exc).__name__}: {exc}", stage="publish"
                ) from exc
            break
        try:
            staging_path.unlink()
        except OSError:
            pass
        self._parts.delivered_db = target
        return name, target

    def _write_pointer(
        self,
        *,
        generation: str,
        db_name: str,
        built_at: str,
        last_verified: str,
        articles: int,
        listed: int,
        coverage: Dict[str, Any],
    ) -> None:
        payload = {
            "schema": POINTER_SCHEMA,
            "generation": generation,
            "db_file": db_name,
            "built_at": built_at,
            "last_verified": last_verified,
            "articles": articles,
            "listed": listed,
            "coverage": coverage,
        }
        self._parts.pointer_written = True
        net_common.atomic_write(self.root / POINTER_NAME, json.dumps(payload, ensure_ascii=False, indent=1))

    def _read_pointer(self) -> Tuple[Optional[Dict[str, Any]], str]:
        path = self.root / POINTER_NAME
        if not path.is_file():
            return None, "missing"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, "corrupt"
        if not isinstance(payload, dict):
            return None, "corrupt"
        name = payload.get("generation")
        db_name = payload.get("db_file")
        if not isinstance(name, str) or not name or not isinstance(db_name, str):
            return None, "corrupt"
        if not _GENERATION_FILE_RE.match(db_name) or Path(db_name).name != db_name:
            return None, "corrupt"
        if not (self.root / db_name).is_file():
            return None, "corrupt"
        return payload, "ok"

    def _generation_from_pointer(self, pointer: Dict[str, Any]) -> Generation:
        return Generation(
            name=str(pointer.get("generation") or ""),
            db_path=self.root / str(pointer.get("db_file")),
            built_at=_as_text(pointer.get("built_at")),
            last_verified=_as_text(pointer.get("last_verified")),
            articles=_as_int(pointer.get("articles")),
            listed=_as_int(pointer.get("listed")),
            coverage=pointer.get("coverage") if isinstance(pointer.get("coverage"), dict) else {},
        )

    def _pointer_generation(self, pointer: Optional[Dict[str, Any]]) -> Optional[str]:
        return str(pointer.get("generation")) if pointer else None

    def _staging_present(self) -> bool:
        try:
            return any((self.root / STAGING_DIRNAME).glob("*.sqlite3"))
        except OSError:
            return False

    # ------------------------------------------------------------ 诊断状态

    def _read_state(self) -> Dict[str, Any]:
        path = self.root / STATE_NAME
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _touch_state(self, state: Dict[str, Any], **updates: Any) -> None:
        """刷新诊断落盘（与指针分离：避免为时间戳重写正式指针）。写失败只记原因。"""
        state.update(updates)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            net_common.atomic_write(
                self.root / STATE_NAME, json.dumps(state, ensure_ascii=False, indent=1)
            )
        except OSError as exc:
            self._parts.reasons.append(
                {"code": "state_write_failed", "detail": f"{type(exc).__name__}: {exc}"}
            )

    def _count_buckets(
        self,
        result: RefreshResult,
        plan: Dict[int, str],
        sample_ids: List[int],
        fetched: Dict[int, Any],
        existing: Dict[int, Dict[str, Any]],
        escalated: Optional[List[int]] = None,
    ) -> None:
        sampled = set(sample_ids)
        promoted = set(escalated or ())
        result.added = sum(1 for kind in plan.values() if kind == "added")
        result.refetched = sum(1 for kind in plan.values() if kind == "refetched")
        result.reused = sum(
            1 for cid, kind in plan.items()
            if kind == "reuse" and cid not in sampled and cid not in promoted
        )
        verified = 0
        for cid in sampled:
            article = fetched.get(cid)
            stored = existing.get(cid) or {}
            if article is None:
                continue
            if article.raw_sha256 == stored.get("raw_sha256"):
                verified += 1
            else:
                # 等戳异文：这篇的正文 hash 变了，按重抓计（升级抓的兄弟篇也算）
                result.refetched += 1
        result.refetched += len(promoted)
        result.verified = verified

    def _finish(self, result: RefreshResult, reasons: List[Dict[str, Any]], started: float) -> None:
        result.elapsed_s = time.monotonic() - started
        result.reasons = reasons[:20]

    # ------------------------------------------------------------ 时间

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _iso_in(self, seconds: float) -> str:
        return (self._now() + timedelta(seconds=float(seconds))).isoformat()

    def _stamp(self) -> str:
        return self._now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class _RefreshParts:
    """一次刷新内的可变上下文（避免把 20 个参数层层传递）。"""

    token: RefreshCancel
    should_stop: Optional[Callable[[], bool]]
    emit: Callable[[Dict[str, Any]], None]
    reasons: List[Dict[str, Any]]
    listing: Dict[str, Any] = field(default_factory=dict)
    plan: Dict[int, str] = field(default_factory=dict)
    sample_ids: set = field(default_factory=set)
    to_fetch: List[int] = field(default_factory=list)
    reused_articles: Dict[int, Any] = field(default_factory=dict)
    escalated: List[int] = field(default_factory=list)
    built_at: Optional[str] = None
    staging_path: Optional[Path] = None
    delivered_db: Optional[Path] = None
    pointer_written: bool = False


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.lstrip("-").isdigit()):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _as_text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


__all__ = [
    "API_BASE",
    "API_HEADERS",
    "API_HOSTS",
    "FetchError",
    "FetchResponse",
    "Generation",
    "RefreshCancel",
    "RefreshResult",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_PUBLISHED",
    "WhatLibrary",
    "WhatLibraryError",
    "WhatLibraryUnavailable",
    "how_owned_ids",
]
