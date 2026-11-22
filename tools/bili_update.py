# -*- coding: utf-8 -*-
"""猫猫球（B 站）清单维护脚本：核验/增量扫描新动态 → 输出候选报告（不改清单）。

运行时不扫列表（查询只按内置清单的 opus_id 精确取详情）；本脚本是**维护期**
工具，人工跑、人工确认、人工更新 assets/bili_guides/manifest.json 并提交。

用法::

    python tools/bili_update.py --ids 1253428983540744198,1270198910947688484
                                # 核验给定 opus_id，输出可直接粘贴的清单片段
    python tools/bili_update.py                 # 增量扫描，打印候选报告
    python tools/bili_update.py --dry-run       # 同上，但不推进 checkpoint
    python tools/bili_update.py --full          # 忽略 checkpoint，扫最近 N 页
    python tools/bili_update.py --pages 3       # 最多扫 3 页（默认 2）

列表接口：`opus/feed/space`（匿名可用，已实机验证）。该接口只给 opus_id 与正文
摘要，**没有标题/图片数**——候选是否需要入库由 `--ids` 逐条取详情确认。

纪律：
- 只输出候选报告（opus_id/摘要），**不下载图、不改 manifest、不自动 push**；
- checkpoint 记 UID + 最近已扫动态 ID 锚点；找不到可靠边界就报错并且**不推进**，
  避免漏扫；
- 有未人工确认的候选时不推进 checkpoint（先确认入库，下次自然以新入库项为界）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import argparse
import json
import sys
import time

from bili_guides import (  # noqa: E402
    BiliGuideError,
    _UA,
    _normalize_role,
    fetch_opus_detail,
    load_manifest,
)
from net_common import HttpBytesError as _HttpBytesError, read_http_bytes as _read_http_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_PATH = ROOT / "tools" / "bili_checkpoint.json"
AUTHOR_UID = "12716510"

_SPACE_FEED_HOST = "api.bilibili.com"
_SPACE_FEED_API = f"https://{_SPACE_FEED_HOST}/x/polymer/web-dynamic/v1/opus/feed/space"
_MAX_FEED_BYTES = 4 * 1024 * 1024


def _fetch_feed_page(
    page: int = 1,
    offset: str = "",
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    """取一页空间动态（匿名可读）。

    返回 {"items": [...], "offset": 下一页游标, "has_more": bool}；结构不符抛
    BiliGuideError。条目形如 {opus_id, content, jump_url, pub_time}——**没有标题
    与图片数**，需要详情才能判断内容。
    """
    query = f"?host_mid={AUTHOR_UID}&page={page}&offset={offset}&type=all&web_location=333.1387"
    try:
        body = _read_http_bytes(
            f"{_SPACE_FEED_API}{query}",
            allowed_hosts=(_SPACE_FEED_HOST,),
            headers={
                "User-Agent": _UA,
                "Referer": f"https://space.bilibili.com/{AUTHOR_UID}/dynamic",
            },
            proxy_url=proxy_url,
            timeout=20,
            max_bytes=_MAX_FEED_BYTES,
        )
    except _HttpBytesError as exc:
        raise BiliGuideError(f"动态列表请求失败: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BiliGuideError(f"动态列表不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BiliGuideError("动态列表响应不是对象")
    if payload.get("code") != 0:
        raise BiliGuideError(
            f"动态列表业务码非 0: code={payload.get('code')!r} message={payload.get('message')!r}"
        )
    data = payload.get("data") or {}
    items = data.get("items")
    if not isinstance(items, list):
        raise BiliGuideError("动态列表缺 data.items")
    return {
        "items": items,
        "offset": str(data.get("offset") or ""),
        "has_more": bool(data.get("has_more")),
    }


def _item_to_candidate(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """单条 feed 项 → 候选信息（只留 opus_id 与摘要文本）。"""
    if not isinstance(item, dict):
        return None
    opus_id = str(item.get("opus_id") or "").strip()
    if not opus_id.isdigit():
        return None
    return {"opus_id": opus_id, "content": str(item.get("content") or "").strip()}


def _guess_role(title: str, manifest: Dict[str, Any]) -> str:
    """按标题猜角色名（仅用于报告，人工确认时参考）。猜不出返回空串。

    命中 role_aliases 的 B 站叫法会归一到我方规范名（否则贴进清单会与 roles
    快照对不上）。
    """
    canonical = {_normalize_role(str(r["name"])): str(r["name"]) for r in manifest.get("roles") or []}
    alias_map = {
        _normalize_role(str(k)): _normalize_role(str(v))
        for k, v in (manifest.get("role_aliases") or {}).items()
    }
    known = set(canonical) | set(alias_map)
    norm_title = _normalize_role(title)
    # 长名优先，避免"科洛妮丝"抢占"科洛妮丝(新春)"
    for name in sorted(known, key=len, reverse=True):
        if name in norm_title:
            resolved = alias_map.get(name, name)
            return canonical.get(resolved, resolved)
    return ""


def _is_suspicious(title: str, role: str) -> bool:
    """标题像攻略但没匹配到任何官方角色名 → 疑似其他游戏的内容，标注排除。

    实例：曾误抓「像素深渊 角色分位榜&配队组件选择一图流」——标题带"一图流"
    但内容是别的游戏。严格角色名匹配天然把它排除，这里的标注只是防止人工复核
    时又把它加回去。
    """
    if role:
        return False
    norm = _normalize_role(title)
    return any(kw in norm for kw in ("一图流", "攻略", "配队", "阵容"))


def load_checkpoint() -> Dict[str, Any]:
    if not CHECKPOINT_PATH.is_file():
        return {}
    try:
        data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BiliGuideError(f"checkpoint 读取失败: {CHECKPOINT_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise BiliGuideError(f"checkpoint 结构不符（应为对象）: {CHECKPOINT_PATH}")
    return data


def save_checkpoint(state: Dict[str, Any]) -> None:
    from net_common import atomic_write

    atomic_write(CHECKPOINT_PATH, json.dumps(state, ensure_ascii=False, indent=1))


def scan(
    known_opus_ids: set,
    anchor: str,
    max_pages: int,
    full: bool,
    proxy_url: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], bool]:
    """翻页扫描，撞到锚点后**再多翻一页确认重叠区**（防置顶/同秒/乱序漏扫）。

    返回 (候选列表, 新 checkpoint|None, 边界是否可信)。边界不可信时 checkpoint
    为 None，由调用方报错且不推进。
    """
    candidates: List[Dict[str, Any]] = []
    newest: Optional[Dict[str, Any]] = None
    seen_ids: set = set()
    hit_anchor = False
    boundary_confirmed = False
    offset = ""

    for page in range(1, max(1, max_pages) + 1):
        page_data = _fetch_feed_page(page=page, offset=offset, proxy_url=proxy_url)
        items = page_data["items"]
        if not items:
            boundary_confirmed = True
            break
        for item in items:
            cand = _item_to_candidate(item)
            if cand is None:
                continue
            if newest is None:
                newest = cand
            if anchor and cand["opus_id"] == anchor:
                hit_anchor = True
                continue
            if cand["opus_id"] in seen_ids or cand["opus_id"] in known_opus_ids:
                # 已知条目：已进入历史区（重叠确认）
                if hit_anchor:
                    boundary_confirmed = True
                continue
            seen_ids.add(cand["opus_id"])
            candidates.append(cand)
        if not page_data["has_more"]:
            boundary_confirmed = True
            break
        if hit_anchor and boundary_confirmed:
            break
        offset = page_data["offset"]
        if not offset:
            break

    if full:
        boundary_ok = boundary_confirmed
        new_state = (
            {
                "uid": AUTHOR_UID,
                "anchor_opus_id": newest["opus_id"] if newest else "",
                "scanned_at": int(time.time()),
                "pages": max_pages,
            }
            if boundary_ok and newest
            else None
        )
        return candidates, new_state, boundary_ok

    if anchor:
        boundary_ok = hit_anchor or boundary_confirmed
    else:
        boundary_ok = boundary_confirmed
    new_state = None
    if boundary_ok and newest is not None:
        new_state = {
            "uid": AUTHOR_UID,
            "anchor_opus_id": newest["opus_id"],
            "scanned_at": int(time.time()),
        }
    return candidates, new_state, boundary_ok


def verify_ids(ids: List[str], manifest: Dict[str, Any], proxy_url: Optional[str] = None) -> int:
    """按 opus_id 逐条核验（走详情接口），输出可粘贴的清单片段。"""
    ok = 0
    lines: List[str] = []
    for opus_id in ids:
        try:
            detail = fetch_opus_detail(opus_id, proxy_url=proxy_url)
        except BiliGuideError as exc:
            print(f"[FAIL] opus_id={opus_id}: {exc}")
            continue
        title = detail["title"]
        role = _guess_role(title, manifest)
        element = ""
        for entry in manifest.get("roles") or []:
            if _normalize_role(str(entry.get("name"))) == _normalize_role(role):
                element = str(entry.get("element") or "")
                break
        ok += 1
        mark = "✓" if role else "?"
        print(f"{mark} opus_id={opus_id} 图{len(detail['image_urls'])}张 标题={title!r} 角色={role or '(未识别)'}")
        if role:
            lines.append(
                f'    {{"opus_id": "{opus_id}", "title": {json.dumps(title, ensure_ascii=False)}, '
                f'"role": "{role}", "element": "{element}"}}'
            )
        elif _is_suspicious(title, role):
            print("    → 疑似其他游戏内容，默认排除（标题含攻略类词但不含任何官方角色名）")
        else:
            print("    → 未识别角色：如确属星塔旅人泛用图，人工加 generic: true；否则不入库")
    if lines:
        print("\n可粘贴到 assets/bili_guides/manifest.json 的 guides 数组：")
        print(",\n".join(lines))
    print(f"\n核验完成：{ok}/{len(ids)} 条可取到图片。")
    return 0 if ok == len(ids) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="猫猫球动态核验/增量扫描（只出报告，不改清单）")
    parser.add_argument("--ids", default="", help="逗号分隔的 opus_id，只核验这些（走详情接口）")
    parser.add_argument("--dry-run", action="store_true", help="不推进 checkpoint")
    parser.add_argument("--full", action="store_true", help="忽略 checkpoint 锚点")
    parser.add_argument("--pages", type=int, default=2, help="最多翻几页（默认 2）")
    parser.add_argument("--proxy", default=None, help="代理地址（默认按环境变量）")
    args = parser.parse_args()

    manifest = load_manifest(ROOT)
    if args.ids:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        return verify_ids(ids, manifest, proxy_url=args.proxy)

    known_ids = {str(g["opus_id"]) for g in manifest.get("guides") or []}

    try:
        state = load_checkpoint()
        anchor = "" if args.full else str(state.get("anchor_opus_id") or "")
        candidates, new_state, boundary_ok = scan(
            known_ids, anchor, max(1, args.pages), args.full, proxy_url=args.proxy
        )
    except BiliGuideError as exc:
        print(f"[FAIL] {exc}")
        return 1

    print(f"已知清单条目: {len(known_ids)}；锚点: {anchor or '(无，全量)'}")
    if not candidates:
        print("没有发现新的动态条目。")
    else:
        print(f"\n发现 {len(candidates)} 条候选（需人工用 --ids 逐条确认后再入库）：")
        for cand in candidates:
            preview = cand["content"][:50].replace("\n", " ")
            suspicious = _is_suspicious(preview, "")
            mark = "✗" if suspicious else "·"
            print(f"  {mark} opus_id={cand['opus_id']} 摘要={preview!r}")
            if suspicious:
                print("      疑似其他游戏内容，默认排除（摘要含攻略类词但不含任何官方角色名）")
            else:
                print(f"      → 确认：python tools/bili_update.py --ids {cand['opus_id']}")

    if not boundary_ok:
        print(
            "\n[FAIL] 未确认 checkpoint 边界（可能置顶/乱序/锚点被删/页数不足），"
            "checkpoint 未推进。请加 --pages 再试，或人工核对后手工更新锚点。"
        )
        return 1

    if args.dry_run:
        print("\n[dry-run] checkpoint 未改动。")
        return 0
    if candidates:
        # 有未确认候选时不推进：人工确认入库后重跑，自然以新入库项为界
        print("\n存在待人工确认的候选，checkpoint 未推进。")
        return 0
    if new_state is not None:
        save_checkpoint(new_state)
        print(f"\ncheckpoint 已更新 → {new_state['anchor_opus_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
