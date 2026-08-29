"""Individual defensive player scoring and board.

Offline like the rest of the suite: synthetic frames, no network, no cache.
"""
import pandas as pd
import pytest

from ffdraft import idp


def _wk(name, season, week, pos_group="LB", solo=0, ast=0, with_ast=0,
        sacks=0, ints=0, ff=0, pd_=0, tds=0):
    return {
        "player_display_name": name, "player_id": name.lower().replace(" ", "-"),
        "season": season, "week": week, "season_type": "REG",
        "position": "LB", "position_group": pos_group,
        "def_tackles_solo": solo, "def_tackle_assists": ast,
        "def_tackles_with_assist": with_ast, "def_sacks": sacks,
        "def_interceptions": ints, "def_fumbles_forced": ff,
        "def_pass_defended": pd_, "def_tds": tds,
    }


class TestScoringFromEspn:
    def test_reads_points_for_the_stats_it_knows(self):
        items = [{"statId": 109, "points": 1.0}, {"statId": 99, "points": 0.5},
                 {"statId": 95, "points": 4.0}]
        sc = idp.scoring_from_espn(items)
        assert sc["tackles_total"] == 1.0
        assert sc["sacks"] == 0.5
        assert sc["interceptions"] == 4.0

    def test_ignores_stats_that_only_score_for_a_defence_unit(self):
        # A statId overridden to 0 for slot 16 still scores for a player; one
        # that scores ONLY via the slot-16 override does not.
        items = [{"statId": 109, "points": 0.0, "pointsOverrides": {"16": 2.0}}]
        assert idp.scoring_from_espn(items).get("tackles_total", 0.0) == 0.0

    def test_unknown_stat_ids_are_skipped_not_guessed(self):
        assert idp.scoring_from_espn([{"statId": 9999, "points": 5.0}]) == {}

    def test_detects_whether_a_league_scores_idp_at_all(self):
        assert idp.league_scores_idp([{"statId": 109, "points": 1.0}]) is True
        assert idp.league_scores_idp([{"statId": 53, "points": 1.0}]) is False


class TestFantasyPoints:
    def test_totals_match_a_hand_computed_line(self):
        # 10 solo + 5 assists + 1 with-assist = 16 total tackles
        rows = pd.DataFrame([_wk("A", 2025, 1, solo=10, ast=5, with_ast=1,
                                 sacks=2, ints=1, ff=1, pd_=3)])
        sc = {"tackles_solo": 1.0, "tackles_assisted": 2.0, "tackles_total": 1.0,
              "sacks": 0.5, "interceptions": 4.0, "forced_fumbles": 4.0,
              "passes_defended": 1.5}
        pts = float(idp.fantasy_points(rows, sc).iloc[0])
        # 10*1 + 6*2 + 16*1 + 2*0.5 + 1*4 + 1*4 + 3*1.5 = 10+12+16+1+4+4+4.5
        assert pts == pytest.approx(51.5)

    def test_missing_columns_score_zero_rather_than_raising(self):
        rows = pd.DataFrame([{"def_tackles_solo": 5}])
        assert float(idp.fantasy_points(rows, {"tackles_solo": 1.0,
                                               "sacks": 2.0}).iloc[0]) == 5.0

    def test_assisted_tackles_combine_both_nflverse_columns(self):
        rows = pd.DataFrame([_wk("A", 2025, 1, ast=4, with_ast=3)])
        assert float(idp.fantasy_points(rows, {"tackles_assisted": 1.0}).iloc[0]) == 7.0


class TestBoard:
    def _weeks(self):
        rows = []
        for wk in range(1, 18):
            rows.append(_wk("Star Backer", 2025, wk, solo=6, ast=4))
            rows.append(_wk("Mid Backer", 2025, wk, solo=3, ast=2))
            rows.append(_wk("Deep Backer", 2025, wk, solo=1, ast=1))
            # An edge rusher whose position_group is LB but label is OLB --
            # filtering on `position` would silently drop ~21% of the pool.
            rows.append(_wk("Edge Guy", 2025, wk, pos_group="LB", solo=4, ast=2))
        return pd.DataFrame(rows)

    def test_ranks_by_points_and_is_ordered(self):
        b = idp.build_board(self._weeks(), {"tackles_solo": 1.0, "tackles_assisted": 2.0},
                            seasons=[2025])
        assert list(b["name"])[0] == "Star Backer"
        assert b["proj_points"].is_monotonic_decreasing

    def test_uses_position_group_so_outside_backers_are_kept(self):
        b = idp.build_board(self._weeks(), {"tackles_solo": 1.0}, seasons=[2025])
        assert "Edge Guy" in set(b["name"])

    def test_value_over_replacement_is_zero_at_the_replacement_rank(self):
        b = idp.build_board(self._weeks(), {"tackles_solo": 1.0}, seasons=[2025],
                            teams=2, idp_slots=1)
        # replacement is the 2nd-best in a 2-team, 1-slot league
        assert float(b.iloc[1]["vor"]) == pytest.approx(0.0)
        assert float(b.iloc[0]["vor"]) > 0

    def test_empty_input_returns_empty_board_not_a_crash(self):
        b = idp.build_board(pd.DataFrame(), {"tackles_solo": 1.0}, seasons=[2025])
        assert b.empty
        assert "proj_points" in b.columns


class TestSmallSampleGate:
    """A per-game rate from one game is not a projection.

    Without this gate a real 2025 board put a defensive back with a single
    38-point game at number one on 646 projected points, ahead of both of the
    best linebackers in the league.
    """

    def _weeks(self):
        rows = []
        for wk in range(1, 18):
            rows.append(_wk("Full Season", 2025, wk, solo=6, ast=4))
        rows.append(_wk("One Game Wonder", 2025, 1, solo=40, ast=30))
        return pd.DataFrame(rows)

    def test_one_game_sample_is_excluded(self):
        b = idp.build_board(self._weeks(), {"tackles_solo": 1.0}, seasons=[2025])
        assert "One Game Wonder" not in set(b["name"])
        assert "Full Season" in set(b["name"])

    def test_threshold_is_adjustable_for_a_short_season(self):
        b = idp.build_board(self._weeks(), {"tackles_solo": 1.0}, seasons=[2025],
                            min_games=1)
        assert "One Game Wonder" in set(b["name"])

    def test_everyone_below_the_gate_yields_an_empty_board(self):
        rows = pd.DataFrame([_wk("Cameo", 2025, 1, solo=5)])
        assert idp.build_board(rows, {"tackles_solo": 1.0}, seasons=[2025]).empty
