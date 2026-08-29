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


class TestDraftTiming:
    """When defenders leave the board, from a league's own history.

    Deliberately not a per-player ADP: published IDP consensus correlated only
    0.30 with actual pick across two real seasons, so per-player timing would be
    noise wearing a market's clothes. The count is the stable part.
    """

    picks = {2024: [77, 78, 80, 85, 90, 94, 99, 108, 109, 124],
             2025: [47, 60, 74, 76, 84, 86, 91, 113, 123, 125, 139, 143]}

    def test_counts_defenders_gone_by_each_round(self):
        t = idp.draft_timing(self.picks, teams=10)
        assert t["gone_by_pick"][80]["by_season"][2024] == 3
        assert t["gone_by_pick"][80]["by_season"][2025] == 4
        assert t["gone_by_pick"][80]["mean_gone"] == pytest.approx(3.5)

    def test_reports_the_window_defenders_go_in(self):
        t = idp.draft_timing(self.picks, teams=10)
        assert t["first_defender_pick"] == {2024: 77, 2025: 47}
        assert t["last_defender_pick"] == {2024: 124, 2025: 143}

    def test_answers_how_many_are_gone_before_each_of_my_picks(self):
        t = idp.draft_timing(self.picks, teams=10, my_picks=[85, 96])
        at = {r["pick"]: r["mean_gone_before"] for r in t["at_my_picks"]}
        assert at[85] == pytest.approx(4.0)   # 2024: 3, 2025: 5
        assert at[96] == pytest.approx(6.5)   # 2024: 6, 2025: 7

    def test_a_league_with_no_defender_history_says_so(self):
        t = idp.draft_timing({}, teams=10)
        assert t["seasons"] == []
        assert "no defender picks" in t["note"]


class TestRecencyWeighting:
    """Multiple seasons are blended with recent ones weighted far more heavily.

    Chosen on evidence: predicting 2025 for 536 defenders from 2021-24, recency
    weighting beat both last-season-only (MAE 2.54 vs 2.67) and a flat mean
    (2.60), and ranked them better (0.712 vs 0.702 / 0.695).
    """

    def _two_seasons(self, old_solo, new_solo):
        rows = []
        for wk in range(1, 18):
            rows.append(_wk("Traj", 2024, wk, solo=old_solo))
            rows.append(_wk("Traj", 2025, wk, solo=new_solo))
        return pd.DataFrame(rows)

    def test_recent_season_dominates_the_blend(self):
        # Declining player: 20/gm then 10/gm. A flat mean would say 15.
        b = idp.build_board(self._two_seasons(20, 10), {"tackles_solo": 1.0},
                            seasons=[2024, 2025])
        ppg = float(b.iloc[0]["ppg"])
        # Two seasons draw the last two RECENCY_WEIGHTS (0.28, 0.40), i.e. a
        # 41/59 tilt -- real but not dramatic. A flat mean would say 15.0.
        assert ppg < 15.0, "a flat mean would have said 15"
        assert ppg == pytest.approx(14.1, abs=0.1)

    def test_improving_player_is_pulled_up_toward_the_recent_season(self):
        b = idp.build_board(self._two_seasons(10, 20), {"tackles_solo": 1.0},
                            seasons=[2024, 2025])
        assert float(b.iloc[0]["ppg"]) > 15.0

    def test_single_season_is_unchanged_by_the_blend(self):
        rows = pd.DataFrame([_wk("Solo", 2025, wk, solo=10) for wk in range(1, 18)])
        b = idp.build_board(rows, {"tackles_solo": 1.0}, seasons=[2025])
        assert float(b.iloc[0]["ppg"]) == pytest.approx(10.0)

    def test_reports_how_many_seasons_backed_the_projection(self):
        b = idp.build_board(self._two_seasons(12, 12), {"tackles_solo": 1.0},
                            seasons=[2024, 2025])
        assert int(b.iloc[0]["seasons_used"]) == 2


