"""Separation and route efficiency: route estimation, qualification, recency weighting.

Offline like the rest of the suite. Next Gen Stats, snap counts and play-by-play are
all network reads, so every test substitutes a synthetic frame. separation_profile
memoises into features._DERIVED, so anything touching it clears that cache first.
"""
import numpy as np
import pandas as pd
import pytest

from ffdraft import features, separation, sources


@pytest.fixture(autouse=True)
def _clear_derived():
    features.clear_derived_cache()
    yield
    features.clear_derived_cache()


def _snap(player, week, position="WR", pct=1.0, season=2024, team="BUF",
          game_type="REG"):
    return {"player": player, "season": season, "week": week, "team": team,
            "position": position, "offense_pct": pct, "game_type": game_type}


def _dropbacks(week, n, season=2024, team="BUF"):
    """n pass plays for a team-week, as play-by-play rows."""
    return [{"posteam": team, "season": season, "week": week, "pass": 1}
            for _ in range(n)]


class TestEstimatedRoutes:
    """Routes = snap share x team dropbacks, damped by position."""

    def _patch(self, monkeypatch, snaps, pbp):
        monkeypatch.setattr(sources, "snap_counts", lambda seasons: pd.DataFrame(snaps))
        monkeypatch.setattr(sources, "play_by_play", lambda seasons: pd.DataFrame(pbp))

    def test_backs_and_tight_ends_are_damped_more_than_receivers(self, monkeypatch):
        # A back or an in-line tight end stays in to protect on a share of
        # dropbacks, so a raw snap-share estimate would overstate their routes.
        snaps = [_snap("A Wr", 1, "WR"), _snap("A Rb", 1, "RB"),
                 _snap("A Te", 1, "TE"), _snap("A Fb", 1, "FB")]
        self._patch(monkeypatch, snaps, _dropbacks(1, 100))

        out = separation.estimated_routes([2024]).set_index("player")
        assert out.loc["A Wr", "routes_est"] == pytest.approx(97.0)
        assert out.loc["A Rb", "routes_est"] == pytest.approx(62.0)
        assert out.loc["A Te", "routes_est"] == pytest.approx(86.0)
        assert out.loc["A Fb", "routes_est"] == pytest.approx(35.0)

    def test_snap_share_scales_the_estimate(self, monkeypatch):
        snaps = [_snap("Full Time", 1, "WR", pct=1.0),
                 _snap("Rotational", 1, "WR", pct=0.5)]
        self._patch(monkeypatch, snaps, _dropbacks(1, 100))

        out = separation.estimated_routes([2024]).set_index("player")
        assert out.loc["Rotational", "routes_est"] == pytest.approx(
            out.loc["Full Time", "routes_est"] / 2)

    def test_postseason_snaps_are_excluded(self, monkeypatch):
        # The rate stats built on top of this are regular-season figures; folding
        # in playoff snaps would inflate routes for exactly the best offences.
        snaps = [_snap("A Wr", 1, "WR"), _snap("A Wr", 2, "WR", game_type="POST")]
        self._patch(monkeypatch, snaps, _dropbacks(1, 100) + _dropbacks(2, 100))

        out = separation.estimated_routes([2024]).set_index("player")
        assert out.loc["A Wr", "routes_est"] == pytest.approx(97.0)
        assert out.loc["A Wr", "games"] == 1

    def test_routes_accumulate_across_weeks(self, monkeypatch):
        snaps = [_snap("A Wr", w, "WR") for w in (1, 2, 3)]
        pbp = _dropbacks(1, 100) + _dropbacks(2, 100) + _dropbacks(3, 100)
        self._patch(monkeypatch, snaps, pbp)

        out = separation.estimated_routes([2024]).set_index("player")
        assert out.loc["A Wr", "routes_est"] == pytest.approx(291.0)
        assert out.loc["A Wr", "games"] == 3

    def test_a_week_with_no_matching_pbp_contributes_nothing(self, monkeypatch):
        # snap_counts and pbp use different game_id formats, so the join is on
        # team-week. A week that fails to match must contribute zero rather than
        # NaN-poisoning the player's season total.
        snaps = [_snap("A Wr", 1, "WR"), _snap("A Wr", 9, "WR")]
        self._patch(monkeypatch, snaps, _dropbacks(1, 100))

        out = separation.estimated_routes([2024]).set_index("player")
        assert out.loc["A Wr", "routes_est"] == pytest.approx(97.0)


