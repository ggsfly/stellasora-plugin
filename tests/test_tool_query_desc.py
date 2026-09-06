#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：锁定 stellasora_how 的 query 参数描述约束。

方案 B 修复（群聊「猫眼攻略」回退 what bug）：
- stellasora_how 的 query 参数 description 必须要求 Planner 只传纯角色名/元素名，
  禁止附带「攻略」「配队」「秘纹」等后缀词，否则 dict.lookup_term(整句) 返回空，
  handle_how 返回「未在字典中找到」，Planner 退而求其次调用 stellasora_what。
- 同时必须保留原有的元素中文名（水/火/风/地/光/暗）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import plugin as plug  # noqa: E402

_COMPONENT_INFO_ATTR = "__maibot_component_info__"


def _get_tool_param_descriptions(tool_name: str) -> dict:
    """从 @Tool 装饰器元数据中提取 {参数名: 描述}。"""
    candidates = [
        getattr(plug.StellaSoraPlugin, name, None)
        for name in dir(plug.StellaSoraPlugin)
    ]
    for func in candidates:
        if func is None:
            continue
        info = getattr(func, _COMPONENT_INFO_ATTR, None)
        if info is None or getattr(info, "name", None) != tool_name:
            continue
        params = getattr(info, "parameters", None) or []
        return {p.name: p.description for p in params if hasattr(p, "name")}
    raise AssertionError(f"未找到 @Tool 元数据: {tool_name}")


def main() -> None:
    # 1. stellasora_how 的 query 描述包含“只传名字本身”的硬约束
    how_params = _get_tool_param_descriptions("stellasora_how")
    assert "query" in how_params, "stellasora_how 缺少 query 参数"
    desc = how_params["query"]
    assert "只传名字本身" in desc, f"query 描述缺少「只传名字本身」: {desc}"
    assert "攻略" in desc and "配队" in desc and "秘纹" in desc, (
        f"query 描述应明确点出后缀词示例（攻略/配队/秘纹）: {desc}"
    )
    # 2. 原有的元素中文名必须保留
    for elem_cn in ("水", "火", "风", "地", "光", "暗"):
        assert elem_cn in desc, f"query 描述丢失元素中文名「{elem_cn}」: {desc}"
    print("PASS: stellasora_how.query 描述含「只传名字本身」+ 后缀词禁令 + 元素名")

    # 3. stellasora_what 的 query 描述保持原样（只传名字，用于角色定位）
    what_params = _get_tool_param_descriptions("stellasora_what")
    assert "query" in what_params, "stellasora_what 缺少 query 参数"
    what_desc = what_params["query"]
    assert "角色" in what_desc and "装备" in what_desc, f"what.query 描述异常: {what_desc}"
    print("PASS: stellasora_what.query 描述未受影响")

    print("=== 全部通过 ===")


if __name__ == "__main__":
    main()