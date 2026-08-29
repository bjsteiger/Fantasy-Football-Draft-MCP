"""redzone_shift_backtest: does a team's red zone identity shift improve on the
touchdown-luck signal alone? Data assembly tested here; the scoring math itself
(matchup_backtest_summary) is already covered generically in test_adp.py."""
import pandas as pd

from ffdraft import adp, sources


def _rz_play(team, player_id, is_pass, td, yardline_100=10):
    row = {
        "season": 2024, "posteam": team, "play_type": "pass" if is_pass else "run",
        "pass": 1 if is_pass else 0, "yardline_100": yardline_100,
    }
    if is_pass:
        row["receiver_player_id"] = player_id
        row["pass_touchdown"] = td
    else:
        row["rusher_player_id"] = player_id
        row["rush_touchdown"] = td
    return row


def _neutral_play(team, is_pass):
    return {
        "season": 2024, "posteam": team, "play_type": "pass" if is_pass else "run",
        "pass": 1 if is_pass else 0, "yardline_100": 50,
    }


def _weekly_row(player_id, name, team, season, week=1, **extra):
    row = {
        "player_id": player_id, "player_display_name": name, "position": "WR",
        "recent_team": team, "season": season, "season_type": "REG", "week": week,
    }
    row.update(extra)
    return row


class TestRedzoneShiftBacktest:
    def test_run_heavy_redzone_team_discounts_matchup_score_vs_pass_heavy_team(self, monkeypatch):
        # Both players have identical red zone role (10 touches, 1 TD -- well below
        # a ~25% WR baseline, so both look like an equal buy-low on talent alone).
        # Team RUN goes run-heavy inside the 20; team PASS keeps throwing there.
        pbp = pd.DataFrame(
            [_rz_play("RUN", "A", True, 0) for _ in range(10)]
            + [_rz_play("RUN", "A", True, 1)]
            + [_neutral_play("RUN", True) for _ in range(8)]
            + [_neutral_play("RUN", False) for _ in range(2)]
            + [_rz_play("PASS", "B", True, 0) for _ in range(10)]
            + [_rz_play("PASS", "B", True, 1)]
            + [_neutral_play("PASS", True) for _ in range(8)]
            + [_neutral_play("PASS", False) for _ in range(2)]
            # Give RUN a run-heavy red zone identity: mostly runs inside the 20.
            + [_rz_play("RUN", "other", False, 0) for _ in range(20)]
            # PASS stays pass-heavy inside the 20 too (already covered by A's own
            # red zone targets above, which are all passes).
        )

        weekly_prior = pd.DataFrame([
            _weekly_row("A", "Player A", "RUN", 2024),
            _weekly_row("B", "Player B", "PASS", 2024),
        ])
        weekly_current = pd.DataFrame([
            _weekly_row("A", "Player A", "RUN", 2025, receptions=50, receiving_yards=600),
            _weekly_row("B", "Player B", "PASS", 2025, receptions=50, receiving_yards=600),
        ])

        def fake_play_by_play(seasons=None, columns=None):
            return pbp

        def fake_weekly_stats(seasons=None):
            seasons = seasons or [2024]
            return weekly_current if seasons == [2025] else weekly_prior

        monkeypatch.setattr(sources, "play_by_play", fake_play_by_play)
        monkeypatch.setattr(sources, "weekly_stats", fake_weekly_stats)
        monkeypatch.setattr(adp, "sources", sources)

        hist = adp.redzone_shift_backtest([2025], position="WR")
        assert not hist.empty

        a = hist[hist["player_id"] == "A"].iloc[0]
        b = hist[hist["player_id"] == "B"].iloc[0]

        # Identical red zone role -> identical talent_z.
        assert a["talent_z"] == pytest_approx(b["talent_z"])
        # RUN's red zone identity shift is higher (more run-heavy near the goal
        # line) than PASS's, so A's matchup_adjusted_score should be discounted
        # below B's even though their talent signal is the same.
        assert a["matchup_adjusted_score"] < b["matchup_adjusted_score"]
        assert a["matchup_z"] > b["matchup_z"]

    def test_unsupported_position_raises(self):
        try:
            adp.redzone_shift_backtest([2025], position="RB")
            raised = False
        except ValueError:
            raised = True
        assert raised


def pytest_approx(x):
    import pytest
    return pytest.approx(x, abs=1e-6)
