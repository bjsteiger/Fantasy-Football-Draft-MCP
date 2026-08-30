"""Team-level context features: drive efficiency and red zone play-calling identity."""
import pandas as pd
import pytest

from ffdraft.config import RECENCY_WEIGHTS
from ffdraft.features import (
    _defense_signature,
    _redzone_identity_shift,
    _season_weights,
    _strength_of_schedule,
    _team_drive_efficiency,
)


def _play(season, team, play_type, yardline_100, drive, fixed_drive_result, is_pass):
    return {
        "season": season, "posteam": team, "play_type": play_type,
        "pass": 1 if is_pass else 0, "rush": 0 if is_pass else 1,
        "yardline_100": yardline_100, "drive": drive,
        "fixed_drive_result": fixed_drive_result,
    }


class TestTeamDriveEfficiency:
    def test_counts_drive_outcomes_once_per_drive_not_per_play(self):
        pbp = pd.DataFrame([
            _play(2025, "BUF", "pass", 50, 1, "Touchdown", True),
            _play(2025, "BUF", "run", 5, 1, "Touchdown", False),   # same drive, same result
            _play(2025, "BUF", "run", 60, 2, "Punt", False),
            _play(2025, "BUF", "pass", 40, 3, "Field goal", True),
        ])
        out = _team_drive_efficiency(pbp)
        row = out[(out["team"] == "BUF") & (out["season"] == 2025)].iloc[0]
        assert row["drives"] == 3
        assert row["pct_td"] == pytest_approx(100 / 3)
        assert row["pct_punt"] == pytest_approx(100 / 3)
        assert row["pct_fg"] == pytest_approx(100 / 3)

    def test_missing_column_returns_empty_frame_not_a_crash(self):
        pbp = pd.DataFrame([{"season": 2025, "posteam": "BUF", "drive": 1}])
        out = _team_drive_efficiency(pbp)
        assert out.empty
        assert "pct_td" in out.columns


class TestRedzoneIdentityShift:
    def test_run_heavy_redzone_team_shows_positive_shift(self):
        pbp = pd.DataFrame([
            _play(2025, "PHI", "pass", 50, 1, "Touchdown", True),
            _play(2025, "PHI", "pass", 45, 1, "Touchdown", True),
            _play(2025, "PHI", "run", 10, 1, "Touchdown", False),
            _play(2025, "PHI", "run", 5, 1, "Touchdown", False),
        ])
        out = _redzone_identity_shift(pbp)
        row = out[(out["team"] == "PHI") & (out["season"] == 2025)].iloc[0]
        assert row["neutral_pass_rate"] == 100.0
        assert row["rz_pass_rate"] == 0.0
        assert row["shift"] == pytest_approx(100.0)

    def test_flat_shift_team_shows_near_zero(self):
        pbp = pd.DataFrame([
            _play(2025, "ARI", "pass", 50, 1, "Touchdown", True),
            _play(2025, "ARI", "run", 45, 1, "Touchdown", False),
            _play(2025, "ARI", "pass", 10, 2, "Touchdown", True),
            _play(2025, "ARI", "run", 5, 2, "Touchdown", False),
        ])
        out = _redzone_identity_shift(pbp)
        row = out[(out["team"] == "ARI") & (out["season"] == 2025)].iloc[0]
        assert row["shift"] == pytest_approx(0.0)


class TestSeasonWeights:
    """Newest season heaviest, whatever the window size.

    Zipping seasons against the five-entry weight table only lines up while the
    window is five or fewer. With more (an FFDRAFT_SEASONS override) zip stopped
    at the shorter list, so the five *oldest* seasons took every weight and the
    most recent ones fell out of the mapping entirely -- callers .fillna(0) the
    misses, so the newest data silently carried zero weight.
    """

    def test_full_window_matches_the_table(self):
        seasons = [2021, 2022, 2023, 2024, 2025]
        w = _season_weights(seasons)
        total = sum(RECENCY_WEIGHTS)
        for s, expected in zip(seasons, RECENCY_WEIGHTS):
            assert w[s] == pytest.approx(expected / total)

    def test_short_window_drops_the_oldest_weights(self):
        w = _season_weights([2024, 2025])
        assert w[2025] > w[2024]
        assert sum(w.values()) == pytest.approx(1.0)

    def test_long_window_still_weights_every_season_newest_heaviest(self):
        seasons = list(range(2018, 2026))  # 8 seasons > 5 weights
        w = _season_weights(seasons)
        # The regression: 2023-2025 used to be absent from the mapping entirely.
        assert set(w) == set(seasons)
        assert all(v > 0 for v in w.values())
        assert w[2025] == max(w.values())
        assert w[2025] > w[2018]
        # The extra oldest seasons sit at the oldest weight, not above it.
        assert w[2018] == w[2019] == w[2020] <= w[2021]
        assert sum(w.values()) == pytest.approx(1.0)

    def test_weights_increase_monotonically_with_recency(self):
        for n in range(1, 9):
            seasons = list(range(2026 - n, 2026))
            vals = [_season_weights(seasons)[s] for s in seasons]
            assert vals == sorted(vals), f"{n} seasons: {vals}"


