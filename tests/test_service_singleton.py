#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字典单例统一 + 索引/缓存预建 验证（plan Todo 2，【Metis 修订 #6/#9/#10/#11】+【双审 SH-6】）。

断言（对应 plan 验收标准）：
  1. dict.json 全进程只 json.load 一次（DictLookup 1 次 + TermReplacer 0 次，
     【Metis 修订 #10】monkeypatch json.load 而非 json.loads——两个模块用的都是 json.load）；
     names.json 额外 +1 次，故总调用数 == 2。
  2. 两个不同 cache_dir 调 _get_services 共享同一 lookup（缓存目录变化仅重建 fetcher）。
  3. _get_lookup() 返回共享实例；term_replace._replacer 即 service 持有实例（单一事实源注入）。
  4. 大小写回落路径 O(1)：lookup_term("NoSuchTermXYZ") 耗时 < 20ms（perf_counter）。
  5. 并发冒烟：10 线程同时首调 _get_services，json.load 计数不变（_init_lock 生效）。
  6. CLI 兼容：TermReplacer(data_dir) preloaded_dict=None 独立加载不崩。
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import dict_lookup as dict_lookup_module
import service
import term_replace as term_replace_module

failures: list = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"{name} -> {'PASS' if ok else 'FAIL'}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ---- monkeypatch json.load 计数（【Metis 修订 #10】patch json.load，两个模块共用）----
_orig_json_load = json.load
_counter = {"total": 0, "dict": 0}


def _counting_json_load(fp, *args, **kwargs):
    _counter["total"] += 1
    if "dict.json" in getattr(fp, "name", ""):
        _counter["dict"] += 1
    return _orig_json_load(fp, *args, **kwargs)


json.load = _counting_json_load

# ---- 用例 1+2：两个 cache_dir 首调/复用，dict.json 只解析一次 ----
tmp_a = Path(tempfile.mkdtemp(prefix="stellasora_singleton_a_"))
tmp_b = Path(tempfile.mkdtemp(prefix="stellasora_singleton_b_"))
t0 = time.perf_counter()
svc_a = service._get_services(tmp_a)
t_first = time.perf_counter() - t0
svc_b = service._get_services(tmp_b)
lookup_a, last_a, st_a, gd_a, replacer = svc_a
lookup_b, last_b, st_b, gd_b, _r2 = svc_b

check("1 dict.json 只解析一次", _counter["dict"] == 1,
      f"dict.json 解析 {_counter['dict']} 次（总 json.load {_counter['total']} 次：dict 1 + names 1）")
check("2 两个 cache_dir 共享同一 lookup", lookup_a is lookup_b)
check("2b 缓存目录更新", last_b == tmp_b and last_a == tmp_a)
check("2c cache_dir 变化仅重建 fetcher", st_a is not st_b and gd_a is not gd_b)
check("2d _instances 恰好 1 个键", list(service._instances.keys()) == [str(service._DATA_DIR)])
print(f"    首调（含 8.8MB 字典加载+索引+replacer 预建）耗时 {t_first*1000:.1f}ms")

# ---- 用例 3：单一事实源注入 ----
check("3a service 持有 replacer 与 term_replace._replacer 同一实例",
      term_replace_module._replacer is replacer)
check("3b _get_lookup 返回共享 lookup", service._get_lookup() is lookup_a)

# ---- 用例 4：大小写回落 O(1)（< 20ms，防 CI 抖动放宽至此）----
# 从真实字典取一个带大小写的角色名，构造非原样的等价写法走回落路径：
# 断言回落结果与精确命中完全一致（不硬编码具体角色名，避免字典更新后失配）
names_with_case = [n for n in lookup_a.get_character_names() if n != n.lower()]
probe_name = next(n for n in names_with_case if n.lower() not in lookup_a._name_index)
probe = probe_name.lower()
real = service.lookup_term(probe)
reference = service.lookup_term(probe_name)
missing = service.lookup_term("NoSuchTermXYZ")
t0 = time.perf_counter()
for _ in range(100):
    service.lookup_term("NoSuchTermXYZ")
elapsed_ms = (time.perf_counter() - t0) * 1000 / 100
check("4 大小写回落 miss < 20ms", elapsed_ms < 20, f"单次平均 {elapsed_ms:.3f}ms")
check("4b 大小写回落与精确命中一致", real == reference and "not_found" not in real,
      f"'{probe}' -> id={real.get('id')}")
check("4c 未收录术语返回 not_found", missing.get("not_found") is True)

# ---- 用例 5：角色名缓存 + 计数函数走共享 lookup ----
count1 = service.count_character_names("夏花 小禾")
count0 = service.count_character_names("与角色无关的普通句子")
check("5 角色名联合计数", count1 >= 2 and count0 == 0, f"命中 {count1} 个角色名 / 无关句 {count0}")

# 角色名缓存验证：第二次调用应命中 _character_names_cache（同一列表对象）
names_cached = lookup_a.get_character_names()
check("5b get_character_names 缓存命中", names_cached is lookup_a.get_character_names())

# ---- 用例 6：并发冒烟——10 线程同时首调（已有实例则锁内复用），计数不变 ----
counter_before = _counter["total"]
barrier = threading.Barrier(10)
results: list = []


def _thread_get():
    barrier.wait()
    results.append(service._get_services(tmp_a)[0])


threads = [threading.Thread(target=_thread_get) for _ in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("6 并发 10 线程 json.load 计数不变", _counter["total"] == counter_before,
      f"计数 {counter_before} -> {_counter['total']}")
check("6b 并发返回同一 lookup", all(r is lookup_a for r in results))

# ---- 用例 7：service replacer 真实替换（等价于 replace_terms 语义）----
replaced = replacer.replace("Skill DMG and Crit Rate")
check("7 replacer 英转中生效", "技能伤害" in replaced and "Crit Rate" not in replaced, replaced)

# ---- 用例 8：CLI 兼容——preloaded_dict=None 独立加载（failure 场景）----
json.load = _orig_json_load  # 恢复原始 json.load，避免 CLI 路径计数干扰
try:
    cli_replacer = term_replace_module.TermReplacer(service._DATA_DIR)
    cli_ok = bool(cli_replacer.mapping) and "技能伤害" in cli_replacer.replace("Skill DMG")
except FileNotFoundError:
    cli_ok = False
check("8 CLI 独立路径 preloaded_dict=None 可用", cli_ok)

# ---- 汇总 ----
print()
if failures:
    print(f"=== 字典单例验证 FAIL（{len(failures)} 项：{', '.join(failures)}）===")
    sys.exit(1)
print("=== 字典单例验证全部通过 ===")
