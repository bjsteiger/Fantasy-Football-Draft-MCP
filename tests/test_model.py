"""Draft logic: survival probability, roster need, and scoring-format conversion."""
import numpy as np
import pandas as pd
import pytest

from ffdraft.board import FORMAT_SHIFT_DAMPING, convert_adp_format, synthetic_adp
from ffdraft.config import LeagueSettings
from ffdraft.model import (
    _positional_need,
    _season_weighted,
    apply_current_team,
    expected_best_at_next_pick,
    explain,
    recommend,
    survival_probability,
    survival_probability_vec,
    touchdown_luck_multiplier,
)


class TestSurvival:
    def test_a_player_going_before_your_pick_is_gone(self):
        assert survival_probability(adp=5, current_pick=20, next_pick=33) < 0.05

    def test_a_late_adp_player_survives(self):
        assert survival_probability(adp=120, current_pick=20, next_pick=33) > 0.9

    def test_probability_falls_as_the_wait_lengthens(self):
        short = survival_probability(adp=40, current_pick=20, next_pick=25)
        long = survival_probability(adp=40, current_pick=20, next_pick=60)
        assert short > long

    def test_always_a_probability(self):
        for adp in (1, 10, 50, 100, 250):
            for nxt in (12, 40, 90):
                p = survival_probability(adp, 10, nxt)
                assert 0.0 <= p <= 1.0

    def test_vectorised_matches_scalar(self):
        adps = np.array([3.0, 25.0, 60.0, 140.0])
        vec = survival_probability_vec(adps, 20, 33)
        scal = [survival_probability(a, 20, 33) for a in adps]
        assert np.allclose(vec, scal, atol=1e-9)

    def test_missing_adp_does_not_produce_nan(self):
        out = survival_probability_vec(np.array([np.nan, 30.0]), 10, 20)
        assert not np.isnan(out).any()


class TestPositionalNeed:
    def test_empty_starting_slot_is_a_premium(self):
        need = _positional_need(LeagueSettings(teams=12), {})
        assert need["RB"] > 1.0 and need["WR"] > 1.0

    def test_backup_quarterback_is_nearly_worthless_in_one_qb(self):
        need = _positional_need(LeagueSettings(teams=12), {"QB": 1, "RB": 2, "WR": 2, "TE": 1})
        assert need["QB"] < 0.3

    def test_third_quarterback_is_worthless(self):
        need = _positional_need(LeagueSettings(teams=12), {"QB": 2, "RB": 2, "WR": 2, "TE": 1})
        assert need["QB"] < 0.05

    def test_superflex_keeps_the_second_quarterback_valuable(self):
        roster = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
        one_qb = _positional_need(LeagueSettings(teams=12), roster)["QB"]
        sflex = _positional_need(LeagueSettings(teams=12, superflex=1), roster)["QB"]
        assert sflex > one_qb * 3
        assert sflex > 1.0  # it's still a starting slot

    def test_running_back_depth_holds_value(self):
        """Backs get hurt constantly, so bench backs actually enter lineups."""
        roster = {"QB": 1, "RB": 3, "WR": 2, "TE": 1}
        need = _positional_need(LeagueSettings(teams=12), roster)
        assert need["RB"] > need["QB"]

    def test_roster_cap_shuts_a_position_off(self):
        need = _positional_need(LeagueSettings(teams=12), {"WR": 9})
        assert need["WR"] < 0.05


class TestOpportunityCost:
    def test_value_of_waiting_reflects_who_survives(self):
        board = pd.DataFrame([
            {"position": "QB", "draft_score": 100.0, "p_available_next": 0.9},
            {"position": "QB", "draft_score": 95.0, "p_available_next": 0.95},
            {"position": "RB", "draft_score": 100.0, "p_available_next": 0.01},
            {"position": "RB", "draft_score": 40.0, "p_available_next": 0.99},
        ])
        fallback = expected_best_at_next_pick(board)
        # Quarterbacks survive, so waiting costs almost nothing.
        assert fallback["QB"] > 90
        # The elite back will be gone; waiting drops you to a much worse player.
        assert fallback["RB"] < 60

    def test_empty_position_is_handled(self):
        board = pd.DataFrame([{"position": "TE", "draft_score": 10.0,
                               "p_available_next": 0.0}])
        out = expected_best_at_next_pick(board)
        assert np.isfinite(out["TE"])


