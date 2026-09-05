#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证字典对攻略文本关键术语的覆盖情况。"""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
with (DATA / "dict.json").open(encoding="utf-8") as f:
    main = json.load(f)

# 从攻略文本中实际出现的关键术语（技能/潜能/秘纹/素材/元素词条）
TERMS = [
    "Torrent Flash", "Aeroflow", "Waves of Naraka", "Water Surge",
    "Cascade of Ruin", "Serpent's Glide", "Mirror Blade",
    "Dauntless Poise", "Tempest Stance", "Blade's Waltz",
    "Flower Formation: Erosion", "Almighty Leader", "Self Improvement",
    "The Cat's Treasure", "Snowy Night Surprise", "One Shot, One Down",
    "Count's Cellaring", "Count's Gift", "Colossus Core",
    "Chess Piece of Skill", "Barrage Game Cartridge", "Demon Bee Game Cartridge",
    "Ignis PEN", "Crit Rate", "Skill DMG", "Auto Attack DMG", "Ventus PEN",
    "Support Skill Lv.", "Main Skill Lv.", "Charge Eff.",
    "Sniper Operation", "Dark Ray", "Lucky Bullet",
]

for t in TERMS:
    hits = {k: v["cn"] for k, v in main.items() if v["en"] == t}
    if hits:
        sample = list(hits.items())[:2]
        print(f"[OK]   {t!r} -> {sample}")
    else:
        print(f"[MISS] {t!r}")

# 统计 en->cn 可替换术语总量
uniq_en = {}
for k, v in main.items():
    uniq_en.setdefault(v["en"], v["cn"])
print(f"\n唯一英文术语总数: {len(uniq_en):,}")
