"""Backtest logic: talent-vs-matchup validation, tested offline with synthetic data."""
import numpy as np
import pandas as pd

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
