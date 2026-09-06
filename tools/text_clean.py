#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文本清理模块：处理从 SSR 爬取的攻略原文格式。

核心修复：
  1. 在剥离 HTML 标签前，先剔除 data-* 属性（含巨大 JSON 实体字符串）
  2. 解码 HTML 实体（&#34; → " 等）
  3. 保留 &Param& 游戏内占位符（LLM 可理解）
  4. strip_game_markup 在 term_replace 之后清理游戏数据标记（颜色标签、##术语#ID#、参数注释）
"""

import html as html_module
import re
from typing import Dict

# 星塔旅人元素列表（用于从角色页文本中检测元素属性）
ELEMENTS = {"Aqua", "Ignis", "Ventus", "Terra", "Lux", "Umbra"}


def extract_ssr_content(raw_html: str) -> str:
    """从 stelladb Astro SSR 页面提取纯文本。

    步骤：
    1. 移除 <head> / <script> / <style> 块
    2. 移除所有 data-* 属性（含巨大 JSON 实体）
    3. 将块级元素替换为换行
    4. 剥离剩余 HTML 标签
    5. 解码 HTML 实体
    6. 压缩多余空行
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

    # 3a. 表格结构保留：td/th 用 " | " 分隔，tr 换行。
    #     这样纹章推荐表（3列 = 70/80/90级纹章）的列结构不会丢失。
    #     注意：纹章无优先级，不标记绿底。
    text = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<t[dh][^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<tr[^>]*>", "", text, flags=re.IGNORECASE)

    # 3b. 块级元素 → 换行
    text = re.sub(
        r"</?(div|p|h[1-6]|li|section|br|table|ul|ol|article|header|footer|nav|main|aside|figure|figcaption)[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    # 4. 剥离剩余 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)

    # 5. 解码 HTML 实体
    text = html_module.unescape(text)

    # 6. 压缩表格空单元格：Google Sheet 稀疏布局会产生大量 "| | |"，
    #    折叠后每行保留实际内容，行内顺序（如 70/80/90 级纹章 3 列）不变
    cleaned_lines = []
    for line in text.split("\n"):
        if "|" not in line:
            cleaned_lines.append(line)
            continue
        cells = [c.strip() for c in line.split("|")]
        non_empty = [c for c in cells if c]
        if not non_empty:
            continue  # 整行全空，丢弃
        cleaned_lines.append(" | ".join(non_empty))
    text = "\n".join(cleaned_lines)

    # 7. 清理：移除连续空行、行首尾空白（保留 " | " 分隔符）
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


def clean_stelladb_html(text: str) -> str:
    """兼容旧接口：清理 HTML 标签。"""
    return re.sub(r"<[^>]+>", "", text).strip()


# ---- Markdown 标记清理（直发 LLM 输出清洗）----

_MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_MD_HEADING_RE = re.compile(r"(?m)^#{1,6}\s*")
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z]*\n?([\s\S]*?)```")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LIST_RE = re.compile(r"(?m)^[-*+]\s+")
_MD_TABLE_DIVIDER_RE = re.compile(r"(?m)^\|?\s*[-:]+[-| :]*\|?$")
_MD_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """清理文本中的 markdown 格式标记（**、*、###、`、- 列表、表格管道符等），面向纯文本场景。

    实现规则：
    - **x** -> x, *x* -> x
    - 行首 ### x -> x
    - ```x``` -> x, `x` -> x
    - 行首 - x / * x -> x
    - | a | b | -> a / b (折叠表格管道符)
    - 压缩 3+ 连续换行为 2
    """
    if not text:
        return text

    # 1. 代码块与行内代码
    text = _MD_CODE_BLOCK_RE.sub(r"\1", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)

    # 2. 粗体与斜体
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)

    # 3. 标题标记（行首 #、##、### 等）
    text = _MD_HEADING_RE.sub("", text)

    # 4. 无序列表符（行首 - 、* 、+ ）
    text = _MD_LIST_RE.sub("", text)

    # 5. 表格分隔线（|---|---|）
    text = _MD_TABLE_DIVIDER_RE.sub("", text)

    # 6. 表格管道符（| a | b | -> a / b）
    def _clean_table_line(match: re.Match) -> str:
        line = match.group(0)
        cells = [c.strip() for c in line.split("|") if c.strip()]
        return " / ".join(cells) if cells else ""

    text = re.sub(r"(?m)^\|.*\|$", _clean_table_line, text)

    # 7. 压缩连续多余空行并去除首尾空白
    text = _MD_MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()