class TestCurrentTeam:
    def test_depth_chart_overrides_a_stale_team(self):
        """A player traded since he last played a game should show the new team."""
        tbl = pd.DataFrame([{"player_id": "p1", "name": "Trade Guy", "team": "OLD"}])
        dc = pd.DataFrame([{"player_id": "p1", "team": "NEW"}])
        out = apply_current_team(tbl, dc)
        assert out.loc[0, "team"] == "NEW"

    def test_player_missing_from_depth_chart_keeps_last_known_team(self):
        tbl = pd.DataFrame([{"player_id": "p1", "name": "Rookie", "team": "OLD"}])
        dc = pd.DataFrame([{"player_id": "p2", "team": "NEW"}])
        out = apply_current_team(tbl, dc)
        assert out.loc[0, "team"] == "OLD"

    def test_empty_depth_chart_is_a_no_op(self):
        tbl = pd.DataFrame([{"player_id": "p1", "name": "X", "team": "OLD"}])
        out = apply_current_team(tbl, pd.DataFrame(columns=["player_id", "team"]))
        assert out.loc[0, "team"] == "OLD"

    def test_none_depth_chart_is_a_no_op(self):
        tbl = pd.DataFrame([{"player_id": "p1", "name": "X", "team": "OLD"}])
        out = apply_current_team(tbl, None)
        assert out.loc[0, "team"] == "OLD"

    def test_multiple_players_only_matched_ones_move(self):
        tbl = pd.DataFrame([
            {"player_id": "p1", "name": "Traded", "team": "OLD"},
            {"player_id": "p2", "name": "Stayed", "team": "SAME"},
        ])
        dc = pd.DataFrame([
            {"player_id": "p1", "team": "NEW"},
            {"player_id": "p2", "team": "SAME"},
        ])
        out = apply_current_team(tbl, dc).set_index("player_id")
        assert out.loc["p1", "team"] == "NEW"
        assert out.loc["p2", "team"] == "SAME"


