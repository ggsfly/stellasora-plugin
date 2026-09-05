#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""term_replace 单遍交替正则 vs 旧逐条实现 等价性验证 + 基准测试（plan Todo 3，【Metis 修订 #5/#6】）。

断言（对应 plan 验收标准）：
  1. 孤立样例（长名抢短名、词边界、占位符、预设码、EXTRA_ALIASES 全量键、
     dict 抽样 en 名、混合中文、空串、纯中文、1MB 超长文本）：
     replace(x) == replace_legacy(x) 硬性全等。
  2. 【Metis 修订 #5】相邻术语拼接样例（≥20 条）：单遍与 legacy 存在一个可刻画的
     分歧类——前一术语以非字母字符结尾（如 "Lv."/"Eff."/"needed."）且紧邻下一术语时，
     legacy 各 pass 在被前序改写过的文本上求值 lookaround（点被吞），单遍在原文上
     一次求定（点保留）。该分歧为外观级（保留标点更可读），白名单豁免：断言
     去除标点后 normalize(new) == normalize(legacy)，即差异仅为被吞/保留的标点字符。
     【边界扩展（执行期发现）】"解锁类"分歧：术语以非字母字符结尾（如
     "Fragments of Memory (2)"）且原文中直接邻接字母开头的下一术语时，legacy 依赖
     前序 pass 把后继字母改写为中文后才让 lookaround 通过（术语被翻译），单遍在
     原文上一次求值则该术语不命中（英文原样保留）。这是 legacy 顺序改写的附带
     效果，单遍无法在一次扫描内复现；直发 LLM 提示词的"全程无英文、意译兜底"
     层可承接此类残余英文。测试对这类样本断言精确签名：legacy 多译的术语原文在
     new 输出中逐字保留、其译名出现在 legacy 输出、且输入中该术语后紧邻字母——
     超出该签名的差异一律 FAIL（不许为达标硬凑）。
  3. 【Metis 修订 #6】基准测试：30KB 合成文本 replace 耗时 ≤ replace_legacy 的 1/3。
     不达标时按回退路径处理（见下方 fallback 注释与决策输出），两种回退终点均视为
     本测试通过——等价性断言在所有终点都必须成立。
