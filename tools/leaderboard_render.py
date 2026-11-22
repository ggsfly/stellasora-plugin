# -*- coding: utf-8 -*-
"""排行榜图片渲染：榜单数据 → HTML 分页表格 → 宿主 render.html2png。

排版纪律：
- 排名不补零（1 与 100 同列左对齐即可，补零方案曾被聊天侧否决）；
- 分数右对齐 + 等宽数字（font-variant-numeric: tabular-nums）+ 千分位完整显示
  （"万"缩写被当场否决——分数必须完整）；
- 长昵称截断省略（ellipsis），不得挤掉分数列；
- 页内无外网资源（纯系统字体栈，避免渲染依赖网络字体）。

编码链路分离（计划裁决 c）：本模块输出 PNG（宿主渲染结果），不做 webp——
CDN webp 参数是 GameKee 攻略图的下载侧能力，与宿主渲染无关。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
import html

# 每页承载的排名条数（与旧文本转发节点 _LB_FORWARD_CHUNK 语义一致）
LB_PAGE_SIZE = 25

# 渲染视口与缩放：宽度收窄到接近内容自然宽度——表格 width:100% 会把昵称列
# 撑满剩余空间、分数钉在右缘，视口越宽名字与分数间空白越大（720→480 实机
# 仍嫌大，360px 时昵称列只剩约 150px，超长昵称由 ellipsis 兜住）
_LB_VIEWPORT_WIDTH = 360
_LB_DEVICE_SCALE = 2.0

# HTML/CSS 模板：系统字体栈 + tabular-nums；表格两列布局（排名+昵称 | 分数）
_PAGE_TMPL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
         background: #f4f6fb; padding: 10px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 10px 14px;
          box-shadow: 0 2px 10px rgba(0,0,0,.06); }}
  .header {{ display: flex; justify-content: space-between; align-items: baseline;
            border-bottom: 2px solid #4a6fa5; padding-bottom: 8px; margin-bottom: 6px; }}
  .title {{ font-size: 18px; font-weight: 700; color: #2c3e5d; white-space: nowrap; }}
  .meta {{ font-size: 10px; color: #8a94a6; white-space: nowrap; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 4px 5px; font-size: 14px; border-bottom: 1px solid #eef1f6;
       font-variant-numeric: tabular-nums; }}
  tr:nth-child(even) td {{ background: #fafbfd; }}
  .rank {{ width: 40px; color: #4a6fa5; font-weight: 600; text-align: left; }}
  .name {{ color: #333; max-width: 0; width: auto; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; }}
  .score {{ width: 88px; text-align: right; color: #2c3e5d; font-weight: 600;
           white-space: nowrap; }}
  .top3 .rank {{ color: #d97b29; }}
  .footer {{ margin-top: 6px; font-size: 10px; color: #aab2c0; text-align: right; }}
</style></head>
<body><div class="card" id="board">
  <div class="header">
    <div class="title">{title}</div>
    <div class="meta">共 {total:,} 人 · {meta}</div>
  </div>
  <table>{rows}</table>
  <div class="footer">{page_label} · 星塔旅人 · 数据源 ssleaderboard</div>
</div></body></html>"""

_ROW_TMPL = """<tr class="{row_class}"><td class="rank">{rank}</td><td class="name">{name}</td><td class="score">{score}</td></tr>"""


def _html_escape(value: Any) -> str:
    """昵称等动态字段转义（防注入/破排版）。"""
    return html.escape(str(value), quote=True)


def build_board_pages(
    mode: str,
    sid: str,
    cn_data: Dict[str, Any],
    age_text: str,
    page_size: int = LB_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """榜单数据 → 分页 HTML 列表（纯函数，可离线测试）。

    cn_data 为瘦身缓存 regions.cn 结构（total/top）。返回
    [{"html", "label", "start", "end"}]，每页一张独立图。
    """
    mode_cn = {"bb": "联合讨伐", "fe": "终焉绝响"}.get(mode, mode.upper())
    match_sid = sid
    season_num = match_sid[2:] if match_sid.startswith(("bb", "fe")) else "?"
    title = f"【{mode_cn} S{season_num}】"

    top = cn_data.get("top") or []
    total = int(cn_data.get("total") or 0)
    pages: List[Dict[str, Any]] = []
    for start in range(0, len(top), page_size):
        chunk = top[start:start + page_size]
        rows_html = []
        for entry in chunk:
            rank = entry.get("rank")
            name = _html_escape(entry.get("name") or "?")
            score = int(entry.get("score") or 0)
            row_class = "top3" if isinstance(rank, int) and rank <= 3 else ""
            rows_html.append(
                _ROW_TMPL.format(row_class=row_class, rank=rank, name=name, score=f"{score:,}")
            )
        page_html = _PAGE_TMPL.format(
            title=title,
            total=total,
            meta=f"更新于 {age_text}",
            rows="".join(rows_html),
            page_label=f"第 {chunk[0].get('rank')}–{chunk[-1].get('rank')} 名",
        )
        pages.append({
            "html": page_html,
            "label": f"第 {chunk[0].get('rank')}–{chunk[-1].get('rank')} 名",
            "start": start,
            "end": start + len(chunk),
        })
    return pages


async def render_board_pages(
    plugin,
    pages: List[Dict[str, Any]],
) -> List[str]:
    """分页 HTML → PNG base64 列表（经宿主 ctx.render.html2png）。

    渲染失败抛 RuntimeError（调用方转为用户可见短提示，不回退文本榜——
    文本榜排版已被聊天侧否决）。render_timeout_ms 显式给足（榜单页比
    默认 10s 预算大，避免慢机型超时）。
    """
    images: List[str] = []
    for page in pages:
        # ctx.render.html2png 是 async（SDK 2.23.0 RenderCapability），必须 await
        result = await plugin.ctx.render.html2png(
            page["html"],
            selector="#board",
            viewport={"width": _LB_VIEWPORT_WIDTH, "height": 500},
            device_scale_factor=_LB_DEVICE_SCALE,
            wait_until="load",
            render_timeout_ms=30000,
        )
        # SDK 成功时解包 result dict；失败返回 {"success": False, "error": ...}
        if not isinstance(result, dict) or not result.get("image_base64"):
            error = str(result.get("error", "unknown"))[:120] if isinstance(result, dict) else "non-dict result"
            raise RuntimeError(f"榜单渲染失败: {error}")
        images.append(str(result["image_base64"]))
    return images
