"""Backtest logic: talent-vs-matchup validation and the value-history rollups,
tested offline with synthetic data."""
import numpy as np
import pandas as pd
import pytest

from ffdraft import adp
from ffdraft.adp import matchup_backtest_summary


def _hist(rows):
    return pd.DataFrame(rows)


class TestMatchupBacktestSummary:
    def test_empty_history_reports_zero_player_seasons(self):
        out = matchup_backtest_summary(pd.DataFrame(
            columns=["talent_z", "matchup_adjusted_score", "points", "season"]))
        assert out["n_player_seasons"] == 0

    def test_perfect_talent_signal_scores_high_correlation(self):
        # talent_z ranks players in exactly the order they finish; matchup_z is pure
        # noise that shouldn't help or hurt much when added on top of a perfect signal.
        n = 20
        rng = np.random.default_rng(0)
        talent = np.linspace(-2, 2, n)
        points = talent * 50 + 150  # monotonic in talent, so rank order matches exactly
        noise = rng.normal(0, 0.01, n)  # negligible vs talent's spread
        hist = _hist({
            "talent_z": talent,
            "matchup_z": noise,
            "matchup_adjusted_score": talent + noise,
            "points": points,
            "season": [2023] * n,
        })
        out = matchup_backtest_summary(hist, top_n=5)
        assert out["talent_only_corr"] > 0.99
        assert out["matchup_adjusted_corr"] > 0.99

    def test_matchup_adjustment_recovers_signal_talent_alone_misses(self):
        # Construct a case where actual finish is driven by talent PLUS a schedule
        # swing that talent_z alone can't see, so matchup_adjusted_score must win.
        n = 40
        rng = np.random.default_rng(1)
        talent = rng.normal(0, 1, n)
        matchup = rng.normal(0, 1, n)
        # Actual points respond to both talent and matchup in equal measure.
        points = 100 + 40 * talent + 40 * matchup
        hist = _hist({
            "talent_z": talent,
            "matchup_z": matchup,
            "matchup_adjusted_score": talent + matchup,
            "points": points,
            "season": [2023] * n,
        })
        out = matchup_backtest_summary(hist, top_n=10)
        assert out["matchup_adjusted_corr"] > out["talent_only_corr"]
        assert out["improvement_corr"] > 0

    def test_top_n_precision_is_between_zero_and_one(self):
        n = 30
        rng = np.random.default_rng(2)
        talent = rng.normal(0, 1, n)
        matchup = rng.normal(0, 1, n)
        points = 100 + 30 * talent + rng.normal(0, 5, n)
        hist = _hist({
            "talent_z": talent,
            "matchup_z": matchup,
            "matchup_adjusted_score": talent + matchup,
            "points": points,
            "season": [2022] * n,
        })
        out = matchup_backtest_summary(hist, top_n=10)
        assert 0.0 <= out["talent_only_top_n_precision"] <= 1.0
        assert 0.0 <= out["matchup_adjusted_top_n_precision"] <= 1.0

    def test_multiple_seasons_are_averaged_not_pooled(self):
        # Two seasons, each internally perfect for talent. Precision should stay
        # near 1.0 because ranking happens within season, not across the pooled set
        # where different seasons' point scales could otherwise scramble the order.
        rows = []
        for season, offset in ((2022, 0), (2023, 500)):
            n = 15
            talent = np.linspace(-2, 2, n)
            points = offset + talent * 50 + 150
            rows.append(_hist({
                "talent_z": talent,
                "matchup_z": np.zeros(n),
                "matchup_adjusted_score": talent,
                "points": points,
                "season": [season] * n,
            }))
        hist = pd.concat(rows, ignore_index=True)
        out = matchup_backtest_summary(hist, top_n=5)
        assert out["talent_only_top_n_precision"] == 1.0
        assert out["seasons"] == [2022, 2023]


def _hist_row(name, season, ecr=20.0, hit=False, bust=False, value_ratio=1.0,
              games=16, position="WR", draft_round=2, unresolved=False):
    return {
        "_key": name.lower().replace(" ", "-"), "name": name, "position": position,
        "season": season, "ecr": ecr, "hit": hit, "bust": bust,
        "value_ratio": value_ratio, "games": games, "draft_round": draft_round,
        "unresolved": unresolved,
    }


