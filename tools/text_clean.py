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
