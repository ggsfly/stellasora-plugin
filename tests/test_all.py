#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星塔旅人插件统一回归测试（单文件精简版）。

运行方式：
    python tests/test_all.py              # 全量
    python tests/test_all.py B G J        # 只跑指定节

测试节（按此顺序执行；B 必须最先——其 json.load 计数断言依赖进程内首次触达 service）：
  B 字典单例与并发  —— dict.json 全进程只解析一次、_init_lock 并发安全、实例共享
  A 字典数据完整性  —— dict.json/names.json 合法性、字段完整、抽样一致、体积上限
  C 术语替换等价性  —— 单遍交替正则 vs 逐条 legacy 在代表样例上全等
  D 中文别名覆盖    —— [overrides.aliases] 模型、DictLookup 链式别名、动态配置
  E 索引页抓取      —— fetch_infodoc_index 缓存命中、URL 正确、异常降级空串
  F how 表驱动查询  —— query_how_rows 按区块抓取、预设码行、rotation 字段、表交集（Task 3 新链路）
  G 直发端到端      —— 直发/缓存/去重/鉴权/人格开关/知识注入/SDK 透传（核心用例）
  H 输出格式        —— LLM 输出原样直发（verbatim trust）+ infodoc 输出规则关键词
  I 非阻塞探针      —— 同步重活在 to_thread 中执行，不阻塞事件循环；异常干净传播
  J 工具参数描述    —— stellasora_how.query 禁止「攻略」等后缀词（群聊回退 bug 回归）
  K 手动更新指令    —— @Command('st_update') 鉴权拦截、授权后台同步、缓存清空与异常降级
  L 定时自动同步    —— 每日 17:00 等待秒数计算、后台定时调度、同步完成清空缓存、on_unload 优雅取消
"""
from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import json
import logging
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DICT_PATH = DATA_DIR / "dict.json"
NAMES_PATH = DATA_DIR / "names.json"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import service  # noqa: E402
import team_table  # noqa: E402
from dict_lookup import DictLookup  # noqa: E402
from fetcher_stelladb import StelladbFetcher  # noqa: E402
from term_replace import TermReplacer  # noqa: E402
from maibot_sdk.context import PluginContext, PluginPaths  # noqa: E402

import plugin as plug  # noqa: E402

failures: list = []


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
    """模拟 ctx.send.text：记录 (stream_id, text) 发送历史。"""

    def __init__(self):
        self.sent = []

    async def text(self, text, stream_id, **kwargs):
        self.sent.append((stream_id, text))
        return True


class _ListLogHandler(logging.Handler):
    """捕获指定 logger 的日志记录，供失败路径断言（caplog 等价物）。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def make_plugin(ttl: int = 0, dedup: int = 0):
    """创建插件实例 + mock ctx（缓存/去重参数默认关闭以便隔离用例）。"""
    p = plug.create_plugin()
    cache = Path(tempfile.mkdtemp(prefix="stellasora_test_all_"))
    ctx = PluginContext(
        plugin_id="ggsfly.stellasora-plugin",
        rpc_call=None,
        paths=PluginPaths(data_dir=cache, runtime_dir=cache),
    )
    p._set_context(ctx)
    p._plugin_config_instance = plug.StellaSoraConfig()
    p._plugin_config_instance.query.answer_cache_ttl = ttl
    p._plugin_config_instance.query.dedup_window = dedup
    ctx.config = MockConfig(
        {
            "bot": {"nickname": "麦麦", "alias_names": []},
            "personality": {"personality": "是一个大二女大学生，现在正在上网和群友聊天。", "reply_style": "你的风格平淡简短，可以参考贴吧的回复风格。"},
            "experimental": {"emotion_trait": "neutral"},
        }
    )
    ctx.llm = MockLLM()
    ctx.send = MockSend()
    return p, ctx


# ===== 节 B：字典单例与并发（必须最先执行） =====