class TestRosterNeeds:
    def test_reports_an_unfilled_idp_slot(self):
        n = idp.roster_needs({"QB": 1, "RB": 2, "IDP": 1}, {"QB": 1, "RB": 2})
        assert n["IDP"] == {"required": 1, "filled": 0, "still_needed": 1}

    def test_a_filled_slot_shows_no_shortfall(self):
        n = idp.roster_needs({"IDP": 1}, {"IDP": 1})
        assert n["IDP"]["still_needed"] == 0

    def test_slots_the_league_does_not_use_are_omitted(self):
        assert "IDP" not in idp.roster_needs({"QB": 1, "IDP": 0}, {})

    def test_multiple_idp_slots_are_counted(self):
        n = idp.roster_needs({"IDP": 3}, {"IDP": 1})
        assert n["IDP"]["still_needed"] == 2


class TestDefenderNames:
    def test_picks_out_defensive_position_groups(self):
        w = pd.DataFrame([
            _wk("Backer", 2025, 1, pos_group="LB"),
            {"player_display_name": "Wideout", "position_group": "WR"},
            {"player_display_name": "Lineman", "position_group": "DL"},
        ])
        assert idp.defender_names(w) == {"Backer", "Lineman"}

    def test_empty_input_is_an_empty_set_not_a_crash(self):
        assert idp.defender_names(pd.DataFrame()) == set()


class TestNoDeadEnds:
    """Asking the offence board about a defender should point somewhere.

    Before this, player_report("Fred Warner") answered "no match for 'Fred
    Warner'" -- which says the name is wrong, not that defenders are on another
    board. best_available and separation_report returned nothing at all.
    """

    def test_a_defensive_position_is_recognised(self):
        from ffdraft import server
        p = server._idp_pointer(position="LB")
        assert p and p["use_instead"] == "idp_report"

    def test_an_offensive_position_is_left_alone(self):
        from ffdraft import server
        assert server._idp_pointer(position="WR") is None

    def test_position_check_is_case_insensitive(self):
        from ffdraft import server
        assert server._idp_pointer(position="lb") is not None
class TestSingleSeasonShrink:
    """One season and five seasons are not equally knowable.

    Measured: predicting a held-out season, one-season defenders landed at 2.80
    MAE / 0.607 rank correlation against 2.30 / 0.854 for four-season players.
    A 0.15 pull toward the upper-half mean improved both metrics on two
    independent folds and was the optimum in each.
    """

    def _mixed(self):
        rows = []
        for wk in range(1, 18):                      # veteran, 4 seasons, steady
            for yr in (2022, 2023, 2024, 2025):
                rows.append(_wk("Veteran", yr, wk, solo=10))
            rows.append(_wk("Rookie", 2025, wk, solo=20))   # one season, hot
            rows.append(_wk("Filler", 2025, wk, solo=4))
            for yr in (2024, 2025):
                rows.append(_wk("Filler Two", yr, wk, solo=5))
        return pd.DataFrame(rows)

    def test_one_season_player_is_pulled_toward_the_anchor(self):
        b = idp.build_board(self._mixed(), {"tackles_solo": 1.0},
                            seasons=[2022, 2023, 2024, 2025])
        rookie = b[b["name"] == "Rookie"].iloc[0]
        assert float(rookie["ppg"]) < 20.0, "an unshrunk rookie would sit at 20"

    def test_multi_season_players_are_untouched(self):
        b = idp.build_board(self._mixed(), {"tackles_solo": 1.0},
                            seasons=[2022, 2023, 2024, 2025])
        vet = b[b["name"] == "Veteran"].iloc[0]
        assert float(vet["ppg"]) == pytest.approx(10.0)

    def test_shrink_is_gentle_not_a_reordering(self):
        # The rookie is genuinely better and must stay ahead of the veteran.
        b = idp.build_board(self._mixed(), {"tackles_solo": 1.0},
                            seasons=[2022, 2023, 2024, 2025])
        order = list(b["name"])
        assert order.index("Rookie") < order.index("Veteran")

    def test_a_board_with_no_single_season_players_is_unchanged(self):
        rows = []
        for wk in range(1, 18):
            for yr in (2024, 2025):
                rows.append(_wk("A", yr, wk, solo=10))
                rows.append(_wk("B", yr, wk, solo=6))
        b = idp.build_board(pd.DataFrame(rows), {"tackles_solo": 1.0},
                            seasons=[2024, 2025])
        assert float(b[b["name"] == "A"].iloc[0]["ppg"]) == pytest.approx(10.0)


