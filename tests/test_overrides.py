#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自定义覆盖与别名（[overrides]）单元测试。

验证项：
  1. DictLookup 别名解析（"土" -> "地", "花玲" -> "花铃"）。
  2. DictLookup 多层链式别名与大小写回落。
  3. TermReplacer 自定义替换（"Finale Echoing" -> "终焉绝响"）。
  4. TermReplacer 优先级高于内置字典映射。
  5. service.configure_overrides 动态生效与热重载。
  6. StellaSoraConfig 与 OverridesConfig Pydantic 模型校验。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import service
from dict_lookup import DictLookup
from plugin import OverridesConfig, StellaSoraConfig
from term_replace import TermReplacer

DATA_DIR = ROOT / "data"

failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{name} -> {'PASS' if ok else 'FAIL'}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def test_dict_lookup_aliases():
    """测试 DictLookup 的自定义别名解析。"""
    lookup = DictLookup(DATA_DIR, custom_aliases={"土": "地", "花玲": "花铃", "Cat": "Amber"})
    
    # "土" 映射到 "地" -> UIText.T_Element_Attr_3.1
    res_tu = lookup.lookup_term("土")
    check(
        "DictLookup: 土 -> 地",
        bool(res_tu and res_tu.get("cn") == "地" and res_tu.get("en") == "Terra"),
        str(res_tu),
    )

    # "花玲" 映射到 "花铃" -> Character.157.1
    res_hl = lookup.lookup_term("花玲")
    check(
        "DictLookup: 花玲 -> 花铃",
        bool(res_hl and res_hl.get("cn") == "花铃" and res_hl.get("cat") == "Character"),
        str(res_hl),
    )

    # 大小写不敏感
    res_cat = lookup.lookup_term("cat")
    check(
        "DictLookup: cat (lowercase) -> Amber",
        bool(res_cat and res_cat.get("en") == "Amber"),
        str(res_cat),
    )

    # 运行时更新 set_custom_aliases
    lookup.set_custom_aliases({"泥土": "土", "土": "地"})
    res_chain = lookup.lookup_term("泥土")
    check(
        "DictLookup: 链式别名 泥土 -> 土 -> 地",
        bool(res_chain and res_chain.get("cn") == "地"),
        str(res_chain),
    )


def test_term_replacer_custom():
    """测试 TermReplacer 自定义替换。"""
    replacer = TermReplacer(
        DATA_DIR,
        custom_replacements={
            "Finale Echoing": "终焉绝响",
            "Custom Spell": "自定义法术",
            "心钥": "心链",
        },
    )

    raw_text = "Trekker uses Finale Echoing and Custom Spell with 心钥."
    replaced = replacer.replace(raw_text)
    check(
        "TermReplacer: Finale Echoing & Custom Spell",
        "终焉绝响" in replaced and "自定义法术" in replaced and "心链" in replaced,
        replaced,
    )

    # 动态 set_custom_replacements
    replacer.set_custom_replacements({"Another Echo": "另一回响"})
    res2 = replacer.replace("Uses Another Echo now.")
    check(
        "TermReplacer: set_custom_replacements 动态更新",
        "另一回响" in res2 and "Another Echo" not in res2,
        res2,
    )


def test_service_configure_overrides():
    """测试 service.configure_overrides 动态配置。"""
    service.configure_overrides(
        aliases={"土": "地", "花玲": "花铃"},
        replacements={"Finale Echoing": "终焉绝响"},
    )
    res = service.lookup_term("土")
    check(
        "service: lookup_term('土') 动态别名",
        bool(res and res.get("cn") == "地"),
        str(res),
    )


def test_config_models():
    """测试 Pydantic 配置模型结构与默认值。"""
    cfg = StellaSoraConfig()
    check(
        "Config: 默认包含 overrides 属性",
        hasattr(cfg, "overrides") and isinstance(cfg.overrides, OverridesConfig),
    )
    check(
        "Config: overrides 默认包含 aliases 与 replacements",
        isinstance(cfg.overrides.aliases, dict) and isinstance(cfg.overrides.replacements, dict),
    )


def main():
    print("=" * 60)
    print("自定义覆盖与别名测试 (test_overrides)")
    print("=" * 60)
    test_dict_lookup_aliases()
    test_term_replacer_custom()
    test_service_configure_overrides()
    test_config_models()
    print("=" * 60)
    if failures:
        print(f"FAILED: {len(failures)} 项失败: {failures}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
