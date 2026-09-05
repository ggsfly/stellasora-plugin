#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""术语替换器：把攻略文本中的英文术语替换为字典官方中文译名。

机制：
  1. 从 dict.json（全字段：.1 名字 + .2/.3 描述/效果文本）构建 en -> cn 映射：
     同 en 多条目时先按「.1 名字字段优先于 .2+ 描述字段」决胜，
     同为 .1 再按 CAT_PRIORITY 取优先类目——保证名字译名稳定，
     .2+ 条目仅贡献增量（整段描述、CV 名等新键）
  2. 按英文名长度降序替换（长名优先，避免 "Skill DMG" 抢先命中 "Skill DMG %"）
  3. 用正则 \b 词边界匹配，替换后不残留英文
  4. 少量缩写变体（Support Skill Lv. 等）通过补充别名表处理

注意：
  - 预设码（AAAA...）、参数占位符（&Param1&）不在替换范围（非英文单词）
  - 人名（Amber 等角色名）也在字典中，会被一并替换，这正是期望行为
  - .2/.3 的整段描述文本作为长模式参与替换：stelladb 页面与游戏解包文本
    逐字一致时会整段命中译为官方中文（实测见 docs/ 与 CHANGELOG 1.2.0）
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

# 与 build_dict.CAT_PRIORITY 一致：同英文多条目时按类目优先级取 cn
CAT_PRIORITY = [
    "Skill", "MainSkill", "SecondarySkill", "SubNoteSkill",
    "Character", "CharacterTag", "Force", "Potential", "Item",
    "Word", "FateCard", "DictionaryEntry", "DictionaryTab",
    "GachaType", "Title", "Honor", "EffectDesc", "UIText",
    "ActivityGoods", "SoldierPotential", "TravelerDuelChallengeAffix",
]

# 字典未直接收录的缩写/变体 → 官方中文（人工维护）
EXTRA_ALIASES: Dict[str, str] = {
    "Support Skill Lv.": "支援技能等级",
    "Main Skill Lv.": "主技能等级",
    "Auto Attack Lv.": "普攻等级",
    "Ultimate Lv.": "终极技等级",
    "Support Skill Lv": "支援技能等级",
    "Main Skill Lv": "主技能等级",
    "Charge Eff.": "充能效率",
    "Charge Efficiency (Main)": "充能效率（主位）",
    "Charge Efficiency (Support)": "充能效率（支援位）",
    "Charge Eff. (Main)": "充能效率（主位）",
    "Charge Eff. (Supp)": "充能效率（支援位）",
    "Charge Eff. (Supp)": "充能效率（支援位）",
    "Charge EFF. (Supp)": "充能效率（支援位）",
    "Self Improvement": "自我提升",
    "Self-Improvement": "自我提升",
    "Gold": "金色",
    "Rainbow": "彩虹",
    "Blue": "蓝色",
    "Green": "绿色",
    "Upgrade": "升级",
    "upgrade": "升级",
    "C1": "共鸣1阶",
    "C6": "共鸣6阶",
    "WIP": "施工中",
    "Not needed": "无需升级",
    "Not needed.": "无需升级",
    "Main Discs": "主位秘纹",
    "Main Disc": "主位秘纹",
    "Support Discs": "支援位秘纹",
    "Affix Priority": "词条优先级",
    "Priority Potentials": "优先潜能",
    "Optional Potentials": "可选潜能",
    "Skill Upgrade Priority": "技能升级优先度",
    "Recommended Main Discs": "推荐主位秘纹",
    "Emblem": "纹章",
    "Trekker": "旅人",
    "Rotation": "循环手法",
    "Ultimate": "终极技",
    "Support Skill": "支援技能",
    "Main Skill": "主技能",
    "Auto Attack": "普攻",
    "Normal attack": "普攻",
    "PEN": "穿透",
    "Crit Rate": "暴击率",
    "Crit DMG": "暴击伤害",
    "Skill Crit Rate": "技能暴击率",
    "Ultimate Crit Rate": "终极技暴击率",
    "Mark DMG": "印记伤害",
    "Mark Crit Rate": "印记暴击率",
    "DMG": "伤害",
    "Lv.": "等级",
    "Lv": "等级",
    "level": "等级",
    "levels": "等级",
    # ---- 攻略站页面结构性词汇（stelladb 版式标签，字典 .1 名字段不含）----
    "Energy Limit": "能量上限",
    "Charge Rate": "充能效率",
    "Attack Range": "攻击距离",
    "Range": "攻击范围",
    "Walk Speed": "移动速度",
    "Run Speed": "疾跑速度",
    "Energy": "能量",
    "Next skill upgrade cost": "技能升级消耗",
    "Skill upgrade cost": "技能升级消耗",
    "Next upgrade cost": "升级消耗",
    "Upgrade cost": "升级消耗",
    "Max upgrade reached": "已达最高升级",
    "Soul Key": "心链",
    "Soul key": "心链",
    "flavor text": "背景故事",
    "Love gift": "喜好礼物",
    "Hate gift": "讨厌礼物",
    "Dates": "约会",
    "Damage": "伤害",
    "Effect": "效果",
    "Effects": "效果",
    "Buff": "增益",
    "Buffs": "增益",
    "Discs": "秘纹",
    "Potentials": "潜能",
    "Potential": "潜能",
    "Talents": "天赋",
    "Talent": "天赋",
    "Details": "详情",
    # 修正字典 .1 条目中的空格错译（"技 能"/"详 情"）
    "Skill": "技能",
    "Skill DMG": "技能伤害",
    # ---- 抽卡渠道标签（trekker 页"渠道"行）----
    "Permanent": "常驻",
    "Standard": "标准",
    "Limited": "限定",
    "Gacha": "抽卡",
    "Banner": "卡池",
    "Premium": "付费",
    "p2w": "氪金",
    "f2p": "免费",
}


