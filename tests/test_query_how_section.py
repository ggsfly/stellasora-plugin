#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 query_how 双页抓取(索引+详细)与角色段切分 (plan Todo 4)

测试内容：
  (a) 模拟详细文本(Chaton段 + Flora段)与索引文本("Chaton (Dark Ray)", "Rotation", "(Main Slot)")，
      query_how("Chaton", cache_dir, with_presets=False)：
      - material 含 "索引页" 区块
      - material 含 "Chaton (Dark Ray)" 且不含 "Flora" 段内容
      - material.find("索引页") > -1 and material.find("Chaton") > -1
      - material.find("索引页") < material.find("Chaton")
  (a2) 预设码排序子用例：monkeypatch fetch_presets 返回含 "预设码推荐" 与预设码，
       query_how("Chaton", cache_dir, with_presets=True)：
       - material.find("预设码") > -1 and material.find("索引页") > -1
       - material.find("预设码") < material.find("索引页")
  (b) 非角色词条(元素查询如 "火")不切分，返回整页 detailed
  (c) 角色名未在 infodoc 中出现时回退整页全文
  (d) fetch_infodoc_index 返回空串时 material 不含 "索引页" 区块且不报错
  (e) fetch_infodoc 返回含 "Error" 文本时，material 保留 "[抓取失败]"
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import service

failures: list = []


def check(name: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    print(f"{name} -> {status}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


cache_dir = Path("./.tmp_test_cache")
if cache_dir.exists():
    shutil.rmtree(cache_dir, ignore_errors=True)
cache_dir.mkdir(parents=True, exist_ok=True)

try:
    # 保存原始方法以便恢复或引用
    orig_fetch_infodoc = service.StelladbFetcher.fetch_infodoc
    orig_fetch_index = service.StelladbFetcher.fetch_infodoc_index
    orig_fetch_trekker = service.StelladbFetcher.fetch_trekker
    orig_fetch_presets = service.GoogleDocFetcher.fetch_presets

    # 离线保证：trekker 返回 Ignis 属性，避免测试依赖外部网络
    service.StelladbFetcher.fetch_trekker = lambda self, num_id: "Ignis character data with Ignis element"

    # 准备假数据
    mock_index_text = "Chaton (Dark Ray) | Rotation | (Main Slot) | Supp Slot"
    mock_detailed_text = (
        "Chaton (Dark Ray) | Main Slot | Skill Priority\n"
        "Chaton build details: Skill 1 > Skill 2\n"
        "Flora | 1st Supp Slot | Supp Build\n"
        "Flora build details: Supp only\n"
    )

    # ---- 用例 (a): 详细页切分 + 索引页前置 ----
    service.StelladbFetcher.fetch_infodoc_index = lambda self: mock_index_text
    service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_detailed_text

    material_a = service.query_how("Chaton", cache_dir, with_presets=False)
    has_index_block = "索引页" in material_a
    has_chaton_ray = "Chaton (Dark Ray)" in material_a
    no_flora_content = "Flora build details" not in material_a
    idx_index = material_a.find("索引页")
    idx_chaton = material_a.find("Chaton")

    check("(a) material 包含索引页区块", has_index_block)
    check("(a) material 包含 Chaton (Dark Ray)", has_chaton_ray)
    check("(a) material 不含 Flora 段内容", no_flora_content)
    check("(a) 索引页与角色名均存在 (> -1)", idx_index > -1 and idx_chaton > -1, f"idx_index={idx_index}, idx_chaton={idx_chaton}")
    check("(a) 严格保序: 索引页 < Chaton", idx_index < idx_chaton, f"{idx_index} < {idx_chaton}")

    # ---- 用例 (a2): 预设码排序子用例 ----
    mock_presets_text = "=== 预设码推荐 ===\nChaton\nMain Trekker\nPreset Code\nABCD1234EFGH5678IJKL\n"
    service.GoogleDocFetcher.fetch_presets = lambda self: mock_presets_text

    material_a2 = service.query_how("Chaton", cache_dir, with_presets=True)
    idx_preset = material_a2.find("预设码")
    idx_index_a2 = material_a2.find("索引页")

    check("(a2) 预设码与索引页均存在 (> -1)", idx_preset > -1 and idx_index_a2 > -1, f"idx_preset={idx_preset}, idx_index={idx_index_a2}")
    check("(a2) 严格保序: 预设码 < 索引页", idx_preset < idx_index_a2, f"{idx_preset} < {idx_index_a2}")

    # ---- 用例 (b): 非角色词条 (如 '火') 跳过切分，返回完整 detailed ----
    material_b = service.query_how("火", cache_dir, with_presets=False)
    check("(b) 非角色词条包含未切分的第二角色段 (未切分)", "Supp only" in material_b)
    check("(b) 非角色词条包含第一角色段", "build details" in material_b)

    # ---- 用例 (c): 角色名未在 infodoc 中找到时回退整页全文 ----
    mock_detailed_no_chaton = (
        "Flora | 1st Supp Slot | Supp Build\n"
        "Flora build details: Supp only\n"
    )
    service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_detailed_no_chaton
    material_c = service.query_how("Chaton", cache_dir, with_presets=False)
    check("(c) 角色未命中时回退整页全文，包含全部内容", "Supp only" in material_c)

    # ---- 用例 (d): fetch_infodoc_index 返回空串时跳过且不崩溃 ----
    service.StelladbFetcher.fetch_infodoc_index = lambda self: ""
    service.StelladbFetcher.fetch_infodoc = lambda self, element: mock_detailed_text
    material_d = service.query_how("Chaton", cache_dir, with_presets=False)
    check("(d) 索引页抓取为空时 material 省略索引页区块", "索引页" not in material_d)
    check("(d) 索引页抓取为空时不影响详细攻略输出", "build details" in material_d)

    # ---- 用例 (e): fetch_infodoc 返回含 Error 时保留 [抓取失败] ----
    service.StelladbFetcher.fetch_infodoc_index = lambda self: mock_index_text
    service.StelladbFetcher.fetch_infodoc = lambda self, element: "Error fetching infodoc."
    material_e = service.query_how("Chaton", cache_dir, with_presets=False)
    check("(e) infodoc 包含 Error 时输出 [抓取失败]", "[抓取失败]" in material_e)

    # 恢复 monkeypatch
    service.StelladbFetcher.fetch_infodoc = orig_fetch_infodoc
    service.StelladbFetcher.fetch_infodoc_index = orig_fetch_index
    service.StelladbFetcher.fetch_trekker = orig_fetch_trekker
    service.GoogleDocFetcher.fetch_presets = orig_fetch_presets

finally:
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)

if failures:
    print(f"\nFAILURES ({len(failures)}): {failures}")
    sys.exit(1)
else:
    print("\nALL TESTS PASSED: test_query_how_section")
    sys.exit(0)
