"""Draft logic: survival probability, roster need, and scoring-format conversion."""
import numpy as np
import pandas as pd

from ffdraft.board import FORMAT_SHIFT_DAMPING, convert_adp_format, synthetic_adp
from ffdraft.config import LeagueSettings
from ffdraft.model import (
    _positional_need,
    apply_current_team,
    expected_best_at_next_pick,
    survival_probability,
    survival_probability_vec,
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