class TestDefenseSignature:
    """The strength_of_schedule memo key must separate scoring formats.

    defense_ratings emits one row per season-team pair whatever the scoring, so
    the old len()-based key was identical for a PPR league and a standard one.
    Whichever asked first won, and every other league in the process silently
    read its schedule z-scores.
    """

    @staticmethod
    def _defense(fpa_wr):
        return pd.DataFrame({
            "season": [2025, 2025], "team": ["BUF", "KC"],
            "fpa_WR": fpa_wr, "fpa_RB": [20.0, 21.0],
            "def_epa_play": [-0.05, 0.02],
        })

    def test_same_values_reuse_the_same_key(self):
        a = self._defense([30.0, 32.0])
        b = self._defense([30.0, 32.0])
        assert _defense_signature(a) == _defense_signature(b)

    def test_different_scoring_gets_its_own_key_despite_equal_length(self):
        ppr = self._defense([30.0, 32.0])
        standard = self._defense([22.0, 24.0])
        assert len(ppr) == len(standard)          # what the old key compared
        assert _defense_signature(ppr) != _defense_signature(standard)

    def test_rank_columns_do_not_affect_the_key(self):
        # Ranks are derived from the values already hashed; including them would
        # only make the key noisier, not more correct.
        base = self._defense([30.0, 32.0])
        with_ranks = base.assign(fpa_WR_rank=[1, 2], def_rank=[1, 2])
        assert _defense_signature(base) == _defense_signature(with_ranks)


class TestScheduleExcludesPlayoffs:
    """Only the regular season is knowable in August.

    A drafter knows the regular-season schedule -- that is the stated reason
    using it isn't leakage. The playoff bracket is decided by the season a
    backtest is predicting, so counting those games both leaks the outcome and
    hands deep playoff teams extra opponents.
    """

    @staticmethod
    def _sched():
        rows = [
            {"season": 2025, "game_type": "REG", "home_team": "PHI",
             "away_team": "DAL", "div_game": 1},
            {"season": 2025, "game_type": "REG", "home_team": "PHI",
             "away_team": "SF", "div_game": 0},
            # Playoff rematch: an extra opponent and an extra divisional game.
            {"season": 2025, "game_type": "DIV", "home_team": "PHI",
             "away_team": "DAL", "div_game": 1},
            {"season": 2025, "game_type": "SB", "home_team": "PHI",
             "away_team": "KC", "div_game": 0},
        ]
        return pd.DataFrame(rows)

    @staticmethod
    def _defense():
        return pd.DataFrame({
            "season": [2024] * 4, "team": ["DAL", "SF", "KC", "PHI"],
            "fpa_WR": [40.0, 20.0, 20.0, 30.0], "fpa_RB": [30.0, 20.0, 20.0, 25.0],
            "fpa_QB": [25.0, 15.0, 15.0, 20.0], "fpa_TE": [12.0, 8.0, 8.0, 10.0],
            "def_epa_play": [0.1, -0.1, -0.1, 0.0],
        })

    def _run(self, monkeypatch):
        from ffdraft import features, sources
        monkeypatch.setattr(sources, "schedules", self._sched)
        features.clear_derived_cache()
        return _strength_of_schedule(2025, self._defense())

    def test_divisional_games_counts_regular_season_only(self, monkeypatch):
        out = self._run(monkeypatch).set_index("team")
        # One divisional game in the regular season; the playoff rematch is not one.
        assert out.loc["PHI", "divisional_games"] == 1

    def test_playoff_opponents_do_not_enter_schedule_strength(self, monkeypatch):
        out = self._run(monkeypatch).set_index("team")
        # Mean over DAL (40) and SF (20) only. Adding the KC Super Bowl row
        # would drag it to 26.7.
        assert out.loc["PHI", "sos_fpa_WR"] == pytest.approx(30.0)


def pytest_approx(x):
    return pytest.approx(x, abs=0.5)
