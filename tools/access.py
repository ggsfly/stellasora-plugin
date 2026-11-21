# -*- coding: utf-8 -*-
"""访问控制：黑白名单鉴权（自 tools/service.py 原样迁出，语义零变化）。

1.5.0 重写期间旧 service.py 将被删除，本模块承接 check_permission 供 plugin.py
的 _denied 与 Hook 前置过滤继续使用。判定规则不变：群聊按 group_id、私聊按
user_id，mode=off 全放行。
"""

from __future__ import annotations


def check_permission(
    mode: str,
    whitelist: list[str],
    blacklist: list[str],
    *,
    group_id: str = "",
    user_id: str = "",
) -> bool:
    """黑白名单鉴权。

    mode:
      - "whitelist": 仅 group_id/user_id 在白名单内才允许
      - "blacklist": group_id/user_id 在黑名单内则拒绝，其余放行
      - "off":       全放行（不限制模式）
    群聊按 group_id 判断；私聊按 user_id 判断。
    """
    mode = (mode or "off").strip().lower()
    if mode == "off":
        return True

    # 群聊有 group_id 就用 group_id；否则用 user_id（私聊）
    target = (group_id or user_id or "").strip()
    if not target:
        return mode == "blacklist"  # 无身份信息时：黑名单模式放行，白名单模式拒绝

    if mode == "whitelist":
        return target in whitelist
    if mode == "blacklist":
        return target not in blacklist
    return False