class TestStalePlayersExcluded:
    """A player who has stopped playing must not rank.

    A multi-season average will happily keep ranking someone who is gone: on a
    real 2026 board built from 2021-25, C.J. Mosley came out FIRST despite last
    appearing in 2024 for four games, his three strong seasons outweighing his
    absence. Every test passed at the time.
    """

    def _rows(self):
        rows = []
        for wk in range(1, 18):
            for yr in (2022, 2023):
                rows.append(_wk("Retired Star", yr, wk, solo=20))   # gone after 2023
            for yr in (2022, 2023, 2024):
                rows.append(_wk("Still Playing", yr, wk, solo=10))
        return pd.DataFrame(rows)

    def test_player_absent_from_the_latest_season_is_dropped(self):
        b = idp.build_board(self._rows(), {"tackles_solo": 1.0},
                            seasons=[2022, 2023, 2024])
        assert "Retired Star" not in set(b["name"])
        assert "Still Playing" in set(b["name"])

    def test_the_filter_can_be_turned_off_for_historical_analysis(self):
        b = idp.build_board(self._rows(), {"tackles_solo": 1.0},
                            seasons=[2022, 2023, 2024], require_recent_season=False)
        assert "Retired Star" in set(b["name"])

    def test_a_single_season_board_is_unaffected(self):
        rows = pd.DataFrame([_wk("Solo", 2025, wk, solo=10) for wk in range(1, 18)])
        assert "Solo" in set(idp.build_board(rows, {"tackles_solo": 1.0},
                                             seasons=[2025])["name"])


class TestIdpOptionGating:
    """When the defender option should and should not appear.

    Shown next to the ranked recommendations rather than inside them: the
    offence ranking weighs value against the chance a player survives to your
    next pick, and defenders have no reliable draft market to estimate survival
    from (published IDP consensus correlated 0.30 with actual pick). Ranking
    them together would imply a comparability that does not exist.
    """

    def _league(self, idp_slots, filled):
        from ffdraft.config import LeagueSettings
        lg = LeagueSettings(teams=10, starters={
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1,
            "K": 1, "DST": 1, "IDP": idp_slots})
        return lg, ({"IDP": filled} if filled else {})

    def test_no_option_when_the_league_has_no_idp_slot(self):
        from ffdraft import server
        lg, roster = self._league(0, 0)
        assert server._idp_option(lg, roster, 85) is None

    def test_no_option_once_the_slot_is_filled(self):
        from ffdraft import server
        lg, roster = self._league(1, 1)
        assert server._idp_option(lg, roster, 85) is None

    def test_unfilled_slot_without_a_league_id_says_what_is_needed(self):
        from ffdraft import server
        lg, roster = self._league(1, 0)
        out = server._idp_option(lg, roster, 85)
        assert out and out["use"] == "idp_report"

    def test_multiple_slots_still_open_after_one_is_filled(self):
        from ffdraft import server
        lg, roster = self._league(2, 1)
        assert server._idp_option(lg, roster, 85) is not None


class TestIdpSeasonFinish:
    """Real defensive points for one season, for scoring a past draft pick.

    adp.season_finish filters to FANTASY_POSITIONS, so it reports a linebacker
    as having had no season at all -- which is why a drafted defender came back
    from draft_backtest with a blank where his result should be.
    """

    def _rows(self):
        rows = []
        for wk in range(1, 18):
            rows.append(_wk("Big Year", 2025, wk, solo=10))
            rows.append(_wk("Quiet Year", 2025, wk, solo=3))
        rows.append(_wk("Hurt Early", 2025, 1, solo=12))   # one game, then injured
        return pd.DataFrame(rows)

    def test_ranks_defenders_by_real_points(self):
        f = idp.season_finish(self._rows(), {"tackles_solo": 1.0}, 2025)
        assert list(f["name"])[:2] == ["Big Year", "Quiet Year"]
        assert float(f.iloc[0]["points"]) == pytest.approx(170.0)

    def test_keeps_an_injured_player_rather_than_filtering_him_out(self):
        # A pick that busted through injury is the outcome worth reporting.
        f = idp.season_finish(self._rows(), {"tackles_solo": 1.0}, 2025)
        assert "Hurt Early" in set(f["name"])
        assert int(f[f["name"] == "Hurt Early"].iloc[0]["games"]) == 1

    def test_a_season_with_no_data_returns_an_empty_frame(self):
        f = idp.season_finish(self._rows(), {"tackles_solo": 1.0}, 2019)
        assert f.empty and "pos_rank" in f.columns