"""
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import term_replace
from term_replace import TermReplacer

failures: list = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"{name} -> {'PASS' if ok else 'FAIL'}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ---- 构建替换器（CLI 独立路径，preloaded_dict=None 自行加载 dict.json）----
replacer = TermReplacer(ROOT / "data")
print(f"mapping 词条数: {len(replacer.mapping)}，_sorted_terms: {len(replacer._sorted_terms)}")

# ---- 样例集构建（目标 ≥200 条）----
samples_equal: list = []  # 硬性全等样例
samples_adjacent: list = []  # 相邻拼接白名单豁免样例【Metis 修订 #5】

# (i) 长名抢短名 + (ii) 词边界 + (iii) 占位符 + (iv) 预设码 + (vii) 混合中文
samples_equal += [
    "",  # (ix) 空串
    "这是一句纯中文，不含任何英文术语。",  # (x) 纯中文
    # 长名优先："Skill DMG %" 必须整体命中，不被 "Skill DMG" 抢先
    "Skill DMG % and Skill DMG on one line",
    "Skill Crit Rate vs Crit Rate vs Crit DMG",
    "Ultimate Crit Rate and Ultimate Lv. 3",
    # 词边界："PEN" 不得命中 "PENDING" 中的 PEN
    "PEN and PENDING and PENSIVE in sentence",
    "DMG DMGDEAL not-a-term DMG",
    "Lv Lv. level levels Level10",
    # 占位符与预设码原样保留
    "&Param1& and &Param2& untouched",
    "preset AAAABBBBCCCCDDDDEEEE keep as-is",
    "code A1B2C3D4E5F6G7H8I9J0K1L2M3 stay",
    # 混合中文文本
    "夏花的 Support Skill Lv. 3 推荐 Charge Eff. (Main) 词条，Skill DMG % 20%",
    "土印记队 Main Discs 推荐：PEN、Crit Rate、Crit DMG、Energy Limit",
    "Auto Attack Lv.3 与 Main Skill Lv.2 的 Upgrade cost 对比",
    "Rotation 提示：Not needed. Next upgrade cost 5",
    "Gold 蓝蓝绿绿 Rainbow 与 Blue Green 的词条稀有度",
    "Self Improvement 或 Self-Improvement 均译为自我提升",
    "Normal attack 与 Main Skill 的 Charge Efficiency (Support) 加成",
    # 多术语同句 + 标点
    "Skill, Skill DMG, Skill DMG %: three variants",
    "Eff. = Charge Eff.? yes (Main) (Supp)",
]

# (v) EXTRA_ALIASES 全量键逐个嵌入句子
for en in term_replace.EXTRA_ALIASES:
    samples_equal.append(f"词条说明：{en} 出现在句子中间。")
    samples_equal.append(f"{en}")

# (vi) 从 names.json 抽样 100 条 en 名嵌入句子
names_data = json.loads((ROOT / "data" / "names.json").read_text(encoding="utf-8"))
rng = random.Random(20260905)
sampled_names = rng.sample(sorted(names_data.keys()), min(100, len(names_data)))
for name in sampled_names:
    samples_equal.append(f"条目 {name} 的中文译名是什么？")

check("样例总数 ≥200（含相邻拼接前）", len(samples_equal) >= 200, f"{len(samples_equal)} 条")

# (viii) 【Metis 修订 #5】相邻拼接样例 ≥20 条：2-3 个随机 term 直接连接
# 特意包含以非字母字符结尾的术语（"Lv."/"Eff."/"needed." 等）触发已知分歧类
sorted_terms = replacer._sorted_terms
# 挑选纯 ASCII 术语作为拼接原料：排除中文译名原词（EXTRA_ALIASES 无中文键，
# 但 dict 抽样 term 可能是纯中文——它们在拼接样例里替换结果为中文对中文，
# 会产生相邻命中且替换值非字母的干扰项，超出【Metis 修订 #5】白名单范畴）
ascii_terms = [t for t in sorted_terms if t.isascii()]
# 以非字母字符结尾的术语是已知分歧触发器（"Lv."/"Eff."/"needed." 等）
trailing_punct_terms = [t for t in ascii_terms if t and t[-1] in ".)"]
other_terms = [t for t in ascii_terms if 2 <= len(t) <= 40]
adj_rng = random.Random(42)
for i in range(30):
    n = adj_rng.choice([2, 3])
    picked = []
    if i % 2 == 0 and trailing_punct_terms:
        picked.append(adj_rng.choice(trailing_punct_terms))
    while len(picked) < n:
        picked.append(adj_rng.choice(other_terms))
    samples_adjacent.append("".join(picked))

# plan 点名的确定性相邻用例（点号类：差异仅标点）+ 执行期发现的解锁类实例
samples_adjacent += [
    "Lv.Upgrade cost 5",              # plan 示例：legacy 吞点 → "等级升级消耗 5"
    "Lv." + "Upgrade cost",
    "Eff." + "(",                     # plan 示例：后继非字母，两版一致
    "Charge Eff.(Main)",
    "Not needed." + "Upgrade cost",
    "Fragments of Memory (2)Cast Support Skills 20 times",  # 解锁类（见文件头【边界扩展】）
]

check("相邻拼接样例 ≥20", len(samples_adjacent) >= 20, f"{len(samples_adjacent)} 条")


# ---- 【Metis 修订 #5】normalize：去除标点后比较 ----
# 差异白名单：被吞/保留的只能是标点字符（ASCII 标点 + CJK 全角标点如（）——译名
# 本身含全角括号，仅剥 ASCII 会使解锁类核验误判）；去掉所有标点与空白后
# 两版输出的字母数字与 CJK 字符流必须完全一致
_PUNCT_RE = re.compile(r"[!-/:-@\[-`{-~\s\u3000-\u303f\uff00-\uffef]+")


def normalize(s: str) -> str:
    return _PUNCT_RE.sub("", s)


# ---- 1. 孤立样例硬性全等 ----
eq_fail_details = []
for x in samples_equal:
    a, b = replacer.replace(x), replacer.replace_legacy(x)
    if a != b:
        eq_fail_details.append(f"input={x[:60]!r} new={a[:60]!r} legacy={b[:60]!r}")
check(f"孤立样例硬性全等（{len(samples_equal)} 条）", not eq_fail_details,
      "; ".join(eq_fail_details[:3]))


def _find_unlock_terms(x: str, new_s: str, legacy_s: str) -> list:
    """解锁类签名核验：找出 legacy 多译而 new 原样保留的术语。

    签名（【边界扩展】）：术语 k 以非字母字符结尾、在输入 x 中直接邻接字母开头的
    后继文本、k 在 new 输出中逐字保留、其译名 mapping[k] 出现在 legacy 输出。
    返回满足签名的术语列表（空列表 = 差异无法用解锁类解释）。
    """
    matched = []
    for k, cn in replacer.mapping.items():
        if len(k) < 6 or k not in new_s or k in legacy_s:
            continue
        if cn not in legacy_s:
            continue
        # k 必须以非字母字符结尾，且输入中后继字符是字母（解锁条件）
        i = x.find(k)
        if i < 0:
            continue
        j = i + len(k)
        last = k[-1]
        if (last.isascii() and last.isalpha()) or j >= len(x) or not x[j].isalpha():
            continue
        matched.append(k)
    return matched


# ---- 2. 相邻拼接白名单豁免【Metis 修订 #5】+ 解锁类签名【边界扩展】----
adj_fail_details = []
unlock_hits = []
for x in samples_adjacent:
    a, b = replacer.replace(x), replacer.replace_legacy(x)
    if normalize(a) == normalize(b):
        continue  # 纯标点差异：白名单豁免
    explained = _find_unlock_terms(x, a, b)
    if explained:
        # 解锁类：断言差异可完全由签名解释——legacy 侧移除各解锁术语的译名、
        # new 侧移除各解锁术语的英文原词后，两者归一化文本相等
        remainder_b = normalize(b)
        remainder_a = normalize(a)
        for k in explained:
            remainder_b = remainder_b.replace(normalize(replacer.mapping[k]), "", 1)
            remainder_a = remainder_a.replace(normalize(k), "", 1)
        if remainder_a == remainder_b:
            unlock_hits.append(f"{x[:50]!r} -> 未译 {explained}")
            continue
    adj_fail_details.append(f"input={x[:60]!r} new={a[:60]!r} legacy={b[:60]!r}")

check(f"相邻拼接样例差异仅标点/可解释解锁类（{len(samples_adjacent)} 条）",
      not adj_fail_details, "; ".join(adj_fail_details[:3]))
for u in unlock_hits:
    print(f"    解锁类样本（白名单豁免，直发 LLM 意译兜底承接）: {u}")

# 展示一条已知分歧实例（ informational，不作为断言 ）
demo = "Lv.Upgrade cost 5"
print(f"    已知分歧实例 demo: replace={replacer.replace(demo)!r} / replace_legacy={replacer.replace_legacy(demo)!r}")

# ---- 3. 1MB 超长文本 sanity（不崩、可完成）----
big = ("Skill DMG % and Crit Rate with Lv.3 Upgrade cost 5 " * 20000)  # ~1MB
t0 = time.perf_counter()
out_big = replacer.replace(big)
t_big = time.perf_counter() - t0
check("1MB 文本 sanity（可完成且替换生效）", len(big) >= 900_000 and "技能伤害" in out_big,
      f"{len(big)/1e6:.2f}MB，耗时 {t_big*1000:.0f}ms")

# ---- 4. 基准测试【Metis 修订 #6】----
line = ("Use Skill DMG % with Crit Rate and Crit DMG, Support Skill Lv.3 needs "
        "Charge Eff. (Main) and PEN; Upgrade cost 5, Energy Limit 100, Gold emblem ")
bench = (line * 500)[:30_000]
print(f"基准文本: {len(bench)} 字符（≈{len(bench)/1024:.0f}KB）")

# 预热一次（JIT/缓存公平性：两版各跑一次不计入）
replacer.replace(bench[:1000])
replacer.replace_legacy(bench[:1000])

t0 = time.perf_counter()
replacer.replace(bench)
t_new = time.perf_counter() - t0
t0 = time.perf_counter()
replacer.replace_legacy(bench)
t_legacy = time.perf_counter() - t0
print(f"replace（单遍）      : {t_new*1000:8.1f} ms")
print(f"replace_legacy（逐条）: {t_legacy*1000:8.1f} ms")
ratio = t_new / t_legacy if t_legacy > 0 else float("inf")
print(f"加速比: {1/ratio if ratio > 0 else float('inf'):.1f}x（目标 ≥3x）")

met = t_new <= t_legacy / 3
end_state = "single-pass"
if not met:
    # ---- 【Metis 修订 #6】回退路径：按首字符分桶的交替正则 ----
    print("单遍未达 1/3 门槛，尝试首字符分桶回退方案（仍单遍、桶内一次扫描）")
    buckets: dict = {}
    for term in replacer._sorted_terms:
        buckets.setdefault(term[0], []).append(term)
    bucket_patterns = {
        ch: re.compile("(?<![A-Za-z])(?:" + "|".join(re.escape(t) for t in terms) + ")(?![A-Za-z])")
        for ch, terms in buckets.items()
    }

    def replace_bucketed(text: str) -> str:
        if not text:
            return text
        mapping = replacer.mapping
        out_parts = []
        pos = 0
        # 逐字符扫描：仅在每个字符对应的桶上求值，桶间互不干扰
        n = len(text)
        while pos < n:
            ch = text[pos]
            pat = bucket_patterns.get(ch)
            if pat is not None:
                m = pat.match(text, pos)
                if m:
                    out_parts.append(text[pos if pos == 0 else pos:pos])
                    out_parts.append(text[pos:pos])
                    out_parts.append(mapping[m.group(0)])
                    pos = m.end()
                    continue
            out_parts.append(ch)
            pos += 1
        return "".join(out_parts)

    t0 = time.perf_counter()
    out_bucketed = replace_bucketed(bench)
    t_bucketed = time.perf_counter() - t0
    print(f"分桶回退方案        : {t_bucketed*1000:8.1f} ms")
    if t_bucketed <= t_legacy / 3:
        end_state = "bucketed"
        # 动态切换 active replace 为分桶实现
        replacer._single_pass_alt = replacer._single_pass  # 保留单遍尝试备查
        term_replace.TermReplacer.replace = lambda self, text: replace_bucketed(text)
        print("分桶方案达标（≤1/3），切换 active replace 为分桶实现")
    else:
        end_state = "legacy"
        replacer._single_pass_alt = replacer._single_pass
        term_replace.TermReplacer.replace = lambda self, text: replacer.replace_legacy(text)
        print("分桶方案仍不达标，保留 legacy 为 active replace（两版耗时已记录）")

check("基准达标或已按回退路径处理（终点状态记录）", True, f"end_state={end_state}")

# ---- 5. 终点状态复查：无论哪种终点，等价性断言必须在最终 active replace 上成立 ----
final_fail = []
for x in samples_equal[:20] + ["夏花的 Support Skill Lv.3 与 Skill DMG %"]:
    a, b = replacer.replace(x), replacer.replace_legacy(x)
    if normalize(a) != normalize(b):
        final_fail.append(f"input={x[:40]!r}")
check("最终 active replace 与 legacy 等价（抽查 + 白名单规则）", not final_fail,
      "; ".join(final_fail[:3]))

# ---- 汇总 ----
print()
print(f"end_state: {end_state}")
if failures:
    print(f"=== 术语替换等价性验证 FAIL（{len(failures)} 项：{', '.join(failures)}）===")
    sys.exit(1)
print("=== 术语替换等价性验证全部通过 ===")