class TestHitRates:
    def test_aggregates_by_draft_round(self):
        h = pd.DataFrame([
            _hist_row("A", 2024, draft_round=1, hit=True, value_ratio=1.5),
            _hist_row("B", 2024, draft_round=1, hit=False, value_ratio=0.5),
            _hist_row("C", 2024, draft_round=2, hit=True, value_ratio=2.0),
        ])
        out = adp.hit_rates(h).set_index("draft_round")
        assert out.loc[1, "n"] == 2
        assert out.loc[1, "hit_rate"] == pytest.approx(0.5)
        assert out.loc[2, "hit_rate"] == pytest.approx(1.0)

    def test_can_group_by_position_instead(self):
        h = pd.DataFrame([
            _hist_row("A", 2024, position="RB", hit=True),
            _hist_row("B", 2024, position="WR", hit=False),
        ])
        out = adp.hit_rates(h, by="position").set_index("position")
        assert out.loc["RB", "hit_rate"] == pytest.approx(1.0)
        assert out.loc["WR", "hit_rate"] == pytest.approx(0.0)

    def test_undrafted_players_are_excluded(self):
        # Past the draftable cutoff nobody was picking these players, so their
        # "hit" rate says nothing about draft-day decisions.
        h = pd.DataFrame([
            _hist_row("Drafted", 2024, ecr=20.0, hit=True),
            _hist_row("Undrafted", 2024, ecr=adp.DRAFTABLE_ECR_CUTOFF + 50, hit=True),
        ])
        assert adp.hit_rates(h)["n"].sum() == 1

    def test_unresolved_names_are_excluded(self):
        # An unresolved name is a player whose finish we never found, which
        # reads as a zero-point season and manufactures a fake bust.
        h = pd.DataFrame([
            _hist_row("Resolved", 2024, hit=True),
            _hist_row("Unresolved", 2024, hit=False, unresolved=True),
        ])
        assert adp.hit_rates(h)["n"].sum() == 1

    def test_results_come_back_in_round_order(self):
        h = pd.DataFrame([_hist_row(f"P{r}", 2024, draft_round=r)
                          for r in (5, 1, 3)])
        assert list(adp.hit_rates(h)["draft_round"]) == [1, 3, 5]


class TestRepeatValuePlayers:
    def test_one_good_season_is_not_a_trait(self):
        # One outperformance is a season; two is the thing worth listing.
        h = pd.DataFrame([
            _hist_row("Once", 2024, hit=True, value_ratio=2.0),
            _hist_row("Twice", 2023, hit=True, value_ratio=1.8),
            _hist_row("Twice", 2024, hit=True, value_ratio=1.9),
        ])
        assert list(adp.repeat_value_players(h)["name"]) == ["Twice"]

    def test_hit_rate_is_hits_over_seasons(self):
        h = pd.DataFrame([
            _hist_row("Half", 2023, hit=True, value_ratio=1.5),
            _hist_row("Half", 2024, hit=False, value_ratio=0.9),
        ])
        assert adp.repeat_value_players(h)["hit_rate"].iloc[0] == pytest.approx(0.5)

    def test_sorted_by_how_much_they_beat_their_slot(self):
        h = pd.DataFrame([
            _hist_row("Modest", 2023, value_ratio=1.2),
            _hist_row("Modest", 2024, value_ratio=1.2),
            _hist_row("Huge", 2023, value_ratio=3.0),
            _hist_row("Huge", 2024, value_ratio=3.0),
        ])
        assert list(adp.repeat_value_players(h)["name"]) == ["Huge", "Modest"]

    def test_the_minimum_season_count_is_adjustable(self):
        h = pd.DataFrame([_hist_row("Once", 2024, value_ratio=2.0)])
        assert adp.repeat_value_players(h, min_seasons=1)["name"].tolist() == ["Once"]
        assert adp.repeat_value_players(h, min_seasons=2).empty


class TestValueHistory:
    def test_stacks_the_seasons_it_can_load(self, monkeypatch):
        frames = {2023: pd.DataFrame([_hist_row("A", 2023)]),
                  2024: pd.DataFrame([_hist_row("B", 2024)])}
        monkeypatch.setattr(adp, "adp_vs_finish", lambda s, sc=None: frames[s])

        out = adp.value_history([2023, 2024])
        assert len(out) == 2
        assert set(out["season"]) == {2023, 2024}

    def test_a_season_that_fails_to_load_does_not_sink_the_rest(self, monkeypatch):
        # A single missing upstream season should not cost you the whole
        # backtest -- it just contributes nothing.
        def flaky(s, sc=None):
            if s == 2023:
                raise RuntimeError("upstream parquet moved")
            return pd.DataFrame([_hist_row("B", 2024)])
        monkeypatch.setattr(adp, "adp_vs_finish", flaky)

        out = adp.value_history([2023, 2024])
        assert list(out["season"]) == [2024]

    def test_no_usable_seasons_yields_an_empty_frame(self, monkeypatch):
        monkeypatch.setattr(adp, "adp_vs_finish",
                            lambda s, sc=None: pd.DataFrame())
        assert adp.value_history([2023, 2024]).empty