def _ngs(name, season=2024, week=0, sep=3.0, cushion=6.0, yac_oe=0.5,
         targets=100, receptions=70, yards=900, position="WR"):
    return {
        "player_gsis_id": f"id-{name}", "player_display_name": name,
        "player_position": position, "season": season, "season_type": "REG",
        "week": week, "team_abbr": "BUF", "avg_separation": sep,
        "avg_cushion": cushion, "avg_yac_above_expectation": yac_oe,
        "avg_intended_air_yards": 9.0, "percent_share_of_intended_air_yards": 25.0,
        "catch_percentage": 70.0, "targets": targets, "receptions": receptions,
        "yards": yards,
    }


def _prod(name, season=2024, rec_yards=900.0, targets=100.0):
    return {
        "player_id": f"id-{name}", "season": season, "week": 1,
        "season_type": "REG", "receiving_yards": rec_yards, "targets": targets,
        "receptions": 70.0, "receiving_air_yards": 1200.0, "receiving_epa": 20.0,
        "target_share": 0.25, "wopr": 0.6, "racr": 0.75,
    }


def _routes(name, season=2024, routes_est=400.0, position="WR"):
    return {"player": name, "season": season, "position": position,
            "routes_est": routes_est, "games": 17, "snap_pct": 0.9}


class TestSeparationProfile:
    def _patch(self, monkeypatch, ngs, prod, routes):
        monkeypatch.setattr(separation, "ngs_receiving", lambda: pd.DataFrame(ngs))
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: pd.DataFrame(prod))
        monkeypatch.setattr(separation, "estimated_routes",
                            lambda seasons: pd.DataFrame(routes))

    def _twelve(self, **kw):
        """Twelve qualified receivers -- one over the ten-player scoring minimum."""
        names = [f"Wr {i}" for i in range(12)]
        ngs = [_ngs(n, sep=2.5 + i * 0.1) for i, n in enumerate(names)]
        prod = [_prod(n, rec_yards=700.0 + i * 40) for i, n in enumerate(names)]
        routes = [_routes(n) for n in names]
        return names, ngs, prod, routes

    def test_yprr_and_tprr_are_production_over_estimated_routes(self, monkeypatch):
        names, ngs, prod, routes = self._twelve()
        self._patch(monkeypatch, ngs, prod, routes)

        out = separation._separation_profile([2024]).set_index("name")
        assert out.loc["Wr 0", "yprr"] == pytest.approx(700.0 / 400.0)
        assert out.loc["Wr 0", "tprr"] == pytest.approx(100.0 / 400.0)

    def test_a_part_time_receiver_is_not_scored(self, monkeypatch):
        # Qualification is deliberately strict. These are rate stats, and a
        # part-timer with 150 routes posts a flattering YPRR that says nothing
        # about how he would hold up in a real workload.
        names, ngs, prod, routes = self._twelve()
        ngs.append(_ngs("Part Timer", sep=9.9))
        prod.append(_prod("Part Timer", rec_yards=300.0, targets=20.0))
        routes.append(_routes("Part Timer", routes_est=150.0))
        self._patch(monkeypatch, ngs, prod, routes)

        out = separation._separation_profile([2024]).set_index("name")
        assert not out.loc["Part Timer", "qualified"]
        assert np.isnan(out.loc["Part Timer", "sep_score"])
        assert out.loc["Wr 0", "qualified"]

    def test_both_route_and_target_floors_must_be_cleared(self, monkeypatch):
        names, ngs, prod, routes = self._twelve()
        # Plenty of routes, too few targets -- a blocking-heavy role.
        ngs.append(_ngs("Few Targets"))
        prod.append(_prod("Few Targets", targets=40.0))
        routes.append(_routes("Few Targets", routes_est=600.0))
        self._patch(monkeypatch, ngs, prod, routes)

        out = separation._separation_profile([2024]).set_index("name")
        assert not out.loc["Few Targets", "qualified"]

    def test_a_season_with_too_few_qualifiers_leaves_scores_missing(self, monkeypatch):
        # Fewer than ten qualified receivers is not a population to z-score
        # against, so the season is left unscored rather than ranked on noise.
        names = [f"Wr {i}" for i in range(5)]
        self._patch(monkeypatch, [_ngs(n) for n in names],
                    [_prod(n) for n in names], [_routes(n) for n in names])

        out = separation._separation_profile([2024])
        assert out["qualified"].all()
        assert out["sep_score"].isna().all()

    def test_separation_drives_the_score(self, monkeypatch):
        # avg_separation carries the heaviest weight in the composite, so with
        # everything else level the receiver who gets open most must score highest.
        names, ngs, prod, routes = self._twelve()
        self._patch(monkeypatch, ngs, prod, routes)

        out = separation._separation_profile([2024]).set_index("name")
        assert out.loc["Wr 11", "sep_score"] > out.loc["Wr 0", "sep_score"]

    def test_weekly_rows_are_aggregated_when_no_season_summary_exists(
            self, monkeypatch):
        # NGS publishes weekly rows plus a week==0 season summary. When the
        # summary is absent the weekly rows have to be rolled up instead --
        # otherwise the frame comes back empty and every receiver disappears.
        names = [f"Wr {i}" for i in range(12)]
        ngs = []
        for i, n in enumerate(names):
            for wk in (1, 2):
                ngs.append(_ngs(n, week=wk, sep=2.5 + i * 0.1, targets=50,
                                receptions=35, yards=450))
        self._patch(monkeypatch, ngs, [_prod(n) for n in names],
                    [_routes(n) for n in names])

        out = separation._separation_profile([2024]).set_index("name")
        assert len(out) == 12
        # targets summed across the two weekly rows, separation averaged
        assert out.loc["Wr 0", "targets"] == 100
        assert out.loc["Wr 0", "avg_separation"] == pytest.approx(2.5)

    def test_the_profile_is_memoised_between_calls(self, monkeypatch):
        names, ngs, prod, routes = self._twelve()
        self._patch(monkeypatch, ngs, prod, routes)
        calls = []
        monkeypatch.setattr(separation, "_separation_profile",
                            lambda seasons=None: calls.append(1) or pd.DataFrame())

        separation.separation_profile([2024])
        separation.separation_profile([2024])
        assert len(calls) == 1