def run_singleton() -> None:
    print("--- B 字典单例与并发 ---")
    _orig_json_load = json.load
    counter = {"total": 0, "dict": 0}

    def counting_json_load(fp, *args, **kwargs):
        counter["total"] += 1
        if "dict.json" in getattr(fp, "name", ""):
            counter["dict"] += 1
        return _orig_json_load(fp, *args, **kwargs)

    json.load = counting_json_load
    try:
        tmp_a = Path(tempfile.mkdtemp(prefix="stellasora_singleton_a_"))
        tmp_b = Path(tempfile.mkdtemp(prefix="stellasora_singleton_b_"))
        svc_a = service._get_services(tmp_a)
        svc_b = service._get_services(tmp_b)
        lookup_a, last_a, st_a, gd_a, replacer = svc_a
        lookup_b, last_b, st_b, gd_b, _ = svc_b

        check("B1 dict.json 全进程只解析一次", counter["dict"] == 1,
              f"解析 {counter['dict']} 次（总 json.load {counter['total']}：dict 1 + names 1）")
        check("B2 两个 cache_dir 共享同一 lookup", lookup_a is lookup_b)
        check("B3 cache_dir 变化仅重建 fetcher", st_a is not st_b and gd_a is not gd_b)
        check("B4 replacer 为单一事实源注入", service._term_replace_module._replacer is replacer)
        check("B5 未收录术语返回 not_found", service.lookup_term("NoSuchTermXYZ").get("not_found") is True)

        # 并发冒烟：10 线程同时取服务，计数不变且拿到同一实例
        barrier = threading.Barrier(10)
        results: list = []

        def thread_get():
            barrier.wait()
            results.append(service._get_services(tmp_a)[0])

        before = counter["total"]
        threads = [threading.Thread(target=thread_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("B6 并发 10 线程计数不变", counter["total"] == before, f"{before} -> {counter['total']}")
        check("B7 并发返回同一 lookup", all(r is lookup_a for r in results))
        check("B8 角色名计数走共享 lookup", service.count_character_names("夏花 小禾") >= 2)
        names_cached = lookup_a.get_character_names()
        check("B9 get_character_names 缓存命中", names_cached is lookup_a.get_character_names())
    finally:
        json.load = _orig_json_load


# ===== 节 A：字典数据完整性 =====

# 抽样一致性：(中文名, 英文名, 期望 cat 前缀)
DICT_SAMPLES = [
    ("琥珀", "Amber", "Character"),
    ("小禾", "Nazuna", "Character"),
    ("风影", "Wraith", "Character"),
    ("感电", "Shock", "Word"),
    ("冻结", "Freeze", "Word"),
]


def run_dict_data() -> None:
    print("--- A 字典数据完整性 ---")
    if not DICT_PATH.is_file() or not NAMES_PATH.is_file():
        hard_fail(f"missing {DICT_PATH} or {NAMES_PATH}")
    with DICT_PATH.open("r", encoding="utf-8") as fp:
        main_dict = json.load(fp)
    with NAMES_PATH.open("r", encoding="utf-8") as fp:
        name_index = json.load(fp)
    print(f"[info] dict entries: {len(main_dict):,} | names entries: {len(name_index):,}")

    bad_fields = [
        key for key, entry in main_dict.items()
        if not isinstance(entry, dict)
        or any(not isinstance(entry.get(f), str) or not entry.get(f) for f in ("en", "cn", "cat"))
    ]
    check("A1 字段完整（en/cn/cat 均非空字符串）", not bad_fields, f"异常条目数 {len(bad_fields)}")

    dangling = [name for name, key in name_index.items() if key not in main_dict]
    check("A2 names 索引无悬空引用", len(dangling) <= 5, f"悬空 {len(dangling)} 条（容差 5）")

    missed = [cn for cn, en, cat in DICT_SAMPLES
              if cn not in name_index or not name_index[cn].startswith(f"{cat}.")]
    check("A3 抽样中英对照可查且类别正确", not missed, f"未命中 {missed}")

    dict_size, names_size = DICT_PATH.stat().st_size, NAMES_PATH.stat().st_size
    check("A4 体积上限（dict≤10MB, names≤5MB）",
          dict_size <= 10 * 1024 * 1024 and names_size <= 5 * 1024 * 1024,
          f"dict {dict_size:,}B / names {names_size:,}B")


# ===== 节 C：术语替换等价性（代表样例精简版） =====

# 覆盖关键歧义类：长名抢短名、词边界、占位符、预设码、混合中文、空串、纯中文
REPLACE_SAMPLES = [
    "",
    "这是一句纯中文，不含任何英文术语。",
    "Skill DMG % and Skill DMG on one line",           # 长名优先
    "Skill Crit Rate vs Crit Rate vs Crit DMG",
    "PEN and PENDING and PENSIVE in sentence",          # 词边界
    "Lv Lv. level levels Level10",
    "&Param1& and &Param2& untouched",                  # 占位符
    "preset AAAABBBBCCCCDDDDEEEE keep as-is",           # 预设码
    "夏花的 Support Skill Lv. 3 推荐 Charge Eff. (Main) 词条，Skill DMG % 20%",
    "土印记队 Main Discs 推荐：PEN、Crit Rate、Crit DMG、Energy Limit",
]


def run_term_replace() -> None:
    print("--- C 术语替换等价性 ---")
    replacer = TermReplacer(DATA_DIR)
    print(f"[info] mapping 词条数: {len(replacer.mapping):,}")
    mismatched = []
    for sample in REPLACE_SAMPLES:
        if replacer.replace(sample) != replacer.replace_legacy(sample):
            mismatched.append(sample[:40])
    check("C1 单遍交替正则与 legacy 等价（代表样例）", not mismatched, f"分歧 {mismatched}")
    replaced = replacer.replace("Skill DMG and Crit Rate")
    check("C2 英转中实际生效", "技能伤害" in replaced and "Crit Rate" not in replaced, replaced)


# ===== 节 D：中文别名覆盖 =====

def run_overrides() -> None:
    print("--- D 中文别名覆盖 ---")
    cfg = plug.StellaSoraConfig()
    check("D1 默认 aliases 非空且为 AliasEntry",
          len(cfg.overrides.aliases) > 0
          and all(isinstance(a, plug.AliasEntry) for a in cfg.overrides.aliases))
    check("D2 默认春科→科洛妮丝（新春）",
          cfg.overrides.aliases[0].alias == "春科"
          and cfg.overrides.aliases[0].official == "科洛妮丝（新春）")
    check("D3 overrides 不再包含 replacements", not hasattr(cfg.overrides, "replacements"))

    lookup = DictLookup(DATA_DIR, custom_aliases={"土": "地", "花玲": "花铃", "春科": "科洛妮丝（新春）"})
    res_tu = lookup.lookup_term("土")
    check("D4 俗称 土→地(Terra)", bool(res_tu and res_tu.get("en") == "Terra"), str(res_tu))
    res_hl = lookup.lookup_term("花玲")
    check("D5 笔误 花玲→花铃(Character)", bool(res_hl and res_hl.get("cat") == "Character"), str(res_hl))
    lookup.set_custom_aliases({"泥土": "土", "土": "地"})
    check("D6 链式别名 泥土→土→地", bool(lookup.lookup_term("泥土") and lookup.lookup_term("泥土").get("cn") == "地"))

    service.configure_overrides(aliases={"土": "地", "花玲": "花铃"})
    check("D7 service 动态别名生效", bool(service.lookup_term("土") and service.lookup_term("土").get("cn") == "地"))

    import inspect
    sig = inspect.signature(TermReplacer.__init__)
    check("D8 TermReplacer 无 custom_replacements",
          "custom_replacements" not in sig.parameters
          and not hasattr(TermReplacer, "set_custom_replacements"))


# ===== 节 E：索引页抓取 =====

class _FakeHTTPResponse:
    """模拟 urlopen 返回的文件对象。"""

    def __init__(self, data: bytes, code: int = 200):
        self._data, self._code = data, code

    def getcode(self) -> int:
        return self._code

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class _FakeOpener:
    """模拟 fetcher._opener（_build_opener 产物），在 fetcher 级网络边界拦截请求。

    fetcher 使用实例级 opener（支持按实例配置代理），测试同样在实例上注入
    _FakeOpener，而非全局 patch urllib.request.urlopen（那会波及进程内其它代码）。
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls: list = []

    def open(self, req, timeout=10):
        self.calls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return self._handler(req, timeout=timeout)


def run_fetcher_index() -> None:
    print("--- E 索引页抓取 ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        fetcher = StelladbFetcher(Path(tmpdir))
        html = "<html><body><div>Team Rotation: Aqua Team</div></body></html>".encode("utf-8")

        fake_opener = _FakeOpener(lambda req, timeout=10: _FakeHTTPResponse(html))
        fetcher._opener = fake_opener

        res1 = fetcher.fetch_infodoc_index()
        check("E1 首抓返回非空且 URL 正确",
              bool(res1) and "Team Rotation: Aqua Team" in res1
              and fake_opener.calls == ["https://stelladb.pages.dev/infodoc"], str(fake_opener.calls))
        res2 = fetcher.fetch_infodoc_index()
        check("E2 二次命中缓存（仅 1 次请求）", res2 == res1 and len(fake_opener.calls) == 1)

        def error_open(req, timeout=10):
            raise urllib.error.URLError("Network unreachable")

        with tempfile.TemporaryDirectory() as err_tmp:
            err_fetcher = StelladbFetcher(Path(err_tmp))
            err_fetcher._opener = _FakeOpener(error_open)
            check("E3 网络异常降级空串不崩溃", err_fetcher.fetch_infodoc_index() == "")


# ===== 节 F：how 表驱动查询（query_how_rows 按区块抓取 + 预设码 + Rotation 字段） =====

# F 节 fixture 预设码（任意 20+ 位 base64 形态串，仅作输出保真断言用）
F_FIXTURE_CODE = "AAAAnAAAAJUAAACfwYADbAZgADsMAJGAAGww"

# F 节 fixture 统一表行：结构 = team_table.json 行契约
# （slots[3] 定位主/支援、team_name_infodoc 优先、guide_ref 指向区块、rotation 构建期固化）
F_FIXTURE_ROWS = [
    {   # 行A：小禾主控 + 格芮/科洛妮丝支援 → Terra Mark (S. Coronis Ver.) 区块
        "main_key": F_FIXTURE_CODE,
        "preset_code": F_FIXTURE_CODE,
        "element": "terra",
        "slots": [
            {"char_id": 156, "en": "Nazuna", "cn": "小禾"},
            {"char_id": 149, "en": "Gerie", "cn": "格芮"},
            {"char_id": 159, "en": "Springseek Coronis", "cn": "科洛妮丝（新春）"},
        ],
        "team_name_preset": "Original Terra Mark",
        "team_name_infodoc": "Terra Mark (S. Coronis Ver.)",
        "guide_ref": {"element": "terra", "block": "Terra Mark (S. Coronis Ver.)"},
        "rotation": "ZZROTMARK 先普攻接大招的循环手法",
    },
    {   # 行B：锚名≠主控（Otoha (Laser) 队主控是珂赛特）——角色定位取行内槽位
        "main_key": "terra::Otoha (Laser)::Flora",
        "preset_code": None,
        "element": "terra",
        "slots": [
            {"char_id": 142, "en": "Cosette", "cn": "珂赛特"},
            {"char_id": 145, "en": "Otoha", "cn": "乙叶"},
            {"char_id": 126, "en": "Flora", "cn": "紫槿"},
        ],
        "team_name_preset": "",
        "team_name_infodoc": "Otoha (Laser)",
        "guide_ref": {"element": "terra", "block": "Otoha (Laser)"},
        "rotation": "",
    },
    {   # 行C：guide_ref=None（未关联码行）——只出队名+成员+预设码，无区块四类字段
        "main_key": "terra::Gerie (Auto Attack) WIP::Ridge",
        "preset_code": "AAAAfwAAAIIAAAB9VbYjEADNkCBsAKyAOACA",
        "element": "terra",
        "slots": [
            {"char_id": 149, "en": "Gerie", "cn": "格芮"},
            {"char_id": 126, "en": "Flora", "cn": "紫槿"},
            {"char_id": 110, "en": "Tilia", "cn": "缇莉亚"},
        ],
        "team_name_preset": "Gerie Auto Attack Preset",
        "team_name_infodoc": "",
        "guide_ref": None,
        "rotation": "",
    },
    {   # 行D：猫眼主控 + 科洛妮丝（新春）支援 → Chaton (Dark Ray) 区块（别名用例）
        "main_key": "terra::Chaton (Dark Ray)::Flora",
        "preset_code": None,
        "element": "terra",
        "slots": [
            {"char_id": 114, "en": "Chaton", "cn": "猫眼"},
            {"char_id": 159, "en": "Springseek Coronis", "cn": "科洛妮丝（新春）"},
            {"char_id": 126, "en": "Flora", "cn": "紫槿"},
        ],
        "team_name_preset": "",
        "team_name_infodoc": "Chaton (Dark Ray)",
        "guide_ref": {"element": "terra", "block": "Chaton (Dark Ray)"},
        "rotation": "",
    },
]

# F 节 fixture infodoc：四区块真实结构（锚点行/段头/角色行/秘纹/纹章转置）+
# 末尾干扰区块（ZZOTHERTEAM 断言按区块抓取不泄漏其他队伍内容）
F_FIXTURE_INFODOC = """Terra Mark (S. Coronis Ver.) | ⏏ Back to Top ⏏
Description | Skill Upgrade Priority
Nazuna (5★) | 10/10/1/10 (Main Skill only)
ZZDESCMAIN 小禾主控描述内容
Priority Potentials | Recommended Main Discs
ZZDISCA (C1) | ZZDISCB (C6)
Optional Potentials | Emblem
Affix Priority | Terra PEN | 110 | Crit Rate | 15%
Main Skill Lv. | +3 levels | Terra DMG | 20%
Description | Skill Upgrade Priority
Gerie (4★) | 1/1/10/1 (Support Skill only)
ZZDESCSUPP 格芮支援描述内容
Otoha (Laser) | ⏏ Back to Top ⏏
Description | Skill Upgrade Priority
Cosette (4★) | 1/10/1/1 (Main Skill only)
ZZMAINNOTE 珂赛特主控描述内容
Description | Skill Upgrade Priority
Otoha (5★ Excl.) | 1/1/10/1 (Support Skill only)
ZZSUPPNOTE 乙叶支援描述内容
Chaton (Dark Ray) | ⏏ Back to Top ⏏
Description | Skill Upgrade Priority
Chaton (5★) | 1+/10/1/1+ (Main Skill >> Auto Attack = Ultimate)
ZZCHATDESC 猫眼主控描述内容
Description | Skill Upgrade Priority
Springseek Coronis (5★) | 1/1/10/1 (Support Skill only)
ZZCORONISDESC 科洛妮丝支援描述内容
Other Team | ⏏ Back to Top ⏏
ZZOTHERTEAM 其他队伍内容不应出现
"""

# F10 fixture umbra infodoc：两个同角色不同玩法区块（翡冷翠 Main Skill / Minion），
# 模板抄 F_FIXTURE_INFODOC 真实结构（锚点行/段头/角色行/秘纹/纹章转置）改名字与标记；
# Mistique/Coronis 段无描述标记——模拟"区块内缺段成员由队友并集 rescue"的关联形态
F10_UMBRA_INFODOC = """Firenze (Main Skill) | ⏏ Back to Top ⏏
Description | Skill Upgrade Priority
Firenze (5★) | 10/10/1/10 (Main Skill only)
ZZMS-F-DESC 翡冷翠主控描述内容
Description | Skill Upgrade Priority
Cosette (4★) | 1/1/10/1 (Support Skill only)
ZZMS-COS-DESC 珂赛特支援描述内容
Description | Skill Upgrade Priority
Otoha (5★ Excl.) | 1/1/10/1 (Support Skill only)
ZZMS-OTO-DESC 乙叶支援描述内容
Description | Skill Upgrade Priority
Mistique (4★) | 1/1/10/1 (Support Skill only)
Description | Skill Upgrade Priority
Coronis (5★) | 1/1/10/1 (Support Skill only)
Firenze (Minion) | ⏏ Back to Top ⏏
Description | Skill Upgrade Priority
Firenze (5★) | 10/10/1/10 (Main Skill only)
ZZMI-F-DESC 翡冷翠仆从主控描述内容
Description | Skill Upgrade Priority
Cosette (4★) | 1/1/10/1 (Support Skill only)
ZZMI-COS-DESC 珂赛特支援描述内容
Description | Skill Upgrade Priority
Otoha (5★ Excl.) | 1/1/10/1 (Support Skill only)
ZZMI-OTO-DESC 乙叶支援描述内容
Description | Skill Upgrade Priority
Mistique (4★) | 1/1/10/1 (Support Skill only)
Description | Skill Upgrade Priority
Coronis (5★) | 1/1/10/1 (Support Skill only)
"""

# F10 fixture 行：同 guide_ref 区块多行并组（Main Skill×4 行各不同码 / Minion×3 行无码）
_F10_MS_REF = {"element": "umbra", "block": "Firenze (Main Skill)"}
_F10_MI_REF = {"element": "umbra", "block": "Firenze (Minion)"}


def _f10_row(
    slots: list,
    ref: dict,
    team_name: str,
    code: str | None,
    main_key: str,
) -> dict:
    """构造 F10 fixture 表行（行结构 = team_table.json 行契约）。"""
    return {
        "main_key": main_key,
        "preset_code": code,
        "element": "umbra",
        "slots": slots,
        "team_name_preset": "Otoha (Weeping Sky)",
        "team_name_infodoc": team_name,
        "guide_ref": ref,
        "rotation": "",
    }


_S_MS = [
    [{"char_id": 110, "en": "Firenze", "cn": "翡冷翠"},
     {"char_id": 142, "en": "Cosette", "cn": "珂赛特"},
     {"char_id": 145, "en": "Otoha", "cn": "乙叶"}],
    [{"char_id": 110, "en": "Firenze", "cn": "翡冷翠"},
     {"char_id": 142, "en": "Cosette", "cn": "珂赛特"},
     {"char_id": 135, "en": "Mistique", "cn": "雾语"}],
    [{"char_id": 110, "en": "Firenze", "cn": "翡冷翠"},
     {"char_id": 142, "en": "Cosette", "cn": "珂赛特"},
     {"char_id": 118, "en": "Coronis", "cn": "科洛妮丝"}],
]
F10_FIXTURE_ROWS = [
    _f10_row(_S_MS[0], _F10_MS_REF, "Firenze (Main Skill)", "AAAAMS0xAAAAJwAAACfzbAbAADAQBgNhsWIAGAw", "umbra::MS::1"),
    _f10_row(_S_MS[1], _F10_MS_REF, "Firenze (Main Skill)", "AAAAMS0yAAAAJwAAACfzbAbAADAQBgNhsWIAGAw", "umbra::MS::2"),
    _f10_row(_S_MS[2], _F10_MS_REF, "Firenze (Main Skill)", "AAAAMS0zAAAAJwAAACfzbAbAADAQBgNhsWIAGAw", "umbra::MS::3"),
    _f10_row(_S_MS[0], _F10_MS_REF, "Firenze (Main Skill)", "AAAAMS00AAAAJwAAACfzbAbAADAQBgNhsWIAGAw", "umbra::MS::4"),
    _f10_row(_S_MS[0], _F10_MI_REF, "Firenze (Minion)", None, "umbra::MI::1"),
    _f10_row(_S_MS[1], _F10_MI_REF, "Firenze (Minion)", None, "umbra::MI::2"),
    _f10_row(_S_MS[2], _F10_MI_REF, "Firenze (Minion)", None, "umbra::MI::3"),
]


def run_query_how_section() -> None:
    print("--- F how 表驱动查询 ---")
    import unittest.mock

    row_a, row_b, row_c, row_d = F_FIXTURE_ROWS
    with tempfile.TemporaryDirectory(prefix="stellasora_f_") as tmp:
        # infodocs 目录 fixture：monkeypatch 模块常量 _INFODOCS_DIR（不触真实离线数据）
        infodocs_dir = Path(tmp)
        (infodocs_dir / "terra.json").write_text(
            json.dumps({"data": F_FIXTURE_INFODOC}, ensure_ascii=False), encoding="utf-8"
        )
        with unittest.mock.patch.object(service, "_INFODOCS_DIR", infodocs_dir):
            # F1 组头+成员行：编号队名头+槽位定位标签（主控/支援），只抓命中区块。
            # 问句含触发词"完整"→两问询角色全详述（详略策略回归；科洛妮丝（新春）
            # 无区块段，由队友并集 rescue）
            mat_a = service.query_how_rows([row_a], with_presets=False, question="小禾 格芮完整攻略")
            check("F1 组头+槽位定位标签，不含未命中区块内容",
                  "1. 地系印记 (S. 科洛妮丝 Ver.)" in mat_a
                  and "小禾（主控位）" in mat_a
                  and "格芮（支援位）" in mat_a
                  and "ZZOTHERTEAM" not in mat_a)

            # F2 四类字段齐全（描述/技能/秘纹/纹章）+ 纹章转置分档（70级/80级）
            check("F2 四类字段齐全且纹章转置分档",
                  "描述：" in mat_a
                  and "技能升级优先度：10/10/1/10" in mat_a
                  and "技能升级优先度：1/1/10/1" in mat_a
                  and "推荐主位秘纹：" in mat_a and "ZZDISCA" in mat_a
                  and "纹章推荐：" in mat_a
                  and "70级：" in mat_a and "80级：" in mat_a
                  and "110" in mat_a and "15%" in mat_a)

            # F3 支援位问询（锚名≠主控）：问句角色=乙叶（槽位支援位）→ 问询角色排首位。
            # 详略策略（1 角色→仅乙叶详述）：珂赛特/紫槿进队友并集行，主控字段不再渲染
            mat_b = service.query_how_rows([row_b], with_presets=False, question="乙叶攻略")
            check("F3 支援位问询定位正确（锚名≠主控）",
                  "乙叶（支援位）" in mat_b
                  and "ZZSUPPNOTE" in mat_b
                  and "队友：珂赛特（主控位）、紫槿（支援位）" in mat_b
                  and "ZZMAINNOTE" not in mat_b)

            # F4 rotation 直读行内字段；空 rotation 行不产生输出手法块
            check("F4 rotation 行字段直读（行A含/行B无输出手法块）",
                  "ZZROTMARK" in mat_a and "循环手法" in mat_a
                  and "输出手法" not in mat_b)

            # F5 预设码行：码原文保真+主控/援护标注，位于组头之后；关闭时不出现
            mat_p = service.query_how_rows([row_a], with_presets=True, question="小禾 格芮完整攻略")
            idx_t, idx_c = mat_p.find("1. 地系印记 (S. 科洛妮丝 Ver.)"), mat_p.find("预设码：")
            check("F5 预设码行格式与保序",
                  idx_t > -1 and idx_c > idx_t
                  and f"预设码：{F_FIXTURE_CODE}（主控小禾、援护格芮、科洛妮丝（新春））" in mat_p
                  and "预设码" not in mat_a)

            # F6 guide_ref=None 行优雅降级：组头+成员+预设码，无区块四类字段
            mat_c = service.query_how_rows([row_c], with_presets=True, question="格芮攻略")
            check("F6 未关联行优雅降级（组头+成员+预设码，无四类字段）",
                  "1. 格芮 普攻 预设" in mat_c
                  and "格芮（主控位）" in mat_c
                  and "紫槿（支援位）" in mat_c
                  and "预设码：" in mat_c
                  and "描述：" not in mat_c and "纹章推荐：" not in mat_c)

            # F7+F8 别名问句（原 F15-1/F15-4 迁移）：提取官方名 + 双"本角色"标签
            service.configure_overrides(aliases={"春科": "科洛妮丝（新春）"})
            try:
                extracted = service.find_character_names("春科 猫眼 谁好")
                check("F7 俗称别名提取为官方角色名",
                      "科洛妮丝（新春）" in extracted and "猫眼" in extracted,
                      f"提取结果: {extracted}")
                mat_d = service.query_how_rows([row_d], with_presets=False, question="春科 猫眼攻略")
                # 详略策略（2 角色→仅第一个即别名角色详述）：科洛妮丝（新春）详述
                # 可观测（别名识别），猫眼（第二问询角色/主控位）进队友并集行
                check("F8 别名问句首角色详述+第二问询角色进并集（详略策略翻转）",
                      "科洛妮丝（新春）（支援位）" in mat_d
                      and "ZZCORONISDESC" in mat_d
                      and "猫眼（主控位）" in mat_d
                      and "ZZCHATDESC" not in mat_d)
            finally:
                service.configure_overrides(aliases={})

            # F9 表交集查询（原 F12/F14 迁移为表语义）：fixture 表经 load_team_table 注入
            fixture_table = {"rows": F_FIXTURE_ROWS, "report": {"invalid_rows": []}}
            with unittest.mock.patch.object(service, "load_team_table", return_value=fixture_table):
                hit_single = service.find_team_rows([156])
                hit_multi = service.find_team_rows([156, 149])
                miss_single = service.find_team_rows([141])
                miss_multi = service.find_team_rows([141, 126])
            check("F9 表交集查询（单角色命中/双角色交集/未命中空列表）",
                  len(hit_single) == 1 and len(hit_multi) == 1
                  and hit_multi[0]["main_key"] == F_FIXTURE_CODE
                  and miss_single == [] and miss_multi == [])

            # F10 分组渲染（Design X）：mock umbra.json 写入同一 tmp 目录，
            # 同 guide_ref 区块跨行并组——组头编号、区块字段去重、队友并集 rescue、组尾预设码
            (infodocs_dir / "umbra.json").write_text(
                json.dumps({"data": F10_UMBRA_INFODOC}, ensure_ascii=False), encoding="utf-8"
            )
            mat_f = service.query_how_rows(F10_FIXTURE_ROWS, with_presets=True)
            group_heads_f = re.findall(r"^\d+\. ", mat_f, flags=re.M)
            ms_cnt = {m: mat_f.count(m) for m in ("ZZMS-F-DESC", "ZZMS-COS-DESC", "ZZMS-OTO-DESC")}
            mi_cnt = {m: mat_f.count(m) for m in ("ZZMI-F-DESC", "ZZMI-COS-DESC", "ZZMI-OTO-DESC")}
            check("F10 同区块分组：组头恰2且区块名+字段去重各恰1次+缺段标记不泄漏",
                  len(group_heads_f) == 2
                  and "1. 翡冷翠 (主技能)" in mat_f
                  and "2. 翡冷翠 (仆从)" in mat_f
                  and all(v == 1 for v in ms_cnt.values()) and all(v == 1 for v in mi_cnt.values())
                  and mat_f.count("ZZMS-MIS-DESC") == 0 and mat_f.count("ZZMS-COR-DESC") == 0,
                  f"group_heads={group_heads_f}, ms={ms_cnt}, mi={mi_cnt}")
            check("F10 队友并集：两组各一并集行+四支援成员齐全",
                  mat_f.count("队友：雾语（支援位）、科洛妮丝（支援位）") == 2
                  and "珂赛特" in mat_f and "乙叶" in mat_f
                  and "雾语（支援位）" in mat_f and "科洛妮丝（支援位）" in mat_f)
            check("F10 组尾预设码：组1四码各一行+旧'配队'头不出现",
                  len(re.findall(r"^预设码：", mat_f, flags=re.M)) == 4
                  and "预设码：AAAAMS0xAAAAJwAAACfzbAbAADAQBgNhsWIAGAw" in mat_f
                  and "预设码：AAAAMS00AAAAJwAAACfzbAbAADAQBgNhsWIAGAw" in mat_f
                  and "配队" not in mat_f)

        # F11 真实表翡冷翠（不在 patch context 内——读真实离线数据）：
        # 4 行按 preset 码独立成组，组头为预设队名
        service.reload_team_table()
        rows_firenze = service.find_team_rows([110])
        mat_real_f = service.query_how_rows(rows_firenze, question="翡冷翠攻略")
        check("F11 真实表翡冷翠分组渲染：组头恰4且旧'配队'头消失+并集行恰4",
              len(re.findall(r"^\d+\. ", mat_real_f, flags=re.M)) == 4
              and "翡冷翠（主控位）" in mat_real_f
              and "配队" not in mat_real_f
              and len(re.findall(r"^队友：", mat_real_f, flags=re.M)) == 4,
              f"rows={len(rows_firenze)}")

        # F12 真实表小禾+格芮：2 行按行序并成 2 组，队友并集正确
        rows_sg = service.find_team_rows([156, 149])
        mat_sg = service.query_how_rows(rows_sg, question="小禾 格芮攻略")
        check("F12 真实表小禾+格芮：2 组头按行序且队友并集正确",
              len(re.findall(r"^\d+\. ", mat_sg, flags=re.M)) == 2
              and "Original 地系印记" in mat_sg
              and "小禾（主控位）" in mat_sg
              and "格芮（支援位）" in mat_sg
              and "配队" not in mat_sg
              and len(re.findall(r"^队友：", mat_sg, flags=re.M)) == 2,
              f"rows={len(rows_sg)}")

        # F13 None 行独立成组+防御：不同 preset_code 各自成组（首行首槽无 char_id
        # 测并集 ident 回退），空行列表返回空串
        rows_none = [
            {
                "main_key": "terra::Alpha::1",
                "preset_code": "Preset Alpha",
                "element": "terra",
                "slots": [
                    {"en": "Gerie", "cn": "格芮"},
                    {"char_id": 156, "en": "Nazuna", "cn": "小禾"},
                ],
                "team_name_preset": "Preset Alpha",
                "team_name_infodoc": "",
                "guide_ref": None,
                "rotation": "",
            },
            {
                "main_key": "terra::Beta::1",
                "preset_code": "Preset Beta",
                "element": "terra",
                "slots": [
                    {"char_id": 149, "en": "Gerie", "cn": "格芮"},
                    {"char_id": 156, "en": "Nazuna", "cn": "小禾"},
                ],
                "team_name_preset": "Preset Beta",
                "team_name_infodoc": "",
                "guide_ref": None,
                "rotation": "",
            },
        ]
        mat_none = service.query_how_rows(rows_none)
        check("F13 None行独立成组：组头恰2（预设Alpha/Beta）+缺char_id不崩",
              len(re.findall(r"^\d+\. ", mat_none, flags=re.M)) == 2
              and "1. 预设 Alpha" in mat_none and "2. 预设 Beta" in mat_none
              and "格芮（主控位）" in mat_none
              and service.query_how_rows([]) == "")


# ===== 节 G：直发端到端（核心用例精简版） =====

async def run_direct_send() -> None:
    print("--- G 直发端到端 ---")

    # ===== G 节 fixture 表衔接：monkeypatch service.load_team_table 返回 fixture 表
    # dict（rows+report），handle_how 真实路径经 load_team_table 即得 fixture——不写磁盘。
    # 未关联行（guide_ref=None）只出队名+成员+预设码骨架；G29 小禾+格芮行挂真实
    # Terra Mark (S. Coronis Ver.) guide_ref，验证按区块抓取真实离线数据。
    import unittest.mock

    def _fixture_row(slots: list, *, code: str | None = None, guide_ref: dict | None = None, rotation: str = "") -> dict:
        """构造单行 fixture 表行（行结构 = team_table.json 行契约）。"""
        return {
            "main_key": code or f"fixture::{slots[0]['en']}",
            "preset_code": code,
            "element": "terra",
            "slots": slots,
            "team_name_preset": "Original Terra Mark",
            "team_name_infodoc": "Terra Mark (S. Coronis Ver.)",
            "guide_ref": guide_ref,
            "rotation": rotation,
        }

    _CODE_A = "AAAAjAAAAJwAAACfzbAbAADAQBgNhsWIAGAw"
    g_fixture_table = {
        "rows": [
            # 夏花单角色行（G1/G4/G5/G8/G10-G18/G26/G27）
            _fixture_row(
                [{"char_id": 133, "en": "Nazuka", "cn": "夏花"},
                 {"char_id": 149, "en": "Gerie", "cn": "格芮"},
                 {"char_id": 159, "en": "Springseek Coronis", "cn": "科洛妮丝（新春）"}],
                code=_CODE_A, rotation="夏花循环手法测试",
            ),
            # 猫眼单角色行（G11）
            _fixture_row(
                [{"char_id": 114, "en": "Chaton", "cn": "猫眼"},
                 {"char_id": 149, "en": "Gerie", "cn": "格芮"},
                 {"char_id": 159, "en": "Springseek Coronis", "cn": "科洛妮丝（新春）"}],
            ),
            # 夏花+小禾同行（G9 联合查询交集命中）
            _fixture_row(
                [{"char_id": 156, "en": "Nazuna", "cn": "小禾"},
                 {"char_id": 133, "en": "Nazuka", "cn": "夏花"},
                 {"char_id": 159, "en": "Springseek Coronis", "cn": "科洛妮丝（新春）"}],
            ),
            # 小禾+格芮 Terra Mark 行（G9c 双本角色标签，挂真实离线区块）
            _fixture_row(
                [{"char_id": 156, "en": "Nazuna", "cn": "小禾"},
                 {"char_id": 149, "en": "Gerie", "cn": "格芮"},
                 {"char_id": 159, "en": "Springseek Coronis", "cn": "科洛妮丝（新春）"}],
                code="AAAAjAAAAJwAAAB0zbAbAADBgBgMBsGAAGwA",
                guide_ref={"element": "terra", "block": "Terra Mark (S. Coronis Ver.)"},
                rotation="小禾格芮循环手法测试",
            ),
        ],
        "report": {"invalid_rows": []},
    }
    g_team_table_patch = unittest.mock.patch.object(
        service, "load_team_table", return_value=g_fixture_table
    )

    # G1 直发成功：stream 正确 + 人格注入 + model=utils 透传（原用例 1+20 合并）
    p, ctx = make_plugin()
    await p.on_load()
    g_team_table_patch.start()
    llm, send = ctx.llm, ctx.send
    r = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g1")
    check("G1 直发成功且引导调 wait 禁 reply",
          "已直接发送" in r["content"] and "wait 工具" in r["content"] and len(send.sent) == 1
          and send.sent[0][0] == "stream_g1")
    check("G2 prompt 注入人格与表达风格",
          "你的名字是麦麦" in llm.calls[0]["prompt"] and "表达风格" in llm.calls[0]["prompt"])
    check("G3 SDK 透传 model=utils", llm.calls[0].get("model") == "utils")

    # G4-G6 失败分支：LLM 软失败→降级回传原始资料 / 硬异常→降级回传 / stream 缺失→未找到
    # （修复点2：资料查询成功但 LLM 加工失败时不再谎报"未找到"，而是带系统说明回传原始资料）
    ctx.llm = MockLLM(fail=True)
    n_sent = len(send.sent)
    r = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g4")
    relay_content = str(r.get("content", ""))
    check("G4 LLM 软失败→降级回传原始资料（前缀+原文+非未找到+不直发）",
          relay_content.startswith("[系统说明")
          and plug._FAILURE_RELAY_PREFIX in relay_content
          and "夏花" in relay_content
          and "未找到相关攻略" not in relay_content
          and len(send.sent) == n_sent)
    ctx.llm = MockLLM(hard_fail=True)
    r = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g5")
    relay_content = str(r.get("content", ""))
    check("G5 LLM 硬异常→降级回传原始资料（前缀+原文+非未找到）",
          relay_content.startswith("[系统说明")
          and "不要调用其他搜索工具" in relay_content
          and "未找到相关攻略" not in relay_content)
    ctx.llm = MockLLM()
    r = await p.handle_what(query="猫眼", group_id="g1", stream_id="")
    check("G6 stream 缺失→未找到", r["content"] == "未找到相关攻略。")

    # G7 鉴权：白名单外的群被拒
    p._plugin_config_instance.access_control.mode = "whitelist"
    r = await p.handle_how(query="夏花", group_id="g999", stream_id="stream_g7")
    check("G7 白名单拒绝", "允许范围" in r["content"])
    p._plugin_config_instance.access_control.mode = "off"

    # G8 人格开关：关闭后 prompt 无人格，开启后恢复
    ctx.llm = llm
    p._plugin_config_instance.query.inject_persona = False
    await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g8a")
    no_persona = "你的名字是麦麦" not in llm.calls[-1]["prompt"]
    p._plugin_config_instance.query.inject_persona = True
    await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g8b")
    check("G8 人格开关生效", no_persona and "你的名字是麦麦" in llm.calls[-1]["prompt"])

    # G9 修复点3：联合查询（≥2 角色名）不再强制回传——direct_send=true 时直发聊天
    n_sent = len(send.sent)
    ret = await p.handle_how(query="夏花", question="夏花 小禾 谁的纹章好", group_id="g1", stream_id="stream_g9")
    check("G9 联合查询（≥2 角色名）改直发（send 一次+已发送确认+联合问题入 prompt）",
          len(send.sent) == n_sent + 1
          and "已直接发送" in str(ret.get("content", ""))
          and "wait 工具" in str(ret.get("content", ""))
          and "你的名字是麦麦" in llm.calls[-1]["prompt"]
          and "小禾" in llm.calls[-1]["prompt"])

    # G9b alternative：direct_send=false → 回传行为正常（加工成品回传 planner，不直发聊天）
    p._plugin_config_instance.query.direct_send = False
    n_sent = len(send.sent)
    ret = await p.handle_how(query="夏花", question="夏花 小禾 谁的纹章好", group_id="g1", stream_id="stream_g9b")
    check("G9b direct_send=false 联合查询回传（客观体+系统说明包装+不直发）",
          "客观" in llm.calls[-1]["prompt"]
          and "你的名字是麦麦" not in llm.calls[-1]["prompt"]
          and "系统说明" in str(ret.get("content", ""))
          and "mock LLM 攻略成品" in str(ret.get("content", ""))
          and len(send.sent) == n_sent)
    p._plugin_config_instance.query.direct_send = True

    # G9c 未命中（Task 3 硬约束）：单角色表未命中 → 直接"未找到相关攻略。"，
    # 无整页资料、不走 LLM、不直发（用户裁定 4：不回退整页、不降级）
    n_sent = len(send.sent)
    n_llm = len(llm.calls)
    r_miss = await p.handle_how(query="赤霞", group_id="g1", stream_id="stream_g9c")
    check("G9c 单角色表未命中→未找到（无整页资料+不走LLM+不直发）",
          r_miss == {"name": "stellasora_how", "content": "未找到相关攻略。"}
          and "配队" not in str(r_miss.get("content", ""))
          and len(llm.calls) == n_llm and len(send.sent) == n_sent)

    # G9d 未命中（多角色交集为空）→ 同样直接"未找到相关攻略。"
    n_sent = len(send.sent)
    n_llm = len(llm.calls)
    r_miss2 = await p.handle_how(query="夏花", question="夏花 猫眼 配队", group_id="g1", stream_id="stream_g9d")
    check("G9d 多角色交集为空→未找到（无整页资料+不走LLM+不直发）",
          r_miss2 == {"name": "stellasora_how", "content": "未找到相关攻略。"}
          and "配队" not in str(r_miss2.get("content", ""))
          and len(llm.calls) == n_llm and len(send.sent) == n_sent)

    # G9e presets=true 直发：资料含"预设码："行（码原文）——透传至 LLM prompt
    n_sent = len(send.sent)
    await p.handle_how(query="夏花", presets=True, group_id="g1", stream_id="stream_g9e")
    check("G9e presets=true 资料+prompt 含预设码行",
          len(send.sent) == n_sent + 1
          and "预设码：" in llm.calls[-1]["prompt"]
          and "AAAAjAAAAJwAAACfzbAbAADAQBgNhsWIAGAw" in llm.calls[-1]["prompt"])

    # G9f-G9h 运行时兼容验证（question 契约改必传 + query 多名兜底）：
    # SDK 层 required=True 语义未经宿主运行时实测（无宿主环境），handler 层
    # 优雅降级在此验证——question 缺省（""/None）经 (question or "").strip()
    # 兜底不抛异常，走 query 归一路径命中角色。
    ctx.llm, ctx.send = MockLLM(), MockSend()
    r_qf = await p.handle_how(query="小禾 格芮", question="", group_id="g1", stream_id="stream_g9f")
    check("G9f question 空串→query 兜底命中双角色（不抛异常+直发+两名入 prompt）",
          "已直接发送" in r_qf.get("content", "")
          and "小禾" in ctx.llm.calls[0]["prompt"] and "格芮" in ctx.llm.calls[0]["prompt"])

    ctx.llm, ctx.send = MockLLM(), MockSend()
    r_qg = await p.handle_how(query="小禾 格芮", question=None, group_id="g1", stream_id="stream_g9g")
    check("G9g question=None→不抛异常且 query 兜底命中（直发+两名入 prompt）",
          "已直接发送" in r_qg.get("content", "")
          and "小禾" in ctx.llm.calls[0]["prompt"] and "格芮" in ctx.llm.calls[0]["prompt"])

    # G9h 4.3 多名兜底路径：问句无可提取角色名时 query 按空格分词逐个归一
    ctx.llm, ctx.send = MockLLM(), MockSend()
    r_qh = await p.handle_how(query="小禾 格芮", question="他俩怎么配队", group_id="g1", stream_id="stream_g9h")
    check("G9h 问句未命中→query 多名分词兜底命中双角色（直发+两名入 prompt）",
          "已直接发送" in r_qh.get("content", "")
          and "小禾" in ctx.llm.calls[0]["prompt"] and "格芮" in ctx.llm.calls[0]["prompt"])

    # G10-G12 去重守卫：同流同 query 拦截 / 不同 query 放行 / 窗口过期放行
    # dedup_window=60：G10 的拦截断言依赖默认 60s 窗口生效
    p2, ctx2 = make_plugin(dedup=60)
    await p2.on_load()
    llm2, send2 = ctx2.llm, ctx2.send
    await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup")
    r2b = await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup")
    check("G10 同流同 query 去重拦截（LLM/send 各一次）",
          "勿重复发送" in r2b.get("content", "") and len(llm2.calls) == 1 and len(send2.sent) == 1)

    ctx2.llm, ctx2.send = MockLLM(), MockSend()
    p2._recent_direct.clear()
    await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup11")
    r11 = await p2.handle_how(query="猫眼", group_id="g1", stream_id="stream_dedup11")
    check("G11 不同 query 不拦截", "已直接发送" in r11.get("content", "") and len(ctx2.send.sent) == 2)

    ctx2.llm, ctx2.send = MockLLM(), MockSend()
    p2._recent_direct.clear()
    p2._recent_direct[("stream_dedup13", "夏花")] = time.time() - 200  # 时间戳前推 200s 超过默认 60s 窗口
    r13 = await p2.handle_how(query="夏花", group_id="g1", stream_id="stream_dedup13")
    check("G12 窗口过期不拦截", "已直接发送" in r13.get("content", ""))

    # G13-G15 直发成品缓存：命中 / 配置更新清空 / presets 独立缓存键
    p15, ctx15 = make_plugin(dedup=0, ttl=86400)
    await p15.on_load()
    llm15, send15 = ctx15.llm, ctx15.send
    llm15.answer = "夏花纹章推荐成品攻略"
    await p15.handle_how(query="夏花", group_id="g1", stream_id="stream_cache15")
    r15b = await p15.handle_how(query="夏花", group_id="g1", stream_id="stream_cache15")
    check("G13 缓存命中（LLM 一次/send 两次/内容相同）",
          len(llm15.calls) == 1 and len(send15.sent) == 2
          and send15.sent[0][1] == send15.sent[1][1] == "夏花纹章推荐成品攻略"
          and "已直接发送" in r15b.get("content", ""))

    await p15.on_config_update(scope="query", config_data={}, version="1.2.3")
    await p15.handle_how(query="夏花", group_id="g1", stream_id="stream_cache15")
    check("G14 配置更新清空缓存（LLM 重新生成）", len(llm15.calls) == 2 and len(send15.sent) == 3)

    # G15 presets 独立缓存键（需要 ttl>0 才有成品缓存行为）
    p18, ctx18 = make_plugin(ttl=86400)
    await p18.on_load()
    llm18, send18 = ctx18.llm, ctx18.send
    await p18.handle_how(query="夏花", presets=False, group_id="g1", stream_id="stream_cache18")
    await p18.handle_how(query="夏花", presets=True, group_id="g1", stream_id="stream_cache18")
    await p18.handle_how(query="夏花", presets=True, group_id="g1", stream_id="stream_cache18")
    check("G15 presets 独立缓存键（1→2→命中 2）",
          len(llm18.calls) == 2 and len(send18.sent) == 3)

    # G16 游戏知识注入开关：默认注入标头 / 关闭后不含
    p19, ctx19 = make_plugin()
    await p19.on_load()
    llm19 = ctx19.llm
    await p19.handle_how(query="夏花", group_id="g1", stream_id="stream_k19a")
    has_knowledge = "【游戏机制知识（回答格式必须遵守）】" in llm19.calls[0]["prompt"]
    p19._plugin_config_instance.query.inject_knowledge = False
    await p19.handle_how(query="夏花", group_id="g1", stream_id="stream_k19b")
    check("G16 知识注入开关生效",
          has_knowledge
          and "【游戏机制知识（回答格式必须遵守）】" not in llm19.calls[-1]["prompt"])

    # G17 llm_model 空串 → generate kwargs 不含 model 键（负路径）
    p20, ctx20 = make_plugin()
    await p20.on_load()
    p20._plugin_config_instance.query.llm_model = ""
    r21 = await p20.handle_how(query="夏花", group_id="g1", stream_id="stream_g17")
    check("G17 llm_model 空串不含 model 键",
          "model" not in ctx20.llm.calls[0] and "已直接发送" in r21.get("content", ""))

    # G18 直发 prompt 注入反 markdown 约束（规则 9）
    ctx20.llm, ctx20.send = MockLLM(), MockSend()
    p20._plugin_config_instance.query.llm_model = "utils"
    p20._recent_direct.clear()
    r22 = await p20.handle_how(query="夏花", group_id="g1", stream_id="stream_anti_md")
    check("G18 反 markdown 指令注入",
          "不要使用任何 markdown 格式" in ctx20.llm.calls[0]["prompt"]
          and "已直接发送" in r22.get("content", ""))

    # G19 提示词单一事实源：docs/prompts.md 加载成功且含关键规则标记（走插件同一加载路径）
    p23, ctx23 = make_plugin()
    await p23.on_load()
    loaded_prompt = plug._load_prompt_doc()
    check("G19 提示词文档加载成功且含三标记",
          isinstance(loaded_prompt, str)
          and "不要使用任何 markdown 格式" in loaded_prompt
          and "2000 字" in loaded_prompt
          and "预设码保持原样" in loaded_prompt)

    # G20-G21 失败路径：monkeypatch 提示词文档路径为不存在并清空模块缓存，
    # 按插件加载同一路径重新赋值类属性 → _direct_send 返回未找到 + 双路 error 日志
    ctx_logger = logging.getLogger("plugin.ggsfly.stellasora-plugin")  # 与 make_plugin 的 plugin_id 对应
    module_logger = logging.getLogger("stellasora.plugin")
    ctx_handler, module_handler = _ListLogHandler(), _ListLogHandler()
    ctx_logger.addHandler(ctx_handler)
    module_logger.addHandler(module_handler)
    saved_doc_path = plug._PROMPT_DOC_PATH
    saved_doc_cache = plug._PROMPT_DOC_CACHE
    saved_prompt = plug.StellaSoraPlugin._DIRECT_SEND_PROMPT
    plug._PROMPT_DOC_PATH = Path(tempfile.mkdtemp(prefix="stellasora_g20_")) / "prompts.md"
    plug._PROMPT_DOC_CACHE = None
    plug.StellaSoraPlugin._DIRECT_SEND_PROMPT = plug._load_prompt_doc()
    try:
        ctx23.llm, ctx23.send = MockLLM(), MockSend()
        r23 = await p23._direct_send(
            tool_name="stellasora_how", question="夏花攻略", material="资料",
            direct=True, query="夏花", stream_id="stream_g20",
        )
        check("G20 提示词文档缺失→未找到且不调 LLM 不发送",
              r23 == {"name": "stellasora_how", "content": "未找到相关攻略。"}
              and ctx23.llm.calls == [] and ctx23.send.sent == [])
        check("G21 提示词文档缺失记录 error 日志（加载器+守卫双路）",
              any(r.levelno == logging.ERROR and "提示词" in r.getMessage() for r in module_handler.records)
              and any(r.levelno == logging.ERROR and "提示词" in r.getMessage() for r in ctx_handler.records))
    finally:
        plug._PROMPT_DOC_PATH = saved_doc_path
        plug._PROMPT_DOC_CACHE = saved_doc_cache
        plug.StellaSoraPlugin._DIRECT_SEND_PROMPT = saved_prompt
        ctx_logger.removeHandler(ctx_handler)
        module_logger.removeHandler(module_handler)

    # G22-G28 修复点2：LLM 加工失败降级回传（资料非空 → 系统说明+原始资料；不再谎报"未找到"）
    relay_prefix = plug._FAILURE_RELAY_PREFIX

    # G22 硬异常 + direct=True：返回工具结果（非聊天直发），内容=前缀+资料原文
    p22, ctx22 = make_plugin()
    await p22.on_load()
    ctx22.llm = MockLLM(hard_fail=True)
    r22 = await p22._direct_send(
        tool_name="stellasora_how", question="夏花攻略", material="测试原始攻略资料XYZ",
        direct=True, query="夏花", stream_id="stream_g22",
    )
    check("G22 LLM 硬异常降级回传（前缀+资料原文+非未找到+不直发聊天）",
          str(r22.get("content", "")).startswith("[系统说明")
          and relay_prefix in str(r22.get("content", ""))
          and "测试原始攻略资料XYZ" in str(r22.get("content", ""))
          and "未找到相关攻略" not in str(r22.get("content", ""))
          and ctx22.send.sent == [])

    # G23 direct=False（回传路径）统一降级语义：前缀+资料原文
    p23b, ctx23b = make_plugin()
    await p23b.on_load()
    ctx23b.llm = MockLLM(hard_fail=True)
    r23b = await p23b._direct_send(
        tool_name="stellasora_what", question="猫眼资料", material="回传模式原始资料ABC",
        direct=False, query="猫眼",
    )
    check("G23 direct=False 路径统一降级回传",
          r23b.get("content") == relay_prefix + "回传模式原始资料ABC"
          and ctx23b.send.sent == [])

    # G24 降级动作记录 warning 日志（失败原因 + 降级动作）
    g24_logger = logging.getLogger("plugin.ggsfly.stellasora-plugin")
    g24_handler = _ListLogHandler()
    g24_logger.addHandler(g24_handler)
    try:
        p24, ctx24 = make_plugin()
        await p24.on_load()
        ctx24.llm = MockLLM(fail=True)
        r24 = await p24._direct_send(
            tool_name="stellasora_how", question="夏花攻略", material="日志断言资料",
            direct=True, query="夏花", stream_id="stream_g24",
        )
        check("G24 降级回传记录 warning 日志（失败原因+降级动作）",
              "[系统说明" in str(r24.get("content", ""))
              and any(r.levelno == logging.WARNING and "降级回传" in r.getMessage()
                      and "mock LLM down" in r.getMessage() for r in g24_handler.records))
    finally:
        g24_logger.removeHandler(g24_handler)

    # G25 资料为空（空串/纯空白）+ LLM 失败 → 维持"未找到相关攻略。"
    p25, ctx25 = make_plugin()
    await p25.on_load()
    ctx25.llm = MockLLM(hard_fail=True)
    r25a = await p25._direct_send(
        tool_name="stellasora_how", question="夏花攻略", material="",
        direct=True, query="夏花", stream_id="stream_g25a",
    )
    r25b = await p25._direct_send(
        tool_name="stellasora_how", question="夏花攻略", material="   ",
        direct=True, query="夏花", stream_id="stream_g25b",
    )
    check("G25 资料为空+LLM 失败→未找到（空串与纯空白一致）",
          r25a == {"name": "stellasora_how", "content": "未找到相关攻略。"}
          and r25b == {"name": "stellasora_how", "content": "未找到相关攻略。"})

    # G26 降级回传不写直发成品缓存：同查询第二次仍重新调 LLM、内存缓存保持为空
    # （对照 G13：成功路径第二次命中缓存 LLM 只调 1 次）
    p26, ctx26 = make_plugin(ttl=86400)
    await p26.on_load()
    ctx26.llm = MockLLM(fail=True)
    r26a = await p26.handle_how(query="夏花", group_id="g1", stream_id="stream_g26")
    r26b = await p26.handle_how(query="夏花", group_id="g1", stream_id="stream_g26")
    check("G26 降级回传不写缓存（二次查询重调 LLM+内存缓存为空+不直发）",
          len(ctx26.llm.calls) == 2
          and len(p26._get_answer_cache()._memory_cache) == 0
          and str(r26a.get("content", "")).startswith("[系统说明")
          and str(r26b.get("content", "")).startswith("[系统说明")
          and ctx26.send.sent == [])

    # G27 软失败（success=False）经 handle_how 真实链路降级回传
    p27, ctx27 = make_plugin()
    await p27.on_load()
    ctx27.llm = MockLLM(fail=True)
    r27 = await p27.handle_how(query="夏花", group_id="g1", stream_id="stream_g27")
    check("G27 软失败经真实链路降级回传（前缀+含资料+非未找到）",
          str(r27.get("content", "")).startswith("[系统说明")
          and relay_prefix in str(r27.get("content", ""))
          and "夏花" in str(r27.get("content", ""))
          and "未找到相关攻略" not in str(r27.get("content", "")))

    # G28 加工成功（响应纯空白视为失败）→ 降级回传：success=True 但 response 空白
    p28, ctx28 = make_plugin()
    await p28.on_load()
    ctx28.llm = MockLLM(answer="   ")
    r28 = await p28._direct_send(
        tool_name="stellasora_how", question="夏花攻略", material="空白响应降级资料",
        direct=True, query="夏花", stream_id="stream_g28",
    )
    check("G28 success=True 但响应空白→降级回传",
          str(r28.get("content", "")).startswith("[系统说明")
          and "空白响应降级资料" in str(r28.get("content", ""))
          and "未找到相关攻略" not in str(r28.get("content", "")))

    # G29-G30 表缓存 reload 接线（Task 3）：同步产出新统一表后 reload_team_table()
    # 使运行时表缓存即时失效——手动 /st_update 与每日 17:00 定时两通道均须接线。
    # plugin 以 from service import reload_team_table 绑定，故补丁挂在 plug 命名空间
    g29_calls: list = []
    g29_patch = unittest.mock.patch.object(plug, "reload_team_table", side_effect=lambda: g29_calls.append(True))
    g29_patch.start()
    try:
        # G29 手动更新通道：handle_update（授权路径）同步成功后调用 reload
        p29, ctx29 = make_plugin()
        await p29.on_load()
        orig_sync29 = plug.sync_offline_data
        plug.sync_offline_data = lambda **kwargs: {"status": "ok"}
        try:
            n_reloads = len(g29_calls)
            await p29.handle_update(stream_id="stream_g29", group_id="any_group")
            check("G29 handle_update 同步后调用 reload_team_table", len(g29_calls) == n_reloads + 1)
        finally:
            plug.sync_offline_data = orig_sync29

        # G30 定时同步通道：_schedule_daily_sync 内 sync 完成后调用 reload
        p30, ctx30 = make_plugin(ttl=3600)
        orig_sync30 = plug.sync_offline_data
        plug.sync_offline_data = lambda **kwargs: {"status": "ok"}
        delays = [0.05, 3600.0]

        def mock_calc_delay(*args, **kwargs):
            return delays.pop(0) if delays else 3600.0

        orig_calc30 = p30._calculate_delay_to_sync
        p30._calculate_delay_to_sync = mock_calc_delay
        n_reloads30 = len(g29_calls)
        try:
            await p30.on_load()
            await asyncio.sleep(0.2)
            check("G30 _schedule_daily_sync 同步后调用 reload_team_table",
                  len(g29_calls) == n_reloads30 + 1,
                  f"reload 次数={len(g29_calls)}（前值 {n_reloads30}）")
        finally:
            plug.sync_offline_data = orig_sync30
            p30._calculate_delay_to_sync = orig_calc30
            if p30._sync_task and not p30._sync_task.done():
                p30._sync_task.cancel()
                await asyncio.gather(p30._sync_task, return_exceptions=True)
    finally:
        g29_patch.stop()
        g_team_table_patch.stop()
        service.reload_team_table()


# ===== 节 H：输出格式（verbatim trust + infodoc 输出规则） =====

async def run_output_format() -> None:
    print("--- H 输出格式 ---")

    # H1 verbatim trust：LLM 返回 markdown 时不清洗，新鲜与缓存路径均原样直发
    p, ctx = make_plugin(ttl=3600)
    await p.on_load()
    # 注意：_direct_send 会对 LLM 返回做 .strip()，样例两端不能是空白字符
    markdown_response = (
        "**夏花攻略**\n"
        "### 推荐阵容\n"
        "主控位：夏花，支援位：猫眼。\n"
        "- 秘纹推荐：`风之眼`、`狂风呼啸`\n"
        "| 纹章 | 词条 |\n"
        "| 三角形 | 风系穿透 |"
    )
    ctx.llm = MockLLM(answer=markdown_response)
    res1 = await p.handle_how(query="夏花", question="夏花怎么玩", group_id="g1", stream_id="s_h1")
    sent1 = ctx.send.sent[0][1]
    res2 = await p.handle_how(query="夏花", question="夏花怎么玩", group_id="g1", stream_id="s_h2")
    sent2 = ctx.send.sent[1][1]
    check("H1 LLM 输出原样直发（新鲜+缓存 verbatim）",
          sent1 == markdown_response and sent2 == markdown_response
          and "已直接发送" in res1["content"] and "已直接发送" in res2["content"])

    # H2 infodoc 输出规则关键词注入（赤霞→Chaton 火队）
    p3, ctx3 = make_plugin()
    p3._plugin_config_instance.query.inject_knowledge = True
    await p3.on_load()
    ctx3.llm = MockLLM(answer="赤霞攻略纯文本输出")

    orig = {
        "index": service.StelladbFetcher.fetch_infodoc_index,
        "infodoc": service.StelladbFetcher.fetch_infodoc,
        "trekker": service.StelladbFetcher.fetch_trekker,
    }
    try:
        service.StelladbFetcher.fetch_infodoc_index = lambda self: "Chaton (Ignis) | Rotation: E > Q > R | Chaton (Main Slot) | Flora (1st Supp. Slot)"
        service.StelladbFetcher.fetch_infodoc = lambda self, element: (
            "Chaton (Ignis) | Main Slot | Recommended Main Discs: DiscA (C1), DiscB\n"
            "Emblem: Triangle | Ignis PEN\n"
            "Priority Potentials: Mark of Flame +3\n"
        )
        service.StelladbFetcher.fetch_trekker = lambda self, num_id: "Ignis character data with Ignis element"
        res = await p3.handle_how(query="赤霞", question="赤霞攻略", group_id="g1", stream_id="s_h3")
        prompt = ctx3.llm.calls[0]["prompt"]
        # 新版关键词：分组结构说明（N. 队伍名）+ 队友并集行 + 同级词条合并；
        # 旧单角色字样（本角色/配队N）随规则 4 改写一并清除
        missing = [kw for kw in ("队友：", "N. ", "合并为一条", "纹章", "秘纹") if kw not in prompt]
        leftover = [kw for kw in ("本角色", "配队N（") if kw in prompt]
        check("H2 prompt 含分组新结构标记且旧单角色字样清除",
              not missing and not leftover and "已直接发送" in res["content"],
              f"缺少 {missing} 残留 {leftover}")
    finally:
        service.StelladbFetcher.fetch_infodoc_index = orig["index"]
        service.StelladbFetcher.fetch_infodoc = orig["infodoc"]
        service.StelladbFetcher.fetch_trekker = orig["trekker"]


# ===== 节 I：非阻塞探针 =====

async def run_nonblocking() -> None:
    print("--- I 非阻塞探针 ---")
    p, ctx = make_plugin()
    await p.on_load()

    # 把 query_what 替换为含 0.3s 同步阻塞的函数；若它在事件循环内执行，
    # 并发的 0.1s 探针将漂移 ≥0.15s 判 FAIL
    orig_query_what, orig_count = plug.query_what, plug.count_character_names

    def slow_query_what(*args, **kwargs):
        time.sleep(0.3)
        return "mock攻略内容"

    plug.query_what = slow_query_what
    plug.count_character_names = lambda q: 1
    try:
        probe_done: list = []

        async def probe():
            t0 = time.monotonic()
            await asyncio.sleep(0.1)
            probe_done.append(time.monotonic() - t0)

        await asyncio.gather(
            p.handle_what(query="夏花", group_id="g1", stream_id="stream_i"),
            probe(),
        )
        drift = (probe_done[0] if probe_done else float("inf")) - 0.1
        check("I1 同步重活不阻塞事件循环（探针漂移<0.15s）", drift < 0.15, f"漂移 {drift:.3f}s")
    finally:
        plug.query_what = orig_query_what
        plug.count_character_names = orig_count

    # 异常传播：query_what 抛错时 handle_what 不挂起（5s 超时保护）
    def raise_query_what(*args, **kwargs):
        raise RuntimeError("test error")

    plug.query_what = raise_query_what
    try:
        behavior = "propagated"
        try:
            result = await asyncio.wait_for(
                p.handle_what(query="夏花", group_id="g1", stream_id="stream_i_b"), timeout=5.0)
            behavior = "caught_as_not_found" if "未找到" in result.get("content", "") else "returned"
            if behavior == "returned":
                behavior = "unexpected_return"
        except asyncio.TimeoutError:
            behavior = "hung"
        except RuntimeError:
            behavior = "propagated"
        check("I2 异常干净传播不挂起", behavior in ("propagated", "caught_as_not_found"), behavior)
    finally:
        plug.query_what = orig_query_what


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
    # J1（契约翻转）：query 由旧"只传名字本身"改为支持空格分隔多名 + 兜底归一
    check("J1 how.query 多名+兜底归一契约（取代旧'只传名字本身'）",
          "空格分隔多个" in desc and "兜底归一" in desc and "只传名字本身" not in desc, desc)
    # J2 工具 description 契约：用途示例（配队/攻略/秘纹）保留；元素名宣称清除；
    # question 参数 required=True；query description 含多名写法
    check("J2 how.description 保留用途示例且清除元素名宣称+question 必传",
          all(w in how_desc for w in ("攻略", "配队", "秘纹"))
          and "元素" not in how_desc
          and not any(w in how_desc for w in ("水", "火", "风"))
          and how_required.get("question") is True
          and "空格分隔多个" in desc,
          f"desc={how_desc}, required={how_required}")
    # J3（契约翻转）：query 不再宣称元素中文名（水/火/风/地/光/暗）
    check("J3 how.query 清除元素中文名宣称",
          not any(elem in desc for elem in ("水", "火", "风", "地", "光", "暗"))
          and "元素" not in desc, desc)
    what_desc = tool_infos.get("stellasora_what", {}).get("query", "")
    check("J4 what.query 描述未受影响", "角色" in what_desc and "装备" in what_desc, what_desc)


# ===== 节 K：手动更新指令（st_update） =====

async def run_manual_update() -> None:
    print("--- K 手动更新指令 ---")
    p, ctx = make_plugin()
    await p.on_load()

    # 1. 验证 @Command 注册属性
    func = getattr(plug.StellaSoraPlugin, "handle_update", None)
    check("K1 handle_update 方法存在", func is not None)
    attr = "__maibot_component_info__"
    info = getattr(func, attr, None) if func is not None else None
    check("K2 Command 装饰器元数据存在", info is not None)
    if info is not None:
        check("K3 指令名匹配 st_update", getattr(info, "name", "") == "st_update")
        check("K4 正则 pattern 匹配 ^/st_update", getattr(info, "command_pattern", "") == r"^/st_update")

    # 2. 鉴权失败拦截
    p.config.access_control.mode = "whitelist"
    p.config.access_control.whitelist = ["allowed_group", "allowed_user"]
    denied_res = await p.handle_update(stream_id="s1", group_id="denied_group", user_id="denied_user")
    check("K5 未授权调用被拦截（返回 False）", denied_res[0] is False)
    check("K6 拦截返回码为 1 且包含权限提示", denied_res[2] == 1 and "权限" in denied_res[1])
    check("K7 未授权不向聊天流发送消息", len(ctx.send.sent) == 0)

    # 3. 授权通过调用全量同步并清空缓存
    p.config.access_control.mode = "off"
    orig_sync = plug.sync_offline_data
    sync_called = []

    def mock_sync_offline_data(**kwargs):
        sync_called.append(kwargs)
        return {"status": "ok"}

    plug.sync_offline_data = mock_sync_offline_data
    # Task 3 reload 接线：手动更新同步成功后 reload_team_table() 失效表缓存
    # （plugin 以 from service import reload_team_table 绑定，补丁挂 plug 命名空间）
    reload_calls: list = []
    orig_reload = plug.reload_team_table
    plug.reload_team_table = lambda: reload_calls.append(True)
    try:
        # 准备缓存测试文件与内存数据
        answers_dir = p._cache_dir_ready() / "answers"
        answers_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = answers_dir / "test_answer.json"
        dummy_file.write_text("{}", encoding="utf-8")
        cache_mgr = p._get_answer_cache()
        cache_mgr._memory_cache["test_key"] = "cached_val"

        success, msg, code = await p.handle_update(stream_id="stream_k", group_id="any_group")
        check("K8 授权调用返回成功 True", success is True)
        check("K9 返回码为 2", code == 2)
        check("K10 调用 sync_offline_data(sync_all=True)", len(sync_called) == 1 and sync_called[0].get("sync_all") is True)
        check("K11 发送开始与完成两批提示消息", len(ctx.send.sent) >= 2 and any("正在后台同步" in t[1] for t in ctx.send.sent) and any("同步完成" in t[1] for t in ctx.send.sent))
        check("K12 磁盘 answers 缓存被清空", not dummy_file.exists())
        check("K13 内存 answers 缓存被清空", len(cache_mgr._memory_cache) == 0)
        check("K20 同步后调用 reload_team_table 失效表缓存", len(reload_calls) == 1)
        service.reload_team_table()
    finally:
        plug.sync_offline_data = orig_sync
        plug.reload_team_table = orig_reload

    # 4. 同步异常降级
    def mock_sync_fail(**kwargs):
        raise RuntimeError("network down")

    plug.sync_offline_data = mock_sync_fail
    try:
        fail_res = await p.handle_update(stream_id="stream_fail", group_id="any_group")
        check("K14 同步异常返回 False", fail_res[0] is False)
        check("K15 异常返回码为 1 且包含错误信息", fail_res[2] == 1 and "network down" in fail_res[1])
    finally:
        plug.sync_offline_data = orig_sync

    # 5. 离线持久化数据读取与 force_update 参数
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        # 准备 offline 假数据（路径为 infodocs/*.txt 或 *.json，presets/presets.txt 或 presets.json）
        offline_infodocs = tmp_dir / "offline" / "infodocs"
        offline_infodocs.mkdir(parents=True, exist_ok=True)
        (offline_infodocs / "index.txt").write_text("Offline Infodoc Index Content", encoding="utf-8")
        (offline_infodocs / "ignis.txt").write_text("Offline Ignis Detailed Content", encoding="utf-8")

        offline_presets = tmp_dir / "offline" / "presets"
        offline_presets.mkdir(parents=True, exist_ok=True)
        (offline_presets / "presets.txt").write_text("Offline Presets TSV Content", encoding="utf-8")

        st_fetcher = service.StelladbFetcher(tmp_dir, offline_dir=tmp_dir / "offline")
        gd_fetcher = service.GoogleDocFetcher(tmp_dir, offline_dir=tmp_dir / "offline")

        network_calls: list = []

        def mock_open(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            network_calls.append(url)
            return _FakeHTTPResponse(b"Online Fresh Content")

        # 在 fetcher 级网络边界注入 Mock（实例级 opener，支持代理配置），
        # 不全局 patch urllib.request.urlopen
        st_fetcher._opener = _FakeOpener(mock_open)
        gd_fetcher._opener = _FakeOpener(mock_open)

        # 5.1 force_update=False 时直接读取 offline 本地文件，不发生网络请求
        res_idx = st_fetcher.fetch_infodoc_index(force_update=False)
        res_elem = st_fetcher.fetch_infodoc("ignis", force_update=False)
        res_pre = gd_fetcher.fetch_presets(force_update=False)

        check("K16 force_update=False 优先读取离线文件",
              res_idx == "Offline Infodoc Index Content"
              and res_elem == "Offline Ignis Detailed Content"
              and "Offline Presets TSV Content" in res_pre)
        check("K17 离线命中时不产生网络调用", len(network_calls) == 0, str(network_calls))

        # 5.2 force_update=True 时绕过离线文件发起网络请求
        res_idx_force = st_fetcher.fetch_infodoc_index(force_update=True)
        res_elem_force = st_fetcher.fetch_infodoc("ignis", force_update=True)
        res_pre_force = gd_fetcher.fetch_presets(force_update=True)

        check("K18 force_update=True 强制抓取在线新数据",
              res_idx_force == "Online Fresh Content"
              and res_elem_force == "Online Fresh Content"
              and "Online Fresh Content" in res_pre_force)
        check("K19 force_update=True 发起 3 次网络请求", len(network_calls) == 3, str(network_calls))


# ===== 节 L：定时自动同步与生命周期注销 =====

async def run_daily_sync_schedule() -> None:
    """测试每日 17:00 等待秒数计算、后台任务调度、同步缓存清理与注销。"""
    # 1. 等待秒数算法断言
    # Case 1: 当前 16:59:50 -> 目标当天 17:00:00 -> 10 秒
    t1 = datetime(2026, 9, 8, 16, 59, 50)
    delay1 = plug.StellaSoraPlugin._calculate_delay_to_sync(t1, target_hour=17, target_minute=0)
    check("L1 未过 17:00 等待秒数精确到当天 17:00", delay1 == 10.0)

    # Case 2: 当前正好 17:00:00 -> 目标明天 17:00:00 -> 86400 秒 (24h)
    t2 = datetime(2026, 9, 8, 17, 0, 0)
    delay2 = plug.StellaSoraPlugin._calculate_delay_to_sync(t2, target_hour=17, target_minute=0)
    check("L2 正好 17:00:00 等待秒数为明天 17:00（86400秒）", delay2 == 86400.0)

    # Case 3: 当前已过 17:00:05 -> 目标明天 17:00:00 -> 86400 - 5 = 86395 秒
    t3 = datetime(2026, 9, 8, 17, 0, 5)
    delay3 = plug.StellaSoraPlugin._calculate_delay_to_sync(t3, target_hour=17, target_minute=0)
    check("L3 已过 17:00 等待秒数计算至明天 17:00（86395秒）", delay3 == 86395.0)

    # Case 4: 上午 10:00:00 -> 目标当天 17:00:00 -> 7 * 3600 = 25200 秒
    t4 = datetime(2026, 9, 8, 10, 0, 0)
    delay4 = plug.StellaSoraPlugin._calculate_delay_to_sync(t4, target_hour=17, target_minute=0)
    check("L4 上午时间计算至当天 17:00（25200秒）", delay4 == 25200.0)

    # 2. 模拟极短延迟触发同步与缓存清理
    sync_called = []
    orig_sync = plug.sync_offline_data

    def mock_sync_offline_data(**kwargs):
        sync_called.append(kwargs)
        return {"status": "ok"}

    plug.sync_offline_data = mock_sync_offline_data
    # Task 3 reload 接线：定时同步产出新统一表后 reload_team_table() 失效表缓存
    # （plugin 以 from service import reload_team_table 绑定，补丁挂 plug 命名空间）
    reload_calls: list = []
    orig_reload = plug.reload_team_table
    plug.reload_team_table = lambda: reload_calls.append(True)
    p, ctx = make_plugin(ttl=3600)

    # 模拟 delay 序列：第一次返回 0.05 秒触发同步，后续返回 3600 秒防空转
    delays = [0.05, 3600.0]

    def mock_calculate_delay(*args, **kwargs):
        if delays:
            return delays.pop(0)
        return 3600.0

    orig_calc = p._calculate_delay_to_sync
    p._calculate_delay_to_sync = mock_calculate_delay

    try:
        # 加载插件，启动后台任务
        await p.on_load()
        check("L5 on_load 成功创建后台任务 _sync_task", p._sync_task is not None and not p._sync_task.done())

        # 准备缓存文件与内存数据
        answers_dir = p._cache_dir_ready() / "answers"
        answers_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = answers_dir / "sched_test.json"
        dummy_file.write_text("{}", encoding="utf-8")
        cache_mgr = p._get_answer_cache()
        cache_mgr._memory_cache["sched_key"] = "cached_val"

        # 等待后台任务唤醒并完成一次同步 (0.05s 延时 + 执行)
        await asyncio.sleep(0.15)

        check("L6 后台任务触发 sync_offline_data(sync_all=True)", len(sync_called) >= 1 and sync_called[0].get("sync_all") is True)
        check("L7 同步后 answers 磁盘缓存被清空", not dummy_file.exists())
        check("L8 同步后 answers 内存缓存被清空", len(cache_mgr._memory_cache) == 0)
        check("L11 定时同步完成后调用 reload_team_table 失效表缓存", len(reload_calls) >= 1)
        service.reload_team_table()

        # 3. on_unload 优雅注销与取消
        task_ref = p._sync_task
        await p.on_unload()
        check("L9 on_unload 后 _sync_task 被置为 None", p._sync_task is None)
        check("L10 后台任务被成功取消并完成 (done)", task_ref is not None and task_ref.done())
    finally:
        plug.sync_offline_data = orig_sync
        plug.reload_team_table = orig_reload
        p._calculate_delay_to_sync = orig_calc
        if p._sync_task and not p._sync_task.done():
            p._sync_task.cancel()
            await asyncio.gather(p._sync_task, return_exceptions=True)


# ===== 节 M：统一队伍-槽位表构建器测试 =====

def run_section_m() -> None:
    """测试 team_table 构建器：解码、伪影清洗、固化关联、无码拆行、校验与 rotation 固化。"""
    import base64
    import struct

    lookup = DictLookup(DATA_DIR)
    lookup._load()

    # M1 & M2: 预设码解码与异常兜底
    sample_code = "AAAAnAAAAIIAAAB9MYDIIYDNgCBsAKyAIACA"
    decoded = team_table.decode_preset_code(sample_code)
    check("M1 预设码解码样本 [156, 130, 125]", decoded == [156, 130, 125], f"decoded={decoded}")
    check("M2 空串解码返回 None", team_table.decode_preset_code("") is None)
    check("M3 短串与非法字符返回 None", team_table.decode_preset_code("short") is None and team_table.decode_preset_code("invalid@@@") is None)
    check("M4 全零 CharId 返回 None", team_table.decode_preset_code("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA") is None)

    # M5-M7: 伪影清洗与弯引号规范化
    artifact_doc = """Aqua
Teresa’s Team
\tMain Trekker
\tPreset Code
\tWIP
\tAAAAnAAAAIIAAAB9MYDIIYDNgCBsAKyAIACA
\tAAAAfwAAAIIAAAB9VbYjEADNkCBsAKyAOACA
"""
    parsed = team_table.parse_presets_doc(artifact_doc)
    check("M5 裸 WIP 行跳过不作为队名", len(parsed) == 2 and parsed[0]["team_name_raw"] == "Teresa’s Team")
    check("M6 孤码正确归属前置队伍", parsed[1]["team_name_raw"] == "Teresa’s Team")

    infodocs_curly = {
        "aqua": """Teresa's Team | ⏏ Back to Top ⏏
Nazuna (5★) | 1/1/1/1
Donna (5★) | 1/1/1/1
Freesia (5★) | 1/1/1/1
⏏ BACK TO TOP ⏏
"""
    }
    table_curly = team_table.build_team_table(artifact_doc, infodocs_curly, lookup, "")
    check(
        "M7 弯引号规范化关联直引号区块",
        len(table_curly["rows"]) == 2
        and table_curly["rows"][0]["guide_ref"] == {"element": "aqua", "block": "Teresa's Team"}
        and table_curly["rows"][0]["team_name_preset"] == "Teresa’s Team"
        and table_curly["rows"][0]["team_name_infodoc"] == "Teresa's Team",
    )

    # M8: Sparkla 3 码 -> 3 行同 guide_ref
    sparkla_doc = """Terra
Sparkla (Rapid Fire) WIP
\tPreset Code
\tAAAAjAAAAJwAAACfzbAbAADAQBgNhsWIAGAw
\tAAAAjAAAAJwAAABrzbAbAADBgBgMBmAGGGAG
\tAAAAjAAAAJwAAAB0zbAbAADBgBgMBsGAAGwA
"""
    infodocs_sparkla = {
        "terra": """Sparkla (Rapid Fire) WIP | ⏏ Back to Top ⏏
Sparkla (5★) | 1/1/1/1
Nazuna (5★) | 1/1/1/1
Springseek Coronis (5★) | 1/1/1/1
Tilia (5★) | 1/1/1/1
Ridge (5★) | 1/1/1/1
⏏ BACK TO TOP ⏏
"""
    }
    table_sparkla = team_table.build_team_table(sparkla_doc, infodocs_sparkla, lookup, "")
    sp_rows = table_sparkla["rows"]
    check(
        "M8 Sparkla 3 码生成 3 行且 guide_ref 一致",
        len(sp_rows) == 3 and all(r["guide_ref"] == {"element": "terra", "block": "Sparkla (Rapid Fire) WIP"} for r in sp_rows),
    )

    # M9: 无码区块共享前缀拆行
    infodocs_codeless = {
        "aqua": """Nazuna-Freesia | ⏏ Back to Top ⏏
Nazuna (5★) | 1/1/1/1
Freesia (5★) | 1/1/1/1
Tilia (5★) | 1/1/1/1
Iris (5★) | 1/1/1/1
⏏ BACK TO TOP ⏏
"""
    }
    table_codeless = team_table.build_team_table("Aqua\n", infodocs_codeless, lookup, "")
    cl_rows = table_codeless["rows"]
    check(
        "M9 无码区块 4 段按前缀拆出 2 行",
        len(cl_rows) == 2
        and cl_rows[0]["main_key"] == "aqua::Nazuna-Freesia::Tilia"
        and cl_rows[1]["main_key"] == "aqua::Nazuna-Freesia::Iris"
        and cl_rows[0]["preset_code"] is None
        and cl_rows[0]["team_name_infodoc"] == "Nazuna-Freesia",
    )

    # M10: 无效行三例（2 段块/未知成员/孤码缺名）进 report 不入 rows
    infodocs_invalid = {
        "aqua": """Short-Block | ⏏ Back to Top ⏏
Nazuna (5★) | 1/1/1/1
Freesia (5★) | 1/1/1/1
⏏ BACK TO TOP ⏏
"""
    }
    fake_code = base64.b64encode(struct.pack(">III", 999999, 130, 125) + b"\x00" * 15).decode("utf-8")
    invalid_presets_doc = f"""Aqua
Fake Team
\tPreset Code
\t{fake_code}
Ignis
\tPreset Code
\tAAAAnAAAAIIAAAB9MYDIIYDNgCBsAKyAIACA
"""
    table_invalid = team_table.build_team_table(invalid_presets_doc, infodocs_invalid, lookup, "")
    inv_reasons = [item["reason"] for item in table_invalid["report"]["invalid_rows"]]
    check("M10 无效行不入 rows", len(table_invalid["rows"]) == 0)
    check(
        "M11 无效行报告记录三类原因（缺槽位/成员未知/缺名字）",
        any("缺槽位" in r for r in inv_reasons)
        and any("成员未知" in r for r in inv_reasons)
        and any("缺名字" in r for r in inv_reasons),
        str(inv_reasons),
    )

    # M12: Rotation 固化（三行结构、非空且逐字一致）
    index_text_fixture = """Nazuna (5★) | Flora | Wraith
Rotation | Rotation | Rotation
Nazuna special 5-star combo rotation | Flora combo | Wraith combo
"""
    rot_expected = service.extract_rotation(index_text_fixture, "Nazuna")
    table_rot = team_table.build_team_table(
        """Aqua
Nazuna Team
\tPreset Code
\tAAAAnAAAAIIAAAB9MYDIIYDNgCBsAKyAIACA
""",
        {"aqua": ""},
        lookup,
        index_text_fixture,
    )
    rot_actual = table_rot["rows"][0]["rotation"] if table_rot["rows"] else ""
    check(
        "M12 Rotation 固化非空且与 extract_rotation 逐字一致",
        bool(rot_actual) and rot_actual == rot_expected == "Nazuna special 5-star combo rotation",
        f"actual={repr(rot_actual)}, expected={repr(rot_expected)}",
    )


# ===== 节 N：slot 查询服务测试 =====

def run_section_n() -> None:
    """测试 slot 查询服务：表加载/交集查询/元素过滤/缓存失效/异常兜底/按名提取区块。"""
    import unittest.mock

    # 1. 构造内联 fixture 表：包含有码行、无码行、跨元素行
    fixture_table = {
        "rows": [
            {
                "main_key": "AAAAnAAAAIIAAAB9MYDIIYDNgCBsAKyAIACA",
                "preset_code": "AAAAnAAAAIIAAAB9MYDIIYDNgCBsAKyAIACA",
                "element": "aqua",
                "slots": [
                    {"char_id": 156, "en": "Nazuna", "cn": "小禾"},
                    {"char_id": 130, "en": "Donna", "cn": "多娜"},
                    {"char_id": 125, "en": "Canglan", "cn": "苍兰"},
                ],
                "team_name_preset": "Nazuna-Donna Team",
                "team_name_infodoc": "Nazuna-Donna",
                "guide_ref": {"element": "aqua", "block": "Nazuna-Donna"},
                "rotation": "Nazuna rotation",
            },
            {
                "main_key": "aqua::Nazuna-Freesia::Tilia",
                "preset_code": None,
                "element": "aqua",
                "slots": [
                    {"char_id": 156, "en": "Nazuna", "cn": "小禾"},
                    {"char_id": 127, "en": "Freesia", "cn": "芙莉西亚"},
                    {"char_id": 110, "en": "Tilia", "cn": "缇莉亚"},
                ],
                "team_name_preset": "",
                "team_name_infodoc": "Nazuna-Freesia",
                "guide_ref": {"element": "aqua", "block": "Nazuna-Freesia"},
                "rotation": "",
            },
            {
                "main_key": "ignis::Kagari::Flora",
                "preset_code": None,
                "element": "ignis",
                "slots": [
                    {"char_id": 140, "en": "Kagari", "cn": "篝"},
                    {"char_id": 130, "en": "Donna", "cn": "多娜"},
                    {"char_id": 115, "en": "Flora", "cn": "芙萝拉"},
                ],
                "team_name_preset": "",
                "team_name_infodoc": "Kagari",
                "guide_ref": {"element": "ignis", "block": "Kagari"},
                "rotation": "",
            },
        ],
        "report": {"invalid_rows": []},
    }

    try:
        # 注入 fixture 表到缓存中进行隔离测试
        service._team_table_cache = fixture_table

        # N1: 单角色查询命中（含无码行）
        rows_156 = service.find_team_rows([156])
        check(
            "N1 单角色查询命中所有含该角色的队伍（含无码行）",
            len(rows_156) == 2
            and any(r["preset_code"] is not None for r in rows_156)
            and any(r["preset_code"] is None for r in rows_156),
            f"rows_156={rows_156}",
        )

        # N2: 双角色交集查询精准命中
        rows_156_130 = service.find_team_rows([156, 130])
        check(
            "N2 双角色交集查询精准命中同时包含两者的行",
            len(rows_156_130) == 1
            and rows_156_130[0]["main_key"] == "AAAAnAAAAIIAAAB9MYDIIYDNgCBsAKyAIACA",
            f"rows_156_130={rows_156_130}",
        )

        # N3: 元素过滤（大小写不敏感且排他）
        rows_donna_aqua = service.find_team_rows([130], element="aqua")
        rows_donna_ignis = service.find_team_rows([130], element="IGNIS")
        rows_donna_terra = service.find_team_rows([130], element="terra")
        check(
            "N3 元素过滤大小写不敏感且精准排他",
            len(rows_donna_aqua) == 1
            and rows_donna_aqua[0]["element"] == "aqua"
            and len(rows_donna_ignis) == 1
            and rows_donna_ignis[0]["element"] == "ignis"
            and len(rows_donna_terra) == 0,
            f"aqua={len(rows_donna_aqua)}, ignis={len(rows_donna_ignis)}, terra={len(rows_donna_terra)}",
        )

        # N4: 零命中与冲突条件
        rows_empty = service.find_team_rows([999999])
        rows_conflict = service.find_team_rows([156, 140])
        check(
            "N4 不存在角色或冲突成员交集返回空列表",
            rows_empty == [] and rows_conflict == [],
            f"empty={rows_empty}, conflict={rows_conflict}",
        )

        # N5: reload_team_table 清除缓存
        service.reload_team_table()
        check("N5 reload_team_table 成功清除缓存", service._team_table_cache is None)

        # N6: 缓存加载失败处理（损坏文件 / 缺失文件 -> {"rows": [], "report": {}} 不崩溃）
        service.reload_team_table()
        with unittest.mock.patch.object(service.Path, "open", side_effect=ValueError("Corrupted JSON")):
            res_corrupt = service.load_team_table()
        check(
            "N6 文件损坏时记录日志并返回空表结构不抛异常",
            res_corrupt == {"rows": [], "report": {}},
            f"res_corrupt={res_corrupt}",
        )

        service.reload_team_table()
        with unittest.mock.patch.object(service.Path, "is_file", return_value=False):
            res_missing = service.load_team_table()
        check(
            "N7 文件缺失时记录日志并返回空表结构不抛异常",
            res_missing == {"rows": [], "report": {}},
            f"res_missing={res_missing}",
        )

        # N8: 真实磁盘 team_table.json 加载测试（行数 > 0 且格式有效）
        service.reload_team_table()
        real_table = service.load_team_table()
        real_rows = real_table.get("rows", [])
        check(
            "N8 真实 team_table.json 成功加载且行数 > 0",
            len(real_rows) > 0 and service._team_table_cache is not None,
            f"count={len(real_rows)}",
        )

        # N9: extract_block_by_name 对真实 aqua.json 提取 Nazuna-Donna
        aqua_path = DATA_DIR / "offline" / "infodocs" / "aqua.json"
        with aqua_path.open("r", encoding="utf-8") as f:
            aqua_data = json.load(f)["data"]
        block = service.extract_block_by_name(aqua_data, "Nazuna-Donna")
        check(
            "N9 extract_block_by_name 提取真实区块成功且结构完整",
            block is not None
            and block.get("name") == "Nazuna-Donna"
            and block.get("members") == ["Nazuna", "Donna", "Freesia"]
            and block.get("roles", {}).get("Nazuna") == "主控位"
            and block.get("roles", {}).get("Donna") == "支援位"
            and len(block.get("segments", {})) == 3,
            f"block={block}",
        )

        # N10: extract_block_by_name 不存在区块或空输入返回 None
        none_block = service.extract_block_by_name(aqua_data, "NonExistentBlockXYZ")
        empty_block = service.extract_block_by_name("", "Nazuna-Donna")
        check(
            "N10 extract_block_by_name 不存在区块或空输入返回 None",
            none_block is None and empty_block is None,
            f"none_block={none_block}, empty_block={empty_block}",
        )

        # N13（真实 terra 区块 emblem 翻案后网格真值基线重锁）：
        # 真实 terra 页 Terra Mark Amplification 区块经 service.query_how_rows 渲染输出——
        # emblem 行无裸数字行号子条目（297/298/299/300/301 等全部滤除），真实词条保留。
        # 小禾真值：70级=1条（充能效率（主位） 30%）、80级=3条（充能效率（主位） 30%、主技能等级 +3 等级、终极技等级 +3 等级）、
        # 90级=4条（自然之触 +3 等级、飚速手推车 +3 等级、古灵精怪 +3 等级、萌萌助威 +3 等级），
        # 3 成员 × 3 等级 = 9 行 emblem，各成员各等级词条与等级前缀完好。
        row_tma = {
            "main_key": "terra::TMA::1",
            "preset_code": None,
            "element": "terra",
            "slots": [
                {"char_id": 156, "en": "Nazuna", "cn": "小禾", "slot": "主控位"},
                {"char_id": 149, "en": "Gerie", "cn": "格芮", "slot": "支援位"},
                {"char_id": 110, "en": "Tilia", "cn": "缇莉娅", "slot": "支援位"},
            ],
            "team_name_preset": "Terra Mark Amplification",
            "team_name_infodoc": "Terra Mark Amplification",
            "guide_ref": {"element": "terra", "block": "Terra Mark Amplification"},
            "rotation": "",
        }
        rows_tma = [row_tma]
        out_tma = service.query_how_rows(rows_tma, question="小禾 格芮 缇莉娅攻略")
        out_lines_tma = out_tma.split("\n")
        # 收集渲染输出中的全部 emblem 行（"纹章推荐：" 之后至空行前的等级标签行）
        emblem_rows_tma: list = []
        for i, ln in enumerate(out_lines_tma):
            if ln == "纹章推荐：":
                j = i + 1
                while j < len(out_lines_tma) and out_lines_tma[j]:
                    emblem_rows_tma.append(out_lines_tma[j])
                    j += 1
        emblem_text_tma = "\n".join(emblem_rows_tma)
        check(
            "N13 真实terra区块emblem无裸数字行号且真实词条保留（基线翻转）",
            len(emblem_rows_tma) == 9
            and all(ln.startswith(("70级：", "80级：", "90级：")) for ln in emblem_rows_tma)
            and all(not s.strip().isdigit() for ln in emblem_rows_tma for s in ln.split("、"))
            and not any(
                d in emblem_text_tma
                for d in ("297", "298", "299", "300", "301", "308", "309", "310", "321", "322", "323")
            )
            and emblem_rows_tma[0] == "70级：充能效率（主位） 30%"
            and len(emblem_rows_tma[0].split("、")) == 1  # 70级：1 真实词条
            and len(emblem_rows_tma[1].split("、")) in (2, 3)  # 80级：真实词条
            and len(emblem_rows_tma[2].split("、")) in (4, 5)  # 90级：真实词条
            and "自然之触 +3 等级" in emblem_rows_tma[2]
            and any("地系穿透 110" in ln for ln in emblem_rows_tma)
            and any("印记伤害 80%" in ln and "暴击率 15%" in ln for ln in emblem_rows_tma),
            f"emblem_rows={emblem_rows_tma}",
        )

        # 新增翡冷翠纹章金标断言（Firenze (Main Skill) 转置真值防线）
        umbra_path = DATA_DIR / "offline" / "infodocs" / "umbra.json"
        with umbra_path.open("r", encoding="utf-8") as f:
            umbra_data = json.load(f)["data"]
        f_block = service.extract_block_by_name(umbra_data, "Firenze (Main Skill)")
        f_emblem_raw = f_block["segments"]["Firenze"]["emblem"] if f_block else []
        replacer = service._instances[str(DATA_DIR)][4]
        f_emblem = [replacer.replace(ln) for ln in f_emblem_raw]
        e70 = next((ln for ln in f_emblem if ln.startswith("70级：")), "")
        e90 = next((ln for ln in f_emblem if ln.startswith("90级：")), "")
        check(
            "N13b 翡冷翠纹章转置金标断言：70级无90级词条且90级包含高危/追猎/买定",
            f_block is not None
            and "暗系穿透 110" in e70
            and "暴击率 15%" in e70
            and "技能伤害 20%" in e70
            and "暗系伤害 12%" in e70
            and "追猎指令" not in e70
            and "买定离手" not in e70
            and "高危风险 +3 等级" in e90
            and "追猎指令 +3 等级" in e90
            and "买定离手 +3 等级" in e90
            and "技能伤害 20%" in e90,
            f"e70={e70!r}, e90={e90!r}",
        )

        # N11: _filter_emblem_entry 混合串仅滤纯数字子条目 + 三边界（空串/全数字串/无前缀）
        check(
            "N11 emblem混合串仅滤纯数字子条目（'310'滤除,'30%'/'110'保留）",
            service._filter_emblem_entry("70级：充能效率 30%、310、地系穿透 110、30%")
            == "70级：充能效率 30%、地系穿透 110、30%",
            f"got={service._filter_emblem_entry('70级：充能效率 30%、310、地系穿透 110、30%')!r}",
        )
        check(
            "N11b emblem三边界：空串恒等/全数字串滤空/无前缀档标签保留在首非数字子条目",
            service._filter_emblem_entry("") == ""
            and service._filter_emblem_entry("297、298") == ""
            and service._filter_emblem_entry("第4档：充能效率 30%、297") == "第4档：充能效率 30%"
            and service._filter_emblem_entry("70级：无需升级") == "70级：无需升级",
            f"空串={service._filter_emblem_entry('')!r}, "
            f"全数字={service._filter_emblem_entry('297、298')!r}, "
            f"第4档={service._filter_emblem_entry('第4档：充能效率 30%、297')!r}",
        )

        # N12: _filter_noise_lines 纯数字行丢弃 + 连续空行折叠
        # （旧 re.sub(r"\n{3,}", "\n\n") 的列表等价语义：≥2 连续空行折叠为 1）
        check(
            "N12 行序列过滤：纯数字行丢弃且真实行保留,≥2连续空行折叠为1",
            service._filter_noise_lines(["287", "段落一", "", "", "", "段落二", "291"])
            == ["段落一", "", "段落二"]
            and service._filter_noise_lines(["297", "298"]) == []
            and service._filter_noise_lines([]) == []
            and service._filter_noise_lines(["地系穿透 110", "+3 levels", "30%"])
            == ["地系穿透 110", "+3 levels", "30%"],
            f"got={service._filter_noise_lines(['287', '段落一', '', '', '', '段落二', '291'])!r}",
        )

        # N14: 渲染端到端——fixture infodoc 含 description/discs 行号噪声行，
        # 经 query_how_rows 真实路径后滤除且真实行保留
        fold_infodoc = (
            "Fold Test Block | ⏏ Back to Top ⏏\n"
            "Description | Skill Upgrade Priority\n"
            "Nazuna (5★) | 1/10/1/10 (Main Skill only)\n"
            "888\n"
            "ZZFOLDDESC 段落一\n"
            "Priority Potentials | Recommended Main Discs\n"
            "777\n"
            "ZZFOLDDISC (C1)\n"
            "Optional Potentials | Emblem\n"
            "Affix Priority | Terra PEN | 110\n"
        )
        fold_row = {
            "main_key": "terra::FoldTest::X",
            "preset_code": None,
            "element": "terra",
            "slots": [
                {"char_id": 156, "en": "Nazuna", "cn": "小禾"},
                {"char_id": 130, "en": "Donna", "cn": "多娜"},
                {"char_id": 125, "en": "Canglan", "cn": "苍兰"},
            ],
            "team_name_preset": "",
            "team_name_infodoc": "Fold Test Block",
            "guide_ref": {"element": "terra", "block": "Fold Test Block"},
            "rotation": "",
        }
        with tempfile.TemporaryDirectory(prefix="stellasora_n14_") as tmp_fold:
            fold_dir = Path(tmp_fold)
            (fold_dir / "terra.json").write_text(
                json.dumps({"data": fold_infodoc}, ensure_ascii=False), encoding="utf-8"
            )
            with unittest.mock.patch.object(service, "_INFODOCS_DIR", fold_dir):
                out_fold = service.query_how_rows([fold_row], question="小禾攻略")
        check(
            "N14 渲染端到端：description/discs 纯数字行滤除,真实行与skill保留",
            "888" not in out_fold
            and "777" not in out_fold
            and "ZZFOLDDESC 段落一" in out_fold
            and "ZZFOLDDISC (共鸣1阶)" in out_fold
            and "技能升级优先度：1/10/1/10 (主技能 only)" in out_fold
            and "70级：地系穿透 110" in out_fold
            # fold 区块只有 Nazuna 段——多娜/苍兰缺段成员由队友并集 rescue（不丢弃任何成员）
            and "队友：多娜（支援位）、苍兰（支援位）" in out_fold,
            f"out_fold={out_fold!r}",
        )

        # N15: 详略策略（真实表，mock 问句）——1-2 角色仅问句第一个详述、
        # ≥3 全员详述、触发词全员详述、空角色集回退全详述、保序提取
        service.reload_team_table()
        rows_156_149 = service.find_team_rows([156, 149])

        # N15a 2 角色（问句第一个=小禾）：仅小禾详述，格芮/缇莉娅等进队友并集行
        mat_a = service.query_how_rows(rows_156_149, False, None, "小禾 格芮攻略")
        check(
            "N15a 详略策略2角色：2组头+仅首问询角色详述+未详述成员进并集行",
            len(re.findall(r"^\d+\. ", mat_a, flags=re.M)) == 2
            and "队友：格芮（支援位）、缇莉娅（支援位）" in mat_a
            and "队友：格芮（支援位）、科洛妮丝（新春）（支援位）" in mat_a
            and "小禾（主控位）" in mat_a,
            f"mat_a={mat_a!r}",
        )

        # N15b 反序（问句第一个=格芮）：格芮详述、小禾进并集行（保序决胜）
        mat_b = service.query_how_rows(rows_156_149, False, None, "格芮 小禾攻略")
        check(
            "N15b 详略策略反序：格芮详述+小禾进并集行",
            "格芮（支援位）" in mat_b
            and "队友：小禾（主控位）、缇莉娅（支援位）" in mat_b
            and "队友：小禾（主控位）、科洛妮丝（新春）（支援位）" in mat_b,
            f"mat_b={mat_b!r}",
        )

        # N15c 3 角色全详述：TMA 组 3 成员 × 3 等级 = 9 行纹章（同 N13）
        rows_tma15 = [row_tma]
        mat_c = service.query_how_rows(rows_tma15, False, None, "小禾 格芮 缇莉娅攻略")
        check(
            "N15c 详略策略3角色全员详述：emblem 行数==9",
            sum(1 for ln in mat_c.split("\n") if ln.startswith(("70级：", "80级：", "90级："))) == 9,
            f"mat_c={mat_c!r}",
        )

        # N15d 触发词（2 角色 + "完整"）→ 全员详述：emblem 行数==9
        mat_d15 = service.query_how_rows(rows_tma15, False, None, "小禾 格芮完整攻略")
        check(
            "N15d 详略策略触发词全员详述：emblem 行数==9",
            sum(1 for ln in mat_d15.split("\n") if ln.startswith(("70级：", "80级：", "90级："))) == 9,
            f"mat_d={mat_d15!r}",
        )

        # N15e 空角色集回退：不设限全详述，不崩且 emblem 行数==9
        mat_e = service.query_how_rows(rows_tma15, False, None, "攻略")
        check(
            "N15e 详略策略空角色集回退全详述：emblem 行数==9",
            sum(1 for ln in mat_e.split("\n") if ln.startswith(("70级：", "80级：", "90级："))) == 9,
            f"mat_e={mat_e!r}",
        )

        # N15f 保序提取单测：等长名先后由文本位置唯一决定
        check(
            "N15f find_character_names_ordered 保序（等长名文本位决定）",
            service.find_character_names_ordered("格芮 小禾攻略") == ["格芮", "小禾"]
            and service.find_character_names_ordered("小禾 格芮攻略") == ["小禾", "格芮"],
            f"a={service.find_character_names_ordered('格芮 小禾攻略')!r}, "
            f"b={service.find_character_names_ordered('小禾 格芮攻略')!r}",
        )

        # N15g 字段筛选触发词（详略词表扩展锁定）：字段问法（纹章）命中触发词表
        # →detail_ens=None→各成员字段齐全（prompt 4b-4e 的"各成员"语义）——
        # 双问询角色（小禾/格芮）的纹章行都进 material，不丢第二角色字段
        mat_g15 = service.query_how_rows(rows_tma15, False, None, "小禾 格芮 纹章")
        seg_by_member: dict = {}
        cur_member: str | None = None
        for ln in mat_g15.split("\n"):
            mh = re.match(r"^([^（]+)（(?:主控位|支援位)）$", ln)
            if mh:
                cur_member = mh.group(1)
                seg_by_member.setdefault(cur_member, [])
                continue
            if cur_member is not None:
                if ln.startswith("队友：") or re.match(r"^\d+\. ", ln):
                    cur_member = None
                else:
                    seg_by_member[cur_member].append(ln)
        check(
            "N15g 字段问法（纹章）双角色字段齐全：纹章行≥2组且两人各含 70级 行",
            mat_g15.count("纹章推荐：") >= 2
            and any(l.startswith("70级：") for l in seg_by_member.get("小禾", []))
            and any(l.startswith("70级：") for l in seg_by_member.get("格芮", [])),
            f"emblem_cnt={mat_g15.count('纹章推荐：')}, "
            f"members={ {k: len(v) for k, v in seg_by_member.items()} }",
        )
    finally:
        service.reload_team_table()


# ===== 汇总入口 =====

SECTIONS = {
    "B": ("字典单例与并发", run_singleton),
    "A": ("字典数据完整性", run_dict_data),
    "C": ("术语替换等价性", run_term_replace),
    "D": ("中文别名覆盖", run_overrides),
    "E": ("索引页抓取", run_fetcher_index),
    "F": ("how 表驱动查询", run_query_how_section),
    "G": ("直发端到端", run_direct_send),
    "H": ("输出格式", run_output_format),
    "I": ("非阻塞探针", run_nonblocking),
    "J": ("工具参数描述", run_tool_query_desc),
    "K": ("手动更新指令", run_manual_update),
    "L": ("定时自动同步", run_daily_sync_schedule),
    "M": ("统一队伍-槽位表构建器", run_section_m),
    "N": ("slot 查询服务", run_section_n),
}

# 执行顺序：B 最先（json.load 计数依赖首次触达），异步节统一在事件循环中跑
ORDER = ["B", "A", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N"]
ASYNC_SECTIONS = {"G", "H", "I", "K", "L"}


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
