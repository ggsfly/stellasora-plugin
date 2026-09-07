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
  F how 双页切分    —— 索引页前置保序、角色段切分、回退整页、[抓取失败] 标注
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
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DICT_PATH = DATA_DIR / "dict.json"
NAMES_PATH = DATA_DIR / "names.json"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import service  # noqa: E402
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


def run_fetcher_index() -> None:
    print("--- E 索引页抓取 ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        fetcher = StelladbFetcher(Path(tmpdir))
        calls: list = []
        html = "<html><body><div>Team Rotation: Aqua Team</div></body></html>".encode("utf-8")

        def fake_urlopen(req, timeout=10):
            calls.append(req.full_url if hasattr(req, "full_url") else str(req))
            return _FakeHTTPResponse(html)

        original = urllib.request.urlopen
        try:
            urllib.request.urlopen = fake_urlopen
            res1 = fetcher.fetch_infodoc_index()
            check("E1 首抓返回非空且 URL 正确",
                  bool(res1) and "Team Rotation: Aqua Team" in res1
                  and calls == ["https://stelladb.pages.dev/infodoc"], str(calls))
            res2 = fetcher.fetch_infodoc_index()
            check("E2 二次命中缓存（仅 1 次请求）", res2 == res1 and len(calls) == 1)

            def error_urlopen(req, timeout=10):
                raise urllib.error.URLError("Network unreachable")

            urllib.request.urlopen = error_urlopen
            with tempfile.TemporaryDirectory() as err_tmp:
                check("E3 网络异常降级空串不崩溃", StelladbFetcher(Path(err_tmp)).fetch_infodoc_index() == "")
        finally:
            urllib.request.urlopen = original


# ===== 节 F：how 抓取（详细页队伍区块 + 索引页 Rotation） =====

def run_query_how_section() -> None:
    print("--- F 详细页队伍区块 + 索引页 Rotation ---")
    cache_dir = Path(tempfile.mkdtemp(prefix="stellasora_section_"))
    orig = {
        "infodoc": service.StelladbFetcher.fetch_infodoc,
        "index": service.StelladbFetcher.fetch_infodoc_index,
        "trekker": service.StelladbFetcher.fetch_trekker,
        "presets": service.GoogleDocFetcher.fetch_presets,
    }
    # 详细页 mock：按真实结构（区块锚点行带 ⏏；区块内首角色=主控，后续=支援；
    # 每角色段含 描述/技能优先度/秘纹/纹章完整四件套）
    mock_detailed = (
        "Chaton (Dark Ray) | ⏏ Back to Top ⏏\n"
        "Description | Skill Upgrade Priority\n"
        "Chaton (5★) | 1+/10/1/1+ (Main Skill >> Auto Attack = Ultimate)\n"
        "Chaton build details: Skill 1 > Skill 2\n"
        "Priority Potentials | Recommended Main Discs\n"
        "Cat Disc (C1) | Snow Disc (C6)\n"
        "Optional Potentials | Emblem\n"
        "Affix Priority | Fire PEN | 110 | Crit Rate | 15%\n"
        "Main Skill Lv. | +3 levels | Fire DMG | 20%\n"
        "Description | Skill Upgrade Priority\n"
        "Flora (4★) | 1/1/10/1 (Support Skill only)\n"
        "Flora is the irreplaceable support of this team\n"
        "Priority Potentials | Emblem\n"
        "Affix Priority | Charge Eff. | 40% | Support Skill Lv. | +3 levels\n"
        "Snowish Laru (Cannon) | ⏏ Back to Top ⏏\n"
        "Snowish build details: other team only\n"
    )
    # 索引页 mock：Rotation 按六元素列段排列，猫眼（火）在第 2 段。
    # 注：索引页槽位行（"X (Main Slot)" 等）在当前逻辑下不参与解析，mock 不含
    mock_index = (
        "Nazuna-Donna | Chaton (Dark Ray) | Wraith (Melee) | << Prev | Firefly WIP | Next >> | Otoha (Laser)\n"
        "Rotation | Rotation (WIP) | Rotation | Rotation | Rotation (WIP) | Rotation\n"
        "idk yet | Chaton Rotation content here | Wraith comps | Terra goals | Lux notes | Umbra tips\n"
    )
    try:
        # 离线保证：trekker 固定返回火属性文本，避免测试依赖外部网络
        service.StelladbFetcher.fetch_trekker = lambda self, num_id: "Ignis character data with Ignis element"
        service.StelladbFetcher.fetch_infodoc_index = lambda self: mock_index
        service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_detailed

        # 场景 1：问询主控位角色（猫眼），问"攻略"→ guide 模式（描述+技能+纹章）
        material = service.query_how("Chaton", cache_dir, with_presets=False, question="猫眼攻略")
        check("F1 主控问询：队名行+本角色标签+队友段，不含其他队",
              "配队1（猫眼 (暗黑射线)）" in material
              and "本角色猫眼（主控位）" in material and "队友紫槿（支援位）" in material
              and "Snowish build details" not in material)
        check("F2 资料全量（描述/技能/秘纹/纹章四类字段齐全，输出裁剪交由 prompt）",
              "描述：" in material and "技能升级优先度：" in material
              and "推荐主位秘纹：" in material and "纹章推荐：" in material
              and "70级：" in material and "80级：" in material)
        check("F3 Rotation 仅取猫眼所在列段",
              "猫眼 循环手法 content here" in material
              and "Wraith comps" not in material and "idk yet" not in material)

        # 场景 2：问询支援位角色（紫槿/Flora）→ 本角色=紫槿（支援位），主控=猫眼
        material_supp = service.query_how("Flora", cache_dir, with_presets=False, question="紫槿攻略")
        check("F4 支援位问询：本角色紫槿(支援位)，猫眼为队友(主控位)",
              "本角色紫槿（支援位）" in material_supp and "队友猫眼（主控位）" in material_supp
              and "irreplaceable support" in material_supp)

        # 场景 3：预设码保序（预设码在最前）
        service.GoogleDocFetcher.fetch_presets = lambda self: "=== 预设码推荐 ===\nChaton\nMain Trekker\nPreset Code\nABCD1234EFGH5678IJKL\n"
        material_p = service.query_how("Chaton", cache_dir, with_presets=True, question="猫眼攻略")
        idx_p, idx_t = material_p.find("预设码"), material_p.find("配队1")
        check("F5 预设码 < 配队正文 严格保序", idx_p > -1 and idx_t > -1 and idx_p < idx_t, f"{idx_p} < {idx_t}")

        # 场景 4：元素查询——不切分、无配队/Rotation 块
        material_elem = service.query_how("火", cache_dir, with_presets=False)
        check("F6 元素查询不切分（整页返回，无配队/Rotation块）",
              "Snowish build details" in material_elem
              and "配队1" not in material_elem and "输出手法" not in material_elem)

        # 场景 5：角色未命中 → 回退整页全文
        service.StelladbFetcher.fetch_infodoc = lambda self, element: "Flora build details: Supp only\n"
        check("F7 角色未命中时回退整页全文",
              "Supp only" in service.query_how("Chaton", cache_dir, with_presets=False))

        # 场景 6：索引页空串 → 无 Rotation 块，配队正文正常
        service.StelladbFetcher.fetch_infodoc_index = lambda self: ""
        service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_detailed
        material_no_idx = service.query_how("Chaton", cache_dir, with_presets=False, question="猫眼攻略")
        check("F8 索引页空串时配队正文正常、无Rotation块",
              "本角色猫眼（主控位）" in material_no_idx and "输出手法" not in material_no_idx)

        # 场景 7：infodoc Error
        service.StelladbFetcher.fetch_infodoc_index = lambda self: mock_index
        service.StelladbFetcher.fetch_infodoc = lambda self, element: "Error fetching infodoc."
        check("F9 infodoc 含 Error 时标注[抓取失败]",
              "[抓取失败]" in service.query_how("Chaton", cache_dir, with_presets=False))

        # 场景 8：Rotation 段数错位保护
        bad_index = "Chaton (Dark Ray)\nRotation\nonly_one_cell\n"
        service.StelladbFetcher.fetch_infodoc_index = lambda self: bad_index
        service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_detailed
        material_bad = service.query_how("Chaton", cache_dir, with_presets=False, question="猫眼攻略")
        check("F10 Rotation 段数错位时安全省略", "输出手法" not in material_bad)

        # 场景 9：队名角色 ≠ 主控（暗队 Otoha (Laser) 实例——主控是 Cosette）
        mock_umbra = (
            "Otoha (Laser) | ⏏ Back to Top ⏏\n"
            "Description | Skill Upgrade Priority\n"
            "Cosette (4★) | 1/10/1/1 (Main Skill only)\n"
            "Cosette occupies this team's Main slot as she provides high amounts of buffs\n"
            "Otoha (5★ Excl.) | 1/1/10/1 (Support Skill only)\n"
            "Otoha's laser build revolves on the Soul Rend effect\n"
        )
        service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_umbra
        material_otoha = service.query_how("Otoha", cache_dir, with_presets=False, question="乙叶攻略")
        check("F11 队名角色≠主控（Otoha队主控=珂赛特）",
              "本角色乙叶（支援位）" in material_otoha
              and "队友珂赛特（主控位）" in material_otoha)

        # 场景 10：多角色联合——2 角色同队 → 单队输出（find_teams_by_members 全链路）
        mock_aqua = (
            "Nazuna-Donna | ⏏ Back to Top ⏏\n"
            "Description | Skill Upgrade Priority\n"
            "Nazuna (5★) | 10/10/1/10 (Main Skill > Ultimate)\n"
            "Nazuna is the main dealer of this team\n"
            "Donna (4★) | 1/1/10/1 (Support Skill only)\n"
            "Donna supports with heals and buffs\n"
        )
        service.StelladbFetcher.fetch_trekker = lambda self, num_id: "Aqua character data with Aqua element"
        service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_aqua
        hit = service.find_teams_by_members(["小禾", "多娜"], cache_dir)
        check("F12 多角色队伍字典匹配（小禾+多娜 → 同队命中）",
              hit is not None and hit["team_name"] == "Nazuna-Donna"
              and set(hit["members"]) == {"Nazuna", "Donna"})

        # 场景 11：联合命中后 query_how 按多成员模式输出单队
        material_team = service.query_how("小禾", cache_dir, with_presets=False, question="小禾和多娜的配队", members=["Nazuna", "Donna"])
        check("F13 联合查询单队输出（无其他队+双本角色标签）",
              material_team.count("配队1（") == 1
              and "本角色小禾" in material_team and "本角色多娜" in material_team
              and "Snowish build details" not in material_team)

        # 场景 14：不同队角色组合 → 未命中
        miss = service.find_teams_by_members(["小禾", "Flora"], cache_dir)
        check("F14 不同队组合未命中", miss is None)

        # 场景 15：多角色别名识别与多 build 连续吞并（F15 回归用例）
        # 1. 动态别名 "春科" -> "科洛妮丝（新春）"
        service.configure_overrides(aliases={"春科": "科洛妮丝（新春）"})
        try:
            # find_character_names 识别别名并输出官方名
            extracted = service.find_character_names("春科 猫眼 谁好")
            check("F15-1 俗称别名提取为官方角色名",
                  "科洛妮丝（新春）" in extracted and "猫眼" in extracted,
                  f"提取结果: {extracted}")

            # 2. 多 build 连续吞并（同一角色多 build 区块连续排列，向后吞并合并成员且队伍不发生断裂）
            mock_multi_build = (
                "Chaton (Dark Ray) | ⏏ Back to Top ⏏\n"
                "Description | Skill Upgrade Priority\n"
                "Chaton (5★) | 10/10/1/10\n"
                "Chaton Dark Ray DPS details\n"
                "Springseek Coronis (5★) | 1/1/10/1\n"
                "Coronis NY support details\n"
                "Priority Potentials | Recommended Main Discs\n"
                "Disc A | Disc B\n"
                "Chaton (Hybrid) | ⏏ Back to Top ⏏\n"
                "Description | Skill Upgrade Priority\n"
                "Chaton (5★) | 1/10/10/1\n"
                "Chaton Hybrid alternate build\n"
                "Springseek Coronis (5★) | 1/1/10/1\n"
                "Coronis NY support details for hybrid\n"
            )
            # 验证底层 extract_team_blocks 对同角色多 build 的完整解析与成员保留
            blocks = service.extract_team_blocks(
                mock_multi_build, "Chaton", service._get_lookup().get_character_names()
            )
            check("F15-2 extract_team_blocks 对多build完整解析且各build队伍不断裂",
                  len(blocks) == 2
                  and blocks[0]["name"] == "Chaton (Dark Ray)"
                  and blocks[1]["name"] == "Chaton (Hybrid)"
                  and "Springseek Coronis" in blocks[0]["members"]
                  and "Springseek Coronis" in blocks[1]["members"])

            # 验证基于别名提取的角色在 find_teams_by_members 中精准同队命中
            service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_multi_build
            alias_hit = service.find_teams_by_members(extracted, cache_dir)
            check("F15-3 别名提取后多角色联合同队命中",
                  alias_hit is not None
                  and alias_hit["team_name"] == "Chaton (Dark Ray)"
                  and set(alias_hit["members"]) == {"Chaton", "Springseek Coronis"})

            # 验证 query_how 联合单队输出与双角色标识
            mat_team = service.query_how("猫眼", cache_dir, with_presets=False,
                                         question="春科和猫眼", members=["Chaton", "Springseek Coronis"])
            check("F15-4 联合查询双角色标签输出且排版完整",
                  "本角色猫眼" in mat_team and "本角色科洛妮丝（新春）" in mat_team)
        finally:
            service.configure_overrides(aliases={})
    finally:
        service.StelladbFetcher.fetch_infodoc = orig["infodoc"]
        service.StelladbFetcher.fetch_infodoc_index = orig["index"]
        service.StelladbFetcher.fetch_trekker = orig["trekker"]
        service.GoogleDocFetcher.fetch_presets = orig["presets"]
        shutil.rmtree(cache_dir, ignore_errors=True)


# ===== 节 G：直发端到端（核心用例精简版） =====

async def run_direct_send() -> None:
    print("--- G 直发端到端 ---")

    # G1 直发成功：stream 正确 + 人格注入 + model=utils 透传（原用例 1+20 合并）
    p, ctx = make_plugin()
    await p.on_load()
    llm, send = ctx.llm, ctx.send
    r = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g1")
    check("G1 直发成功且引导调 wait 禁 reply",
          "已直接发送" in r["content"] and "wait 工具" in r["content"] and len(send.sent) == 1
          and send.sent[0][0] == "stream_g1")
    check("G2 prompt 注入人格与表达风格",
          "你的名字是麦麦" in llm.calls[0]["prompt"] and "表达风格" in llm.calls[0]["prompt"])
    check("G3 SDK 透传 model=utils", llm.calls[0].get("model") == "utils")

    # G4-G6 失败分支：LLM 软失败/硬异常/stream 缺失 → 统一"未找到"
    ctx.llm = MockLLM(fail=True)
    n_sent = len(send.sent)
    r = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g4")
    check("G4 LLM 软失败→未找到且未发送",
          r["content"] == "未找到相关攻略。" and len(send.sent) == n_sent)
    ctx.llm = MockLLM(hard_fail=True)
    r = await p.handle_how(query="夏花", group_id="g1", stream_id="stream_g5")
    check("G5 LLM 硬异常→未找到", r["content"] == "未找到相关攻略。")
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

    # G9 联合查询（≥2 角色名）强制回传：无人格、客观体、系统包装、不直发
    n_sent = len(send.sent)
    ret = await p.handle_how(query="夏花", question="夏花 小禾 谁的纹章好", group_id="g1", stream_id="stream_g9")
    check("G9 联合查询回传（无人格+系统包装+不直发）",
          "你的名字是麦麦" not in llm.calls[-1]["prompt"]
          and "客观" in llm.calls[-1]["prompt"]
          and "系统说明" in str(ret.get("content", ""))
          and len(send.sent) == n_sent)

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
        # 新版关键词：资料结构约定 + 按问裁剪 + 多队去重
        missing = [kw for kw in ("本角色", "队友", "配队", "纹章", "去重", "秘纹") if kw not in prompt]
        check("H2 prompt 含 infodoc 输出规则关键词",
              not missing and "已直接发送" in res["content"], f"缺少 {missing}")
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
    for name in dir(plug.StellaSoraPlugin):
        func = getattr(plug.StellaSoraPlugin, name, None)
        info = getattr(func, attr, None) if func is not None else None
        if info is not None and getattr(info, "name", None):
            params = getattr(info, "parameters", None) or []
            tool_infos[info.name] = {
                param.name: param.description for param in params if hasattr(param, "name")
            }

    how_params = tool_infos.get("stellasora_how", {})
    desc = how_params.get("query", "")
    check("J1 how.query 含「只传名字本身」", "只传名字本身" in desc, desc)
    check("J2 how.query 点出后缀词禁令示例", all(w in desc for w in ("攻略", "配队", "秘纹")))
    check("J3 how.query 保留元素中文名",
          all(elem in desc for elem in ("水", "火", "风", "地", "光", "暗")))
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
    finally:
        plug.sync_offline_data = orig_sync

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

        def mock_urlopen(req, timeout=10):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            network_calls.append(url)
            return _FakeHTTPResponse(b"Online Fresh Content")

        orig_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = mock_urlopen

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
        finally:
            urllib.request.urlopen = orig_urlopen


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

        # 3. on_unload 优雅注销与取消
        task_ref = p._sync_task
        await p.on_unload()
        check("L9 on_unload 后 _sync_task 被置为 None", p._sync_task is None)
        check("L10 后台任务被成功取消并完成 (done)", task_ref is not None and task_ref.done())
    finally:
        plug.sync_offline_data = orig_sync
        p._calculate_delay_to_sync = orig_calc
        if p._sync_task and not p._sync_task.done():
            p._sync_task.cancel()
            await asyncio.gather(p._sync_task, return_exceptions=True)


# ===== 汇总入口 =====

SECTIONS = {
    "B": ("字典单例与并发", run_singleton),
    "A": ("字典数据完整性", run_dict_data),
    "C": ("术语替换等价性", run_term_replace),
    "D": ("中文别名覆盖", run_overrides),
    "E": ("索引页抓取", run_fetcher_index),
    "F": ("how 双页切分", run_query_how_section),
    "G": ("直发端到端", run_direct_send),
    "H": ("输出格式", run_output_format),
    "I": ("非阻塞探针", run_nonblocking),
    "J": ("工具参数描述", run_tool_query_desc),
    "K": ("手动更新指令", run_manual_update),
    "L": ("定时自动同步", run_daily_sync_schedule),
}

# 执行顺序：B 最先（json.load 计数依赖首次触达），异步节统一在事件循环中跑
ORDER = ["B", "A", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
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
