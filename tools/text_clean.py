#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文本清理模块：处理从 SSR 爬取的攻略原文格式。

核心修复：
  1. 在剥离 HTML 标签前，先剔除 data-* 属性（含巨大 JSON 实体字符串）
  2. 解码 HTML 实体（&#34; → " 等）
  3. 保留 &Param& 游戏内占位符（LLM 可理解）
  4. strip_game_markup 在 term_replace 之后清理游戏数据标记（颜色标签、##术语#ID#、参数注释）
  5. 表格按 rowspan/colspan 网格展开导出（合并格在左上角坐标存储文本，
     被覆盖坐标以空段占位），保留纹章表的列位置信息
"""

from typing import Dict, Tuple
import html as html_module
import re

# 星塔旅人元素列表（用于从角色页文本中检测元素属性）
ELEMENTS = {"Aqua", "Ignis", "Ventus", "Terra", "Lux", "Umbra"}

# 表格网格解析正则：Google Sheets 导出的 waffle 结构规整（无嵌套 td），
# 用 <table>/<tr>/<td>/<th> 边界匹配即可，无需引入完整 HTML 解析器
_TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE)
_TR_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(r"<t([dh])\b([^>]*)>(.*?)</t\1>", re.DOTALL | re.IGNORECASE)

# 行表头 th 识别：Sheet 行号列（id 形如 "297R0"）+ 行号背景样式
_ROW_HEADER_ID_RE = re.compile(r'id\s*=\s*["\']?\d+R\d+', re.IGNORECASE)
_ROW_HEADER_CLASS_RE = re.compile(r'class\s*=\s*["\'][^"\']*row-headers-background', re.IGNORECASE)


def _parse_span(attrs: str, name: str) -> int:
    """从单元格属性串中解析 rowspan/colspan。

    缺失视为 1；非法值（0/负数）按 1 处理——Sheets 导出不会出现，纯容错。
    """
    match = re.search(r'\b%s\s*=\s*["\']?(-?\d+)' % name, attrs, re.IGNORECASE)
    if not match:
        return 1
    value = int(match.group(1))
    if value < 1:
        # HTML 规范中 rowspan=0 有特殊语义（延伸到表尾），但 waffle 导出不使用；
        # 按 1 处理避免非法值污染网格
        return 1
    return value


def _is_row_header(tag: str, attrs: str) -> bool:
    """判定单元格是否为 Sheet 行号噪声表头（不输出的纯数字 th）。

    tag 为 _CELL_RE 捕获的单字母 'd'/'h'（td/th 的类型位）。
    """
    if tag != "h":
        return False
    return bool(_ROW_HEADER_ID_RE.search(attrs)) or bool(_ROW_HEADER_CLASS_RE.search(attrs))


def _expand_table_grid(table_html: str) -> str:
    """把单个 <table> 展开为 rowspan/colspan 网格并按行渲染。

    - 单元格文本存储在网格左上角坐标 (r0,c0)，被跨行/跨列覆盖的坐标留空（占位）；
    - 行表头 th（Sheet 行号列）整格跳过，不占列位；
    - 每行按网格列渲染，空单元格保留为空段（列位置信号），行尾空段修剪；
    - 整行无任何非空单元格则整行丢弃。
    """
    rows_html = _TR_RE.findall(table_html)
    occupied: set = set()
    grid: Dict[Tuple[int, int], str] = {}

    for row_index, row_html in enumerate(rows_html):
        col = 0
        for cell_match in _CELL_RE.finditer(row_html):
            tag = cell_match.group(1).lower()
            attrs = cell_match.group(2)
            inner = cell_match.group(3)
            if _is_row_header(tag, attrs):
                # 行号噪声表头：跳过，不占网格列位（下游按去 th 后的列序消费）
                continue
            # 从当前列向右找到第一个未被 rowspan 覆盖的空闲坐标
            while (row_index, col) in occupied:
                col += 1
            row_span = _parse_span(attrs, "rowspan")
            col_span = _parse_span(attrs, "colspan")
            for dr in range(row_span):
                for dc in range(col_span):
                    occupied.add((row_index + dr, col + dc))
            # 跨行/跨列单元格文本只存左上角坐标；其余覆盖坐标不留记录（渲染为空段占位）
            grid[(row_index, col)] = re.sub(r"<[^>]+>", "", inner).strip()
            col += col_span

    if not occupied:
        return ""
    max_col = max(col for _, col in occupied)

    lines = []
    for row_index in range(len(rows_html)):
        cells = [grid.get((row_index, c), "") for c in range(max_col + 1)]
        # 行尾空段修剪；行首/行中空段必须保留（这是列位置信号）
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue  # 整行无内容（如只剩行号表头），丢弃
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def extract_ssr_content(raw_html: str) -> str:
    """从 stelladb Astro SSR 页面提取纯文本。

    步骤：
    1. 移除 <head> / <script> / <style> 块
    2. 移除所有 data-* 属性（含巨大 JSON 实体）
    3. 表格网格展开：逐 <tr> 解析 <td>/<th>，按 rowspan/colspan 展开为网格，
       每行 cells 用 " | " 连接（空段保留、行尾修剪、行号 th 跳过）
    4. 将块级元素替换为换行
    5. 剥离剩余 HTML 标签
    6. 解码 HTML 实体
    7. 压缩多余空行
    """
    if not raw_html:
        return ""

    # 1. 移除 head / script / style 块
    text = re.sub(r"<head>.*?</head>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # 2. 移除 data-* 属性（它们包含巨大 JSON，用 &#34; 代替引号，
    #    所以属性值内不会有字面引号或 >）
    text = re.sub(r'\sdata-[a-z_-]+="[^"]*"', "", text, flags=re.IGNORECASE)
    text = re.sub(r"\sdata-[a-z_-]+='[^']*'", "", text, flags=re.IGNORECASE)

    # 3. 表格结构保留：rowspan/colspan 网格展开后按行列导出，td/th 用 " | " 分隔，
    #    tr 换行。这样纹章推荐表（3列 = 70/80/90级纹章）的列位置不会因合并格
    #    缺位/空段压缩而坍缩；列信号由保留的空段承载。
    #    注意：纹章无优先级，不标记绿底。
    text = _TABLE_RE.sub(lambda m: "\n%s\n" % _expand_table_grid(m.group(0)), text)

    # 4. 块级元素 → 换行
    text = re.sub(
        r"</?(div|p|h[1-6]|li|section|br|table|ul|ol|article|header|footer|nav|main|aside|figure|figcaption)[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    # 5. 剥离剩余 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)

    # 6. 解码 HTML 实体
    text = html_module.unescape(text)

    # 7. 清理：移除连续空行、行首尾空白（保留 " | " 分隔符与行间空段）
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = text.strip()

    return text


# ---- 游戏数据标记清理（term_replace 之后调用）----

_COLOR_TAG_RE = re.compile(r"</?color[^>]*>")
_TERM_REF_RE = re.compile(r"##([^#]+)#\d+#")
_PARAM_ANNOT_RE = re.compile(r"参数\d+:\s*&Param\d+&[^。\n]*")
_PARAM_REF_RE = re.compile(r"&Param\d+&")


def strip_game_markup(text: str) -> str:
    """清理游戏数据标记：颜色标签、##术语#ID# 引用、参数占位符与注释。

    在 term_replace 之后调用，剥离 dict .2 字段 CN 值中残留的游戏内标记：
    - ``<color=#xxx>...</color>`` → 保留内部文本
    - ``##术语#ID#`` → ``术语``（如 ``##风系印记#1017#`` → ``风系印记``）
    - ``参数N: &ParamN&（...）`` → 整段移除（游戏调试注释，对用户无价值）
    - ``&ParamN&`` → 移除（运行时占位符，QQ 回复中无意义）
    """
    if not text:
        return text
    text = _COLOR_TAG_RE.sub("", text)
    text = _TERM_REF_RE.sub(r"\1", text)
    text = _PARAM_ANNOT_RE.sub("", text)
    text = _PARAM_REF_RE.sub("", text)
    return text


def detect_element(text: str) -> str | None:
    """从角色页文本中检测元素属性。"""
    for element in ELEMENTS:
        if element in text:
            return element
    return None