class TestTouchdownLuck:
    """touchdown_luck_multiplier is a cross-sectional z-score, like every other
    environment multiplier in project() -- it needs a real spread of players to
    compare against, so single-player cases are exercised as one row in a small
    board rather than in isolation.
    """

    def test_overperformer_gets_discounted_relative_to_the_field(self):
        # Player 0 converted way more red zone touches than baseline predicts;
        # players 1-2 landed close to it.
        touches = pd.Series([20.0, 20.0, 20.0])
        td = pd.Series([10.0, 4.0, 5.0])         # baseline expects 4 on 20 touches
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert m.iloc[0] < 1.0
        assert m.iloc[0] < m.iloc[1]

    def test_underperformer_gets_boosted_relative_to_the_field(self):
        touches = pd.Series([20.0, 20.0, 20.0])
        td = pd.Series([1.0, 4.0, 5.0])          # baseline expects 4 on 20 touches
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert m.iloc[0] > 1.0
        assert m.iloc[0] > m.iloc[1]

    def test_small_sample_is_pinned_neutral_even_in_a_skewed_field(self):
        """A two-touch, two-score '100%' sample sits at exactly 1.0, regardless of
        how much variance the qualifying players around it carry."""
        touches = pd.Series([2.0, 20.0, 20.0])
        td = pd.Series([2.0, 10.0, 1.0])
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06, min_touches=8)
        assert m.iloc[0] == 1.0

    def test_weight_zero_disables_the_adjustment(self):
        touches = pd.Series([20.0, 20.0])
        td = pd.Series([10.0, 1.0])
        baseline = pd.Series([0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.0)
        assert (m == 1.0).all()

    def test_never_exceeds_the_configured_weight(self):
        touches = pd.Series([50.0, 50.0, 50.0])
        td = pd.Series([49.0, 0.0, 10.0])   # one huge overperformer, one huge underperformer
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert ((m - 1.0).abs() <= 0.06 + 1e-9).all()

    def test_missing_baseline_does_not_produce_nan(self):
        touches = pd.Series([20.0, 20.0])
        td = pd.Series([5.0, 8.0])
        baseline = pd.Series([np.nan, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert np.isfinite(m).all()

    def test_uniform_field_is_neutral(self):
        """Everyone matches the baseline exactly -- no spread, no adjustment."""
        touches = pd.Series([20.0, 30.0, 40.0])
        td = pd.Series([4.0, 6.0, 8.0])     # each exactly 20%
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert (m == 1.0).all()


class TestSyntheticAdp:
    def test_quarterbacks_and_tight_ends_slide_past_their_value(self):
        """A room does not draft in value order: the QB1 goes far later than RB1."""
        assert synthetic_adp("QB", 1) > synthetic_adp("RB", 1) * 5
        assert synthetic_adp("TE", 1) > synthetic_adp("WR", 1) * 3

    def test_monotonic_within_a_position(self):
        for pos in ("QB", "RB", "WR", "TE"):
            vals = [synthetic_adp(pos, r) for r in range(1, 30)]
            assert vals == sorted(vals)


class TestFormatConversion:
    @staticmethod
    def _board():
        """A board with realistic depth. Format conversion works on rank shifts, so
        a three-player board can't move anyone — the ordering has nowhere to go."""
        rows = []
        for i in range(60):   # receivers: high reception volume
            rows.append({"name": f"WR{i}", "position": "WR",
                         "proj_points": 240 - i * 2.5,
                         "receptions": 95 - i})
        for i in range(50):   # backs: mixed, some pass-catching some not
            rows.append({"name": f"RB{i}", "position": "RB",
                         "proj_points": 250 - i * 3.0,
                         "receptions": (70 - i) if i % 2 == 0 else 15})
        for i in range(24):   # quarterbacks: zero receptions
            rows.append({"name": f"QB{i}", "position": "QB",
                         "proj_points": 380 - i * 6.0, "receptions": 0})
        b = pd.DataFrame(rows)
        b["receptions"] = b["receptions"].clip(lower=0)
        b = b.sort_values("proj_points", ascending=False).reset_index(drop=True)
        b["overall_rank"] = np.arange(1, len(b) + 1)
        b["adp"] = b["overall_rank"].astype(float)
        # proj_points here are league-format points; PPR adds the missing credit.
        return b

    def _converted(self, label, gap):
        b = self._board()
        b["proj_points_ppr"] = b["proj_points"] + gap * b["receptions"]
        return convert_adp_format(b, label).set_index("name")

    def test_ppr_league_leaves_rankings_untouched(self):
        b = self._board()
        b["proj_points_ppr"] = b["proj_points"]
        out = convert_adp_format(b, "ppr")
        assert out["adp"].equals(b["adp"])
        assert out["adp_format"].iloc[0] == "ppr"

    def test_reception_heavy_players_fall_in_standard(self):
        out = self._converted("standard", 1.0)
        # WR0 catches 95 passes; RB1 catches 15.
        assert out.loc["WR0", "adp"] > out.loc["WR0", "adp_ppr"]
        assert out.loc["RB1", "adp"] < out.loc["RB1", "adp_ppr"]

    def test_quarterbacks_move_less_than_receivers(self):
        out = self._converted("standard", 1.0)
        qb_move = float(abs(out.loc["QB0", "adp"] - out.loc["QB0", "adp_ppr"]))
        wr_move = float(abs(out.loc["WR0", "adp"] - out.loc["WR0", "adp_ppr"]))
        assert qb_move < wr_move

    def test_half_ppr_shift_is_smaller_than_standard(self):
        half = self._converted("half_ppr", 0.5)
        std = self._converted("standard", 1.0)
        assert abs(half.loc["WR0", "adp_shift"]) < abs(std.loc["WR0", "adp_shift"])

    def test_shift_is_damped_not_applied_whole(self):
        assert 0 < FORMAT_SHIFT_DAMPING < 1

    def test_adp_never_goes_below_one(self):
        for label, gap in [("half_ppr", 0.5), ("standard", 1.0)]:
            out = self._converted(label, gap)
            assert (out["adp"] >= 1.0).all()

    def test_missing_ppr_column_is_a_no_op(self):
        b = self._board().drop(columns=[])
        out = convert_adp_format(b, "standard")
        assert out["adp"].equals(b["adp"])


class TestSeasonWeighted:
    def test_the_recent_season_carries_more_weight(self):
        profiles = pd.DataFrame([
            {"player_id": "p1", "season": 2023, "v": 0.0},
            {"player_id": "p1", "season": 2024, "v": 1.0},
        ])
        # Two seasons draw weights 0.28 and 0.40, so an improving player lands
        # above the midpoint of his two values.
        assert _season_weighted(profiles, "v").loc["p1"] == pytest.approx(0.40 / 0.68)

    def test_one_row_per_player(self):
        profiles = pd.DataFrame([
            {"player_id": "p1", "season": 2023, "v": 1.0},
            {"player_id": "p1", "season": 2024, "v": 1.0},
            {"player_id": "p2", "season": 2024, "v": 2.0},
        ])
        out = _season_weighted(profiles, "v")
        assert len(out) == 2
        assert out.loc["p2"] == pytest.approx(2.0)

    def test_a_season_with_no_value_counts_as_a_zero(self):
        # Unlike separation_summary, a missing value here is folded in as zero
        # rather than skipped: the denominator still carries that season's
        # weight, so a player who produced nothing in a season is pulled down
        # by it instead of being judged only on the seasons he showed up.
        profiles = pd.DataFrame([
            {"player_id": "p1", "season": 2023, "v": np.nan},
            {"player_id": "p1", "season": 2024, "v": 1.0},
        ])
        assert _season_weighted(profiles, "v").loc["p1"] == pytest.approx(0.40 / 0.68)


def _cand(name, position="RB", draft_score=100.0, adp=10.0, drafted=False, **extra):
    row = {"name": name, "position": position, "draft_score": draft_score,
           "adp": adp, "drafted": drafted}
    row.update(extra)
    return row


class TestRecommend:
    def test_drafted_players_are_not_recommended(self):
        b = pd.DataFrame([_cand("Gone", drafted=True),
                          _cand("Available", drafted=False)])
        out = recommend(b, LeagueSettings(), current_pick=1, next_pick=24)
        assert list(out["name"]) == ["Available"]

    def test_an_empty_board_returns_empty(self):
        b = pd.DataFrame(columns=["name", "position", "draft_score", "adp", "drafted"])
        assert recommend(b, LeagueSettings(), current_pick=1, next_pick=24).empty

    def test_top_n_caps_the_list(self):
        b = pd.DataFrame([_cand(f"Player {i}", draft_score=100.0 - i)
                          for i in range(20)])
        out = recommend(b, LeagueSettings(), current_pick=1, next_pick=24, top_n=5)
        assert len(out) == 5

    def test_results_come_back_ranked_by_pick_value(self):
        b = pd.DataFrame([_cand(f"Player {i}", draft_score=100.0 - i)
                          for i in range(6)])
        out = recommend(b, LeagueSettings(), current_pick=1, next_pick=24)
        assert list(out["pick_value"]) == sorted(out["pick_value"], reverse=True)

    def test_your_last_pick_treats_nobody_as_surviving(self):
        # With no next pick there is no waiting, so every survival chance is zero
        # and urgency is total.
        b = pd.DataFrame([_cand("A Back", adp=200.0)])
        out = recommend(b, LeagueSettings(), current_pick=1, next_pick=None)
        assert out["p_available_next"].iloc[0] == 0.0
        assert out["urgency"].iloc[0] == 1.0

    def test_a_player_certain_to_survive_is_worth_less_now(self):
        # Opportunity cost: two equally good backs, one of whom will still be
        # there at your next turn, should not be valued the same right now.
        b = pd.DataFrame([
            _cand("Goes Now", adp=1.0, draft_score=100.0),
            _cand("Lasts", position="WR", adp=250.0, draft_score=100.0),
        ])
        out = recommend(b, LeagueSettings(), current_pick=2, next_pick=23
                        ).set_index("name")
        assert out.loc["Lasts", "p_available_next"] > out.loc["Goes Now",
                                                              "p_available_next"]
        assert out.loc["Goes Now", "urgency"] > out.loc["Lasts", "urgency"]

    def test_a_full_position_is_discounted_against_an_open_slot(self):
        # Same projection, but one position still has a starting slot open and
        # the other is capped out.
        b = pd.DataFrame([_cand("Back", position="RB", draft_score=100.0),
                          _cand("Passer", position="QB", draft_score=100.0)])
        roster = {"QB": 2, "RB": 0}      # QB at ROSTER_CAP, RB empty
        out = recommend(b, LeagueSettings(), current_pick=1, next_pick=24,
                        roster=roster).set_index("name")
        assert out.loc["Back", "need_mult"] > out.loc["Passer", "need_mult"]
        assert out.loc["Back", "pick_value"] > out.loc["Passer", "pick_value"]

    def test_a_board_without_a_drafted_column_is_all_available(self):
        b = pd.DataFrame([{"name": "A Back", "position": "RB",
                           "draft_score": 100.0, "adp": 10.0}])
        assert len(recommend(b, LeagueSettings(), current_pick=1, next_pick=24)) == 1


class TestExplain:
    def _row(self, **kw):
        base = {"position": "RB", "pos_rank": 3, "proj_points": 240.0,
                "adj_ppg": 15.2}
        base.update(kw)
        return pd.Series(base)

    def test_leads_with_positional_rank_and_projection(self):
        out = explain(self._row())
        assert "RB3 by projection" in out
        assert "240 pts" in out

    def test_a_multiplier_near_one_is_not_mentioned(self):
        # Everything within 2% of neutral is noise; listing it would bury the
        # factors that actually moved the projection.
        assert "O-line" not in explain(self._row(m_oline=1.01))

    def test_a_meaningful_multiplier_is_reported_with_its_direction(self):
        out = explain(self._row(m_oline=1.15, m_td_luck=0.80))
        assert "O-line +15.0%" in out
        assert "touchdown regression -20.0%" in out

    def test_survival_chance_is_reported_when_known(self):
        assert "60% chance he lasts" in explain(self._row(p_available_next=0.6))

    def test_a_sparse_row_still_produces_a_string(self):
        # explain() runs on rows from several different frames, not all of which
        # carry every column.
        assert isinstance(explain(pd.Series({"position": "RB"})), str)


def _proj_row(name, position="RB", fp_mean=12.0, **kw):
    """A full feature row for project(). Defaults are deliberately neutral so a
    test can move one input and read the effect of that input alone."""
    row = {
        "player_id": f"id-{name}", "name": name, "position": position,
        "team": "BUF", "fp_mean": fp_mean, "fp_cv": 0.35,
        "games_last": 17.0, "seasons_played": 3.0, "last_season": 2025,
        "age": 26.0, "exp_games": 16.0, "injury_risk": 0.10,
        "consistency_sample_games": 17.0,
        "rz_touches": 20.0, "rz_td": 4.0, "rz_baseline_rate": 0.20,
        "run_block_z": 0.0, "pass_block_z": 0.0,
        "plays_per_game": 64.0, "neutral_pass_rate": 0.55, "rush_rate": 0.43,
        "divisional_games": 6.0, "rec_per_game": 3.0,
        # weekly-distribution inputs, produced upstream by build_player_table
        "floor": 8.0, "startable_rate": 0.55, "rookie_consistency": np.nan,
    }
    row.update(kw)
    return row


def _projected(*rows, league=None, weights=None):
    from ffdraft.config import ModelWeights
    from ffdraft.model import project
    return project(pd.DataFrame(list(rows)), league or LeagueSettings(),
                   weights or ModelWeights()).set_index("name")


def _field(n=12, position="RB", **kw):
    """A spread of comparable players, so cross-sectional z-scores have a
    population to work against."""
    return [_proj_row(f"Filler {i}", position=position, fp_mean=8.0 + i * 0.5, **kw)
            for i in range(n)]


class TestProjectBaseline:
    def test_a_projection_and_a_draft_score_come_out(self):
        out = _projected(_proj_row("A Back"), *_field())
        assert np.isfinite(out.loc["A Back", "proj_points"])
        assert np.isfinite(out.loc["A Back", "draft_score"])

    def test_a_small_sample_is_regressed_toward_the_positional_target(self):
        # Two games of hot form is not a season. The regressed baseline must sit
        # well below the raw average, or a cameo outranks a proven starter.
        hot = _proj_row("Two Game Wonder", fp_mean=30.0, games_last=2.0,
                        seasons_played=1.0)
        proven = _proj_row("Proven Starter", fp_mean=18.0, games_last=17.0,
                           seasons_played=5.0)
        out = _projected(hot, proven, *_field())
        assert out.loc["Two Game Wonder", "baseline_ppg"] < 30.0
        assert out.loc["Proven Starter", "baseline_ppg"] > out.loc[
            "Two Game Wonder", "baseline_ppg"]

    def test_a_stale_veteran_is_discounted_hard(self):
        # A two-seasons-retired back's still-strong old form once outprojected a
        # real board and became the runaway top recommendation.
        fresh = _proj_row("Still Playing", fp_mean=18.0, last_season=2025)
        stale = _proj_row("Long Retired", fp_mean=18.0, last_season=2023)
        out = _projected(fresh, stale, *_field())
        assert out.loc["Long Retired", "baseline_ppg"] < 0.25 * out.loc[
            "Still Playing", "baseline_ppg"]

    def test_the_regression_target_is_starter_caliber_not_everyone(self):
        # Including third-stringers would drag the target down far enough that
        # regressing toward it cuts a genuine RB1 by a third.
        starters = [_proj_row(f"Starter {i}", fp_mean=16.0 + i * 0.2)
                    for i in range(10)]
        scrubs = [_proj_row(f"Scrub {i}", fp_mean=0.5) for i in range(60)]
        out = _projected(*starters, *scrubs)
        assert out.loc["Starter 0", "pos_target"] > 5.0


class TestProjectRookies:
    def test_a_rookie_keeps_its_capital_fitted_baseline(self):
        # The veteran regression would replace it with a positional average and
        # erase the whole distinction between a top-five pick and a sixth-rounder.
        early = _proj_row("Early Rookie", fp_mean=np.nan, games_last=0.0,
                          seasons_played=0.0, last_season=np.nan,
                          is_rookie=True, baseline_ppg=14.0, age=22.0)
        late = _proj_row("Late Rookie", fp_mean=np.nan, games_last=0.0,
                         seasons_played=0.0, last_season=np.nan,
                         is_rookie=True, baseline_ppg=4.0, age=22.0)
        out = _projected(early, late, *_field())
        assert out.loc["Early Rookie", "baseline_ppg"] == pytest.approx(14.0)
        assert out.loc["Late Rookie", "baseline_ppg"] == pytest.approx(4.0)

    def test_a_rookie_is_not_treated_as_stale(self):
        # Rookies have no last_season; the staleness discount must not fire on
        # them or every rookie collapses to nearly zero.
        rook = _proj_row("A Rookie", fp_mean=np.nan, games_last=0.0,
                         seasons_played=0.0, last_season=np.nan,
                         is_rookie=True, baseline_ppg=12.0)
        out = _projected(rook, *_field())
        assert out.loc["A Rookie", "baseline_ppg"] == pytest.approx(12.0)


class TestProjectRanking:
    def test_positional_and_overall_ranks_are_assigned(self):
        out = _projected(*_field(n=6), *_field(n=6, position="WR"))
        assert set(out["pos_rank"]) >= {1, 2, 3}
        assert out["overall_rank"].min() == 1

    def test_vor_is_value_over_the_replacement_level_player(self):
        out = _projected(*_field(n=30))
        assert (out["vor"] == out["proj_points"] - out["replacement_points"]).all()

    def test_the_best_player_at_a_position_ranks_first(self):
        rows = _field(n=10)
        best = _proj_row("Clear Best", fp_mean=30.0)
        out = _projected(best, *rows)
        assert out.loc["Clear Best", "pos_rank"] == 1
