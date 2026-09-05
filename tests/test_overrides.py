#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文别名映射（[overrides.aliases]）单元测试。

验证项：
  1. Pydantic 模型：AliasEntry 结构、OverridesConfig 默认值为 list[AliasEntry]。
  2. list[AliasEntry] → dict 转换逻辑（_apply_overrides_config 内部）。
  3. DictLookup 中文俗称 → 官方中文名解析（"春科" → "科洛妮丝（新春）", "土" → "地"）。
  4. DictLookup 多层链式别名。
  5. service.configure_overrides 动态生效与热重载。
  6. access_control.mode 为 Literal 下拉选项。
  7. TermReplacer 不再携带 custom_replacements（确认已移除）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args, get_origin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import service
from dict_lookup import DictLookup
from plugin import AccessControlConfig, AliasEntry, OverridesConfig, StellaSoraConfig
from term_replace import TermReplacer

DATA_DIR = ROOT / "data"

failures: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{name} -> {'PASS' if ok else 'FAIL'}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def test_config_models():
    """测试 Pydantic 配置模型结构与默认值。"""
    cfg = StellaSoraConfig()
    check(
        "Config: 默认包含 overrides 属性",
        hasattr(cfg, "overrides") and isinstance(cfg.overrides, OverridesConfig),
    )

    # aliases 应为 list[AliasEntry]
    check(
        "Config: overrides.aliases 是 list",
        isinstance(cfg.overrides.aliases, list),
    )
    check(
        "Config: 默认 aliases 非空且元素为 AliasEntry",
        len(cfg.overrides.aliases) > 0 and all(isinstance(a, AliasEntry) for a in cfg.overrides.aliases),
    )
    check(
        "Config: 默认第一条 alias='春科' official='科洛妮丝（新春）'",
        cfg.overrides.aliases[0].alias == "春科" and cfg.overrides.aliases[0].official == "科洛妮丝（新春）",
    )

    # 确认 replacements 字段已移除
    check(
        "Config: overrides 不再包含 replacements",
        not hasattr(cfg.overrides, "replacements"),
    )

    # access_control.mode 应为 Literal
    mode_field = AccessControlConfig.model_fields["mode"]
    origin = get_origin(mode_field.annotation)
    args = get_args(mode_field.annotation)
    check(
        "Config: access_control.mode 是 Literal['off','whitelist','blacklist']",
        str(origin) == "typing.Literal" and set(args) == {"off", "whitelist", "blacklist"},
        f"origin={origin} args={args}",
    )


def test_list_to_dict_conversion():
    """测试 list[AliasEntry] → dict 转换逻辑（_apply_overrides_config 内部）。"""
    cfg = StellaSoraConfig()
    alias_dict = {
        entry.alias: entry.official
        for entry in cfg.overrides.aliases
        if entry.alias and entry.official
    }
    check(
        "list→dict: 春科→科洛妮丝（新春）",
        alias_dict.get("春科") == "科洛妮丝（新春）",
        str(alias_dict),
    )
    check(
        "list→dict: 土→地",
        alias_dict.get("土") == "地",
        str(alias_dict),
    )


def test_dict_lookup_aliases():
    """测试 DictLookup 的中文俗称→官方名解析。"""
    lookup = DictLookup(DATA_DIR, custom_aliases={
        "春科": "科洛妮丝（新春）",
        "土": "地",
        "花玲": "花铃",
    })

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

    # "春科" 映射到 "科洛妮丝（新春）"
    res_ck = lookup.lookup_term("春科")
    check(
        "DictLookup: 春科 -> 科洛妮丝（新春）",
        bool(res_ck and res_ck.get("cn") == "科洛妮丝（新春）"),
        str(res_ck),
    )

    # 链式别名：泥土 -> 土 -> 地
    lookup.set_custom_aliases({"泥土": "土", "土": "地"})
    res_chain = lookup.lookup_term("泥土")
    check(
        "DictLookup: 链式别名 泥土 -> 土 -> 地",
        bool(res_chain and res_chain.get("cn") == "地"),
        str(res_chain),
    )


def test_service_configure_overrides():
    """测试 service.configure_overrides 动态配置中文别名。"""
    service.configure_overrides(aliases={"土": "地", "花玲": "花铃"})
    res = service.lookup_term("土")
    check(
        "service: lookup_term('土') 动态别名",
        bool(res and res.get("cn") == "地"),
        str(res),
    )


def test_term_replacer_no_custom_replacements():
    """确认 TermReplacer 不再接受 custom_replacements 参数。"""
    import inspect
    sig = inspect.signature(TermReplacer.__init__)
    check(
        "TermReplacer: __init__ 不含 custom_replacements 参数",
        "custom_replacements" not in sig.parameters,
        str(sig.parameters),
    )
    check(
        "TermReplacer: 无 set_custom_replacements 方法",
        not hasattr(TermReplacer, "set_custom_replacements"),
    )


def main():
    print("=" * 60)
    print("中文别名映射与 WebUI 适配测试 (test_overrides)")
    print("=" * 60)
    test_config_models()
    test_list_to_dict_conversion()
    test_dict_lookup_aliases()
    test_service_configure_overrides()
    test_term_replacer_no_custom_replacements()
    print("=" * 60)
    if failures:
        print(f"FAILED: {len(failures)} 项失败: {failures}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
