"""Rookie projections: the draft-capital curve, the p75 cap, and the PFR team crosswalk.

Offline like the rest of the suite. draft_picks, combine and weekly_stats are all
network reads, so every test that needs them substitutes a synthetic frame.
"""
import numpy as np
import pandas as pd

from ffdraft import rookies, sources
from ffdraft.config import Scoring
from ffdraft.rookies import (
    UDFA_PICK,
    _bin_for,
    _fix_team,
    draft_capital,
    predict_rookie_ppg,
    rookie_consistency_prior,
)


def _curves(slope=-3.0, intercept=25.0, bins=None, played_rate=0.6, position="RB"):
    """One fitted position curve, in the shape fit_draft_curves returns."""
    return {position: {
        "slope": slope, "intercept": intercept, "resid_sd": 4.0, "n": 200,
        "seasons": [2020], "played_rate": played_rate, "mean_games": 12.0,
        "bins": bins or {},
    }}


def _bin(n, median, p75):
    return {"n": n, "median": median, "mean": median, "p75": p75, "played_rate": 0.6}


class TestDraftCapital:
    def test_the_first_pick_is_worth_everything_and_the_last_nothing(self):
        assert draft_capital(1) == 1.0
        assert draft_capital(UDFA_PICK) == 0.0

    def test_falls_monotonically_with_pick(self):
        picks = [1, 5, 20, 60, 150, UDFA_PICK]
        caps = [draft_capital(p) for p in picks]
        assert caps == sorted(caps, reverse=True)

    def test_the_curve_is_steeper_early_than_late(self):
        # The whole reason for the log scale: the gap between pick 3 and pick 20 is
        # enormous, and the gap between 180 and 197 is nothing.
        early = draft_capital(3) - draft_capital(20)
        late = draft_capital(180) - draft_capital(197)
        assert early > late * 10

    def test_a_missing_pick_is_treated_as_undrafted(self):
        # An unpicked player arrives as None or NaN, not as a number. Either must
        # land on the UDFA slot rather than blowing up or scoring as pick zero.
        assert draft_capital(None) == draft_capital(UDFA_PICK)
        assert draft_capital(float("nan")) == draft_capital(UDFA_PICK)
        assert draft_capital(0) == draft_capital(UDFA_PICK)

    def test_a_pick_past_the_udfa_slot_clips_instead_of_going_negative(self):
        assert draft_capital(400) == 0.0


class TestBinFor:
    def test_bin_edges_are_inclusive_at_the_top(self):
        # PICK_BINS uses lo < pick <= hi, so pick 10 is the last of "1-10" and
        # pick 11 opens the next bin.
        assert _bin_for(10) == "1-10"
        assert _bin_for(11) == "11-25"
        assert _bin_for(25) == "11-25"

    def test_anything_past_the_last_bin_falls_into_the_tail(self):
        assert _bin_for(UDFA_PICK) == "180+"
        assert _bin_for(400) == "180+"


class TestPredictRookiePpg:
    def test_an_unmodelled_position_projects_zero(self):
        assert predict_rookie_ppg("K", 5, _curves()) == 0.0

    def test_never_promises_more_than_the_bin_has_ever_produced(self):
        # The reason the cap exists: the log-linear fit extrapolates badly at the
        # top of the draft, where it predicted 19.4 PPG for a back at pick 3 when
        # the top-ten bin has actually averaged 15.9.
        curves = _curves(bins={"1-10": _bin(n=40, median=15.9, p75=18.0)})
        raw_fit = -3.0 * np.log(3) + 25.0
        assert raw_fit > 18.0  # the fit alone would overpromise
        assert predict_rookie_ppg("RB", 3, curves) == 18.0

    def test_a_thin_bin_leans_on_the_median_and_a_fat_one_on_the_fit(self):
        # Blend weight is n/(n+12): the sparse top of the draft trusts the
        # empirical median, a well-populated bin trusts the smooth fit.
        fit = -3.0 * np.log(60) + 25.0
        median = fit + 6.0  # pull the median well clear of the fit so the mix shows
        thin = predict_rookie_ppg(
            "RB", 60, _curves(bins={"51-100": _bin(n=2, median=median, p75=99.0)}))
        fat = predict_rookie_ppg(
            "RB", 60, _curves(bins={"51-100": _bin(n=400, median=median, p75=99.0)}))
        assert abs(thin - median) < abs(fat - median)
        assert fat > fit  # the median still pulls it up, just less

    def test_a_position_with_no_bin_data_falls_back_to_the_bare_fit(self):
        curves = _curves(bins={})
        assert predict_rookie_ppg("RB", 60, curves) == max(
            0.0, -3.0 * np.log(60) + 25.0)

    def test_a_projection_is_never_negative(self):
        # A steep enough slope drives the raw fit below zero deep in the draft.
        steep = _curves(slope=-9.0, intercept=5.0)
        assert predict_rookie_ppg("RB", 250, steep) == 0.0