class TermReplacer:
    """基于字典的英文→官方中文替换器。"""

    def __init__(
        self,
        data_dir: Path,
        preloaded_dict: Optional[dict] = None,
        custom_replacements: Optional[Dict[str, str]] = None,
    ):
        if preloaded_dict is not None:
            # service 层传入已解析的字典：跳过 json.load，dict.json 全进程只解析一次
            main = preloaded_dict
        else:
            # CLI 独立路径：自行加载 dict.json（保持懒加载行为不变）
            dict_path = data_dir / "dict.json"
            if not dict_path.is_file():
                raise FileNotFoundError(f"字典不存在: {dict_path}")
            with dict_path.open("r", encoding="utf-8") as f:
                main = json.load(f)
        self.mapping = self._build_mapping(main)
        # 合并人工别名（别名表人工维护、更精准，直接覆盖字典映射）
        for en, cn in EXTRA_ALIASES.items():
            self.mapping[en] = cn
        # 按长度降序，长名优先替换
        self._sorted_terms: List[str] = sorted(
            self.mapping.keys(), key=lambda s: len(s), reverse=True
        )
        # 预编译正则：词边界 + 转义
        self._patterns: List[tuple] = [
            (term, re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])"))
            for term in self._sorted_terms
        ]
        # 单遍交替正则：全部 term 用 re.escape 转义后按 | 连接，一次扫描完成替换；
        # _sorted_terms 已按长度降序，alternation 左优先即最长匹配语义（与逐条长名优先一致）
        self._single_pass = re.compile(
            "(?<![A-Za-z])(?:" + "|".join(re.escape(t) for t in self._sorted_terms) + ")(?![A-Za-z])"
        )
        self.custom_replacements: Dict[str, str] = {}
        self._custom_patterns: List[tuple] = []
        if custom_replacements:
            self.set_custom_replacements(custom_replacements)

    def set_custom_replacements(self, replacements: Dict[str, str]) -> None:
        """设置用户自定义文本替换规则（优先于内置字典替换）。"""
        self.custom_replacements = dict(replacements or {})
        # 按键长度降序排序，最长短语优先替换
        sorted_keys = sorted(
            self.custom_replacements.keys(), key=lambda s: len(s), reverse=True
        )
        patterns = []
        for term in sorted_keys:
            if not term:
                continue
            # 若首尾为英文字母则加词边界，避免子词误伤；非英文/混合直接匹配
            left = r"(?<![A-Za-z])" if term[0].isalpha() else ""
            right = r"(?![A-Za-z])" if term[-1].isalpha() else ""
            pattern = re.compile(left + re.escape(term) + right)
            patterns.append((pattern, self.custom_replacements[term]))
        self._custom_patterns = patterns

    @staticmethod
    def _build_mapping(main: dict) -> Dict[str, str]:
        """en -> cn 映射；同 en 多条目时先按 .1 名字字段优先，再按 CAT_PRIORITY 取优先类目。

        过滤规则：
          - 纯数字 / 单字符英文条目跳过（防止 "5"→"五"、把普通字母替换掉）
        """
        rank = {cat: i for i, cat in enumerate(CAT_PRIORITY)}
        max_rank = len(CAT_PRIORITY)
        mapping: Dict[str, str] = {}
        best: Dict[str, tuple] = {}
        for key, entry in main.items():
            en = entry.get("en", "")
            cn = entry.get("cn", "")
            cat = entry.get("cat", "")
            if not en or not cn:
                continue
            # 过滤纯数字、单字符（避免 "5"→"五" 这类污染）
            if en.strip().isdigit() or len(en.strip()) <= 1:
                continue
            # 决胜顺序：.1 名字字段优先于 .2+ 描述字段（旧精简字典全部为 .1，
            # 因此其译名零回归），同为 .1 时再按 CAT_PRIORITY
            score = (0 if key.endswith(".1") else 1, rank.get(cat, max_rank))
            if en not in mapping or score < best[en]:
                mapping[en] = cn
                best[en] = score
        return mapping

    def replace(self, text: str) -> str:
        """把文本中的英文术语替换为中文（单遍交替正则，一次扫描）。

        等价性边界见 tests/test_term_replace_equiv.py：相邻术语命中且前序术语
        以非字母字符结尾（如 "Lv."+"Upgrade cost"）时，单遍与逐条实现可能存在
        仅标点字符差异（白名单豁免，【Metis 修订 #5】）。
        """
        if not text:
            return text
        # 1. 优先执行用户自定义替换（英文术语、特定笔误或特殊称谓）
        for pattern, replacement in self._custom_patterns:
            text = pattern.sub(replacement, text)
        # 2. 内置大字典单遍交替正则替换
        text = self._single_pass.sub(lambda m: self.mapping[m.group(0)], text)
        # 3. 对字典替换后的中文文本再次执行自定义替换（用于术语改名归一，如 心钥→心链）
        for pattern, replacement in self._custom_patterns:
            text = pattern.sub(replacement, text)
        return text

    def replace_legacy(self, text: str) -> str:
        """把文本中的英文术语替换为中文。

        旧实现保留用于等价性对照测试（tests/test_term_replace_equiv.py）：
        逐条 pattern.sub，约 2.3 万次扫描，慢但语义为历史基线。
        """
        if not text:
            return text
        for term, pattern in self._patterns:
            text = pattern.sub(self.mapping[term], text)
        return text


def replace_terms(text: str, data_dir: Optional[Path] = None) -> str:
    """便捷入口：模块级单例，避免每次调用都重新加载字典。"""
    global _replacer
    if "_replacer" not in globals():
        if data_dir is None:
            data_dir = Path(__file__).resolve().parents[1] / "data"
        _replacer = TermReplacer(data_dir)
    return _replacer.replace(text)
