from pathlib import Path
import sys

# Add tools to sys.path
tools_dir = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(tools_dir))

from dict_lookup import DictLookup
from fetcher_stelladb import StelladbFetcher
from service import (
    _build_banner_material,
    _build_disc_list_material,
    _build_leaderboard_material,
    _build_monster_material,
    _match_monster,
    _route_what_keywords,
)


def test_what_renderers():
    data_dir = Path(__file__).resolve().parents[1] / "data"
    fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "ssdata"
    lookup = DictLookup(data_dir)
    st = StelladbFetcher(cache_dir=fixtures_dir, offline_dir=fixtures_dir, proxy="")

    print("=== 1. Test _route_what_keywords ===")
    assert _route_what_keywords("卡池") == "banner", "Failed on 卡池"
    assert _route_what_keywords("这期池子抽什么") == "banner", "Failed on 池子"
    assert _route_what_keywords("最新up池") == "banner", "Failed on up池"
    assert _route_what_keywords("最新UP池") == "banner", "Failed on UP池"

    assert _route_what_keywords("排行榜") == "leaderboard", "Failed on 排行榜"
    assert _route_what_keywords("最新榜单") == "leaderboard", "Failed on 榜单"
    assert _route_what_keywords("当前赛季") == "leaderboard", "Failed on 赛季"

    assert _route_what_keywords("秘纹") == "disc", "Failed on 秘纹"
    assert _route_what_keywords("旋律推荐") == "disc", "Failed on 旋律"

    # MUST NOT match "纹章"
    assert _route_what_keywords("纹章") is None, "Failed on 纹章"
    assert _route_what_keywords("夏花的纹章优先级") is None, "Failed on 夏花的纹章优先级"
    assert _route_what_keywords("其他") is None, "Failed on 其他"
    print("✓ _route_what_keywords tests passed")

    print("\n=== 2. Test _build_banner_material ===")
    banner_text, banner_ok = _build_banner_material(st, lookup)
    assert banner_ok is True, f"Banner fetch failed: {banner_text}"
    assert "【卡池资讯】" in banner_text, f"Missing header: {banner_text}"
    assert "2026-" in banner_text, f"Missing formatted date: {banner_text}"
    assert "<color=" not in banner_text, "Found markup tag <color="
    assert "&Param" not in banner_text, "Found markup &Param"
    print("✓ _build_banner_material tests passed")

    print("\n=== 3. Test _build_leaderboard_material ===")
    lb_text, lb_ok = _build_leaderboard_material(st, lookup)
    assert lb_ok is True, f"Leaderboard fetch failed: {lb_text}"
    assert "Boss Blitz S11" in lb_text, f"Missing Boss Blitz S11 in {lb_text}"
    assert "Finale Echoing S6" in lb_text, f"Missing Finale Echoing S6 in {lb_text}"
    assert "违规封禁统计" in lb_text, f"Missing ban stats in {lb_text}"
    assert "もんえ" not in lb_text, "Leaked player name in leaderboard output"
    assert "308383893" not in lb_text, "Leaked player QQ/ID in leaderboard output"
    assert "<color=" not in lb_text, "Found markup tag <color="
    print("✓ _build_leaderboard_material tests passed")

    print("\n=== 4. Test _build_disc_list_material ===")
    disc_list_text, disc_list_ok = _build_disc_list_material(st, lookup)
    assert disc_list_ok is True, f"Disc list fetch failed: {disc_list_text}"
    assert "【秘纹列表" in disc_list_text, f"Missing header: {disc_list_text}"
    assert "<color=" not in disc_list_text, "Found markup tag <color="
    print("✓ _build_disc_list_material tests passed")

    print("\n=== 5. Test _match_monster ===")
    # Exact ID
    assert _match_monster("51002", lookup, st) == "51002"
    # Substring Opera Ghost
    assert _match_monster("Opera Ghost", lookup, st) == "51002"
    # Substring Rovina
    assert _match_monster("Rovina", lookup, st) == "51002"
    # Substring Waltz
    assert _match_monster("Waltz", lookup, st) == "51002"
    # Unknown
    assert _match_monster("UnknownMonsterXYZ", lookup, st) is None
    print("✓ _match_monster tests passed")

    print("\n=== 6. Test _build_monster_material ===")
    m_text, m_ok = _build_monster_material(st, "51002", lookup)
    assert m_ok is True, f"Monster fetch failed: {m_text}"
    assert "火" in m_text and "风" in m_text, f"Missing elemental weakness in {m_text}"
    assert "【首领机制】" in m_text, f"Missing mechanics header in {m_text}"
    assert "【难度与属性】" in m_text, f"Missing diff header in {m_text}"
    assert "Normal" in m_text, f"Missing Normal diff in {m_text}"
    assert "<color=" not in m_text, "Found markup tag <color="
    assert "&Param" not in m_text, "Found markup &Param"

    # Non-existent monster
    no_m_text, no_m_ok = _build_monster_material(st, "999999", lookup)
    assert no_m_ok is False and no_m_text == "", "Should return ('', False) for invalid monster"
    print("✓ _build_monster_material tests passed")

    print("\nALL WHAT RENDERER TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    test_what_renderers()