class TestRookieConsistencyPrior:
    def test_stays_inside_the_documented_bounds(self):
        curves = _curves()
        for pick in (1, 15, 90, UDFA_PICK):
            assert 0.12 <= rookie_consistency_prior("RB", pick, curves) <= 0.62

    def test_draft_capital_buys_consistency(self):
        curves = _curves()
        assert (rookie_consistency_prior("RB", 2, curves)
                > rookie_consistency_prior("RB", 200, curves))

    def test_an_unknown_position_still_returns_a_usable_prior(self):
        # curves.get(position, {}) means played_rate falls back too, so this must
        # not raise on a position the fit skipped.
        assert 0.12 <= rookie_consistency_prior("FB", 100, {}) <= 0.62


class TestPfrTeamCrosswalk:
    def test_pfr_codes_are_translated_to_nflverse_codes(self):
        # draft_picks comes from Pro Football Reference, which uses its own team
        # codes. Left unmapped, every rookie's landing-spot join (O-line, pace,
        # schedule) silently returns nothing and the rookie is modelled in a vacuum.
        out = _fix_team(pd.Series(["GNB", "KAN", "SFO", "LAR", "OAK", "NWE"]))
        assert list(out) == ["GB", "KC", "SF", "LA", "LV", "NE"]

    def test_codes_that_already_match_pass_through(self):
        assert list(_fix_team(pd.Series(["BUF", "KC", "PHI"]))) == ["BUF", "KC", "PHI"]

    def test_lowercase_input_is_upcased_before_mapping(self):
        assert list(_fix_team(pd.Series(["gnb", "sfo"]))) == ["GB", "SF"]


def _pick_row(name, pos, pick, season=2020, **extra):
    row = {
        "season": season, "position": pos, "pick": float(pick),
        "gsis_id": f"id-{name}", "pfr_player_name": name, "name": name,
        "_key": rookies.normalize(name), "round": (int(pick) - 1) // 32 + 1,
        "team": "GNB", "college": "State", "age": 22.0,
    }
    row.update(extra)
    return row


def _week_row(gsis, season, week, pos="RB", rush_yards=0.0, rush_tds=0):
    return {
        "player_id": gsis, "season": season, "week": week, "season_type": "REG",
        "position": pos, "rushing_yards": rush_yards, "rushing_tds": rush_tds,
    }


class TestRookieSeasons:
    """The historical fit set: what counts as a rookie outcome and what gets dropped."""

    def test_a_drafted_player_who_never_played_stays_in_the_fit_as_a_zero(
            self, monkeypatch):
        # A drafted player with no stat line played no meaningful snaps. That is a
        # real rookie outcome, not missing data -- dropping it would bias the curve
        # upward by quietly deleting every bust.
        picks = pd.DataFrame([_pick_row("Played Rookie", "RB", 5),
                              _pick_row("Never Played", "RB", 200)])
        weekly = pd.DataFrame([_week_row("id-Played Rookie", 2020, w, rush_yards=100.0)
                               for w in range(1, 4)])
        monkeypatch.setattr(rookies, "draft_picks", lambda: picks)
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: weekly)

        out = rookies._rookie_seasons([2020], Scoring.preset("ppr")).set_index("name")
        assert len(out) == 2
        assert out.loc["Never Played", "ppg"] == 0.0
        assert out.loc["Never Played", "games"] == 0
        assert out.loc["Played Rookie", "ppg"] == 10.0  # 100 rushing yards a game

    def test_career_columns_on_draft_picks_do_not_shadow_rookie_production(
            self, monkeypatch):
        # draft_picks ships career totals of its own. If a colliding column
        # survived the merge, pandas would suffix the real rookie figure out from
        # under us and the fit would read career points as a first-year outcome.
        picks = pd.DataFrame([_pick_row("Career Guy", "RB", 5, points=9999.0)])
        weekly = pd.DataFrame([_week_row("id-Career Guy", 2020, 1, rush_yards=100.0)])
        monkeypatch.setattr(rookies, "draft_picks", lambda: picks)
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: weekly)

        out = rookies._rookie_seasons([2020], Scoring.preset("ppr"))
        assert out["points"].iloc[0] == 10.0
        assert "points_x" not in out.columns and "points_y" not in out.columns

    def test_an_undrafted_pick_is_pinned_to_the_udfa_slot(self, monkeypatch):
        picks = pd.DataFrame([_pick_row("Undrafted", "RB", 5)])
        picks["pick"] = np.nan
        monkeypatch.setattr(rookies, "draft_picks", lambda: picks)
        monkeypatch.setattr(sources, "weekly_stats",
                            lambda seasons: pd.DataFrame(columns=[
                                "player_id", "season", "week", "season_type",
                                "position", "rushing_yards"]))

        out = rookies._rookie_seasons([2020], Scoring.preset("ppr"))
        assert out["pick"].iloc[0] == UDFA_PICK