def _qualified(name, season, sep_score=1.0, cushion=6.0, yprr=2.0):
    return {"player_id": f"id-{name}", "name": name, "position": "WR",
            "season": season, "qualified": True, "sep_score": sep_score,
            "avg_separation": 3.0, "avg_cushion": cushion, "yprr": yprr,
            "tprr": 0.25, "avg_yac_above_expectation": 0.5, "routes_est": 400.0}


class TestSeparationSummary:
    def test_no_qualified_receivers_returns_the_empty_contract(self, monkeypatch):
        # Callers merge on these columns. Returning a bare empty frame instead
        # would turn an unremarkable early-season state into a KeyError.
        monkeypatch.setattr(separation, "separation_profile",
                            lambda seasons=None: pd.DataFrame(
                                [_qualified("Nobody", 2024)]).assign(qualified=False))

        out = separation.separation_summary([2024])
        assert out.empty
        assert {"player_id", "sep_score", "yprr", "tprr"} <= set(out.columns)

    def test_the_recent_season_carries_more_weight(self, monkeypatch):
        # Two seasons draw weights 0.28 and 0.40, so a player who improved should
        # land above the midpoint of his two scores.
        monkeypatch.setattr(separation, "separation_profile",
                            lambda seasons=None: pd.DataFrame([
                                _qualified("Riser", 2023, sep_score=0.0),
                                _qualified("Riser", 2024, sep_score=1.0)]))

        out = separation.separation_summary([2023, 2024])
        assert len(out) == 1
        assert out["sep_score"].iloc[0] == pytest.approx(0.40 / 0.68)
        assert out["seasons_qualified"].iloc[0] == 2

    def test_a_metric_missing_in_one_season_is_not_diluted_toward_zero(
            self, monkeypatch):
        # The weighted mean zeroes both numerator and denominator for a NaN, so a
        # metric present in only one season averages to exactly that season's
        # value. Treating the gap as a zero instead would silently halve it.
        rows = pd.DataFrame([_qualified("Half Tracked", 2023),
                             _qualified("Half Tracked", 2024, cushion=8.0)])
        rows.loc[rows["season"] == 2023, "avg_cushion"] = np.nan
        monkeypatch.setattr(separation, "separation_profile", lambda seasons=None: rows)

        out = separation.separation_summary([2023, 2024])
        assert out["avg_cushion"].iloc[0] == pytest.approx(8.0)

    def test_a_metric_missing_everywhere_stays_missing(self, monkeypatch):
        rows = pd.DataFrame([_qualified("No Cushion", 2023),
                             _qualified("No Cushion", 2024)])
        rows["avg_cushion"] = np.nan
        monkeypatch.setattr(separation, "separation_profile", lambda seasons=None: rows)

        out = separation.separation_summary([2023, 2024])
        assert out["avg_cushion"].isna().all()

    def test_one_row_per_player_not_per_season(self, monkeypatch):
        monkeypatch.setattr(separation, "separation_profile",
                            lambda seasons=None: pd.DataFrame([
                                _qualified("A Wr", 2023), _qualified("A Wr", 2024),
                                _qualified("B Wr", 2024)]))

        out = separation.separation_summary([2023, 2024])
        assert len(out) == 2
        assert set(out["name"]) == {"A Wr", "B Wr"}