class TestFitDraftCurves:
    def test_a_position_with_too_little_history_is_skipped(self, monkeypatch):
        # Fewer than 25 observations is not a curve, it is noise. Fitting one
        # anyway would hand the board a confident projection built on nothing.
        picks = pd.DataFrame([_pick_row(f"Rb {i}", "RB", i + 1) for i in range(10)]
                             + [_pick_row(f"Wr {i}", "WR", i + 1) for i in range(30)])
        weekly = pd.DataFrame([_week_row(f"id-Wr {i}", 2020, 1, rush_yards=50.0)
                               for i in range(30)])
        monkeypatch.setattr(rookies, "draft_picks", lambda: picks)
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: weekly)

        curves = rookies.fit_draft_curves(Scoring.preset("ppr"), seasons=[2020])
        assert "RB" not in curves
        assert "WR" in curves

    def test_a_fitted_curve_slopes_down_from_the_top_of_the_draft(self, monkeypatch):
        # Early picks outproduce late ones, so pick (on a log scale) should carry a
        # negative slope. A positive one would mean the board prefers late picks.
        rows, weeks = [], []
        for i in range(40):
            pick = i * 6 + 1
            rows.append(_pick_row(f"Rb {i}", "RB", pick))
            # production falls off as the pick number climbs
            weeks.append(_week_row(f"id-Rb {i}", 2020, 1,
                                   rush_yards=max(0.0, 200.0 - pick)))
        monkeypatch.setattr(rookies, "draft_picks", lambda: pd.DataFrame(rows))
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: pd.DataFrame(weeks))

        curves = rookies.fit_draft_curves(Scoring.preset("ppr"), seasons=[2020])
        assert curves["RB"]["slope"] < 0
        assert curves["RB"]["n"] == 40
        assert set(curves["RB"]["bins"]) <= set(rookies.BIN_LABELS)


class TestRookieBoard:
    def _setup(self, monkeypatch, picks, combine=None):
        monkeypatch.setattr(rookies, "draft_picks", lambda: picks)
        if combine is None:
            def boom():
                raise RuntimeError("combine parquet unreachable")
            monkeypatch.setattr(rookies, "combine", boom)
        else:
            monkeypatch.setattr(rookies, "combine", lambda: combine)

    def test_a_season_with_no_draft_class_returns_an_empty_frame(self, monkeypatch):
        # The class for an upcoming season isn't published until the draft happens.
        self._setup(monkeypatch, pd.DataFrame([_pick_row("Old Guy", "RB", 5,
                                                         season=2019)]))
        out = rookies.rookie_board(2025, Scoring.preset("ppr"), curves=_curves())
        assert out.empty

    def test_an_unreachable_combine_leaves_athleticism_missing_not_broken(
            self, monkeypatch):
        # Combine testing is a nudge, not a requirement. If the parquet is
        # unreachable the board must still come back, with the columns present
        # and null rather than absent.
        picks = pd.DataFrame([_pick_row("Fast Rookie", "WR", 12, season=2025)])
        self._setup(monkeypatch, picks)

        out = rookies.rookie_board(2025, Scoring.preset("ppr"),
                                   curves=_curves(position="WR"))
        assert len(out) == 1
        assert out["forty"].isna().all()

    def test_expected_games_scales_with_draft_capital_and_stays_clipped(
            self, monkeypatch):
        # A top-five pick is on the field from week one; a late pick is inactive
        # half the year. Using one positional average for both would badly
        # understate the early picks.
        picks = pd.DataFrame([_pick_row("Early", "WR", 2, season=2025),
                              _pick_row("Late", "WR", 240, season=2025)])
        self._setup(monkeypatch, picks)

        out = rookies.rookie_board(2025, Scoring.preset("ppr"),
                                   curves=_curves(position="WR")).set_index("name")
        assert out.loc["Early", "exp_games"] > out.loc["Late", "exp_games"]
        assert out["exp_games"].between(5, 16.5).all()

    def test_the_landing_spot_team_is_translated_from_the_pfr_code(self, monkeypatch):
        picks = pd.DataFrame([_pick_row("Packer", "WR", 12, season=2025, team="GNB")])
        self._setup(monkeypatch, picks)

        out = rookies.rookie_board(2025, Scoring.preset("ppr"),
                                   curves=_curves(position="WR"))
        assert out["team"].iloc[0] == "GB"
        assert out["draft_team"].iloc[0] == "GNB"
