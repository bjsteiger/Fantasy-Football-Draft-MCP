"""League settings, scoring, and draft-slot arithmetic."""
import pandas as pd
import pytest

from ffdraft.config import LeagueSettings, ModelWeights, Scoring
from ffdraft.features import fantasy_points


class TestScoring:
    def test_presets_differ_only_in_reception_value(self):
        assert Scoring.preset("ppr").rec == 1.0
        assert Scoring.preset("half_ppr").rec == 0.5
        assert Scoring.preset("standard").rec == 0.0

    def test_unknown_preset_falls_back_to_half(self):
        assert Scoring.preset("something_odd").rec == 0.5

    def test_applied_to_a_box_score(self):
        row = pd.DataFrame([{
            "receptions": 8, "receiving_yards": 100, "receiving_tds": 1,
            "rushing_yards": 20, "position": "WR",
        }])
        ppr = float(fantasy_points(row, Scoring.preset("ppr")).iloc[0])
        half = float(fantasy_points(row, Scoring.preset("half_ppr")).iloc[0])
        std = float(fantasy_points(row, Scoring.preset("standard")).iloc[0])
        # 10 receiving + 6 TD + 2 rushing = 18, plus reception credit
        assert std == pytest.approx(18.0)
        assert half == pytest.approx(22.0)   # + 8 * 0.5
        assert ppr == pytest.approx(26.0)    # + 8 * 1.0

    def test_te_premium_only_touches_tight_ends(self):
        rows = pd.DataFrame([
            {"receptions": 6, "position": "TE"},
            {"receptions": 6, "position": "WR"},
        ])
        pts = fantasy_points(rows, Scoring.preset("ppr"), te_bonus=0.5)
        assert float(pts.iloc[0]) - float(pts.iloc[1]) == pytest.approx(3.0)


class TestDraftSlots:
    def test_snake_order_reverses_on_even_rounds(self):
        lg = LeagueSettings(teams=12, draft_slot=1, rounds=4)
        assert lg.picks_for_slot() == [1, 24, 25, 48]

    def test_last_slot_gets_the_turn(self):
        lg = LeagueSettings(teams=10, draft_slot=10, rounds=4)
        assert lg.picks_for_slot() == [10, 11, 30, 31]

    def test_linear_draft_does_not_reverse(self):
        lg = LeagueSettings(teams=12, draft_slot=3, rounds=3, snake=False)
        assert lg.picks_for_slot() == [3, 15, 27]

    @pytest.mark.parametrize("teams,slot", [(10, 4), (12, 6), (13, 11), (14, 1)])
    def test_every_pick_is_in_range_and_unique(self, teams, slot):
        lg = LeagueSettings(teams=teams, draft_slot=slot, rounds=16)
        picks = lg.picks_for_slot()
        assert len(picks) == len(set(picks)) == 16
        assert all(1 <= p <= teams * 16 for p in picks)
        assert picks == sorted(picks)


class TestReplacementLevel:
    def test_scales_with_league_size(self):
        """The last startable back in a 10-team league is a better player than in
        a 13-team league, so replacement level must sit higher."""
        small = LeagueSettings(teams=10).replacement_ranks()
        large = LeagueSettings(teams=13).replacement_ranks()
        for pos in ("RB", "WR", "TE", "QB"):
            assert small[pos] < large[pos], pos

    def test_superflex_makes_quarterbacks_scarce(self):
        one_qb = LeagueSettings(teams=12).replacement_ranks()["QB"]
        superflex = LeagueSettings(teams=12, superflex=1).replacement_ranks()["QB"]
        assert superflex > one_qb * 1.5

    def test_all_positions_positive(self):
        for teams in (8, 10, 12, 14, 16):
            ranks = LeagueSettings(teams=teams).replacement_ranks()
            assert all(v >= 1 for v in ranks.values())


class TestCacheKey:
    def test_same_settings_share_a_board(self):
        a = LeagueSettings(name="home", teams=12, draft_slot=1, scoring=Scoring.preset("ppr"))
        b = LeagueSettings(name="work", teams=12, draft_slot=9, scoring=Scoring.preset("ppr"))
        # Draft slot doesn't change projections, so the board is reusable.
        assert a.cache_key() == b.cache_key()

    @pytest.mark.parametrize("override", [
        {"teams": 13},
        {"scoring": Scoring.preset("half_ppr")},
        {"superflex": 1},
        {"te_premium_bonus": 0.5},
        {"starters": {"QB": 2, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}},
    ])
    def test_anything_affecting_projections_forces_a_new_board(self, override):
        settings = {"teams": 12, "scoring": Scoring.preset("ppr")}
        base = LeagueSettings(**settings)
        other = LeagueSettings(**{**settings, **override})
        assert base.cache_key() != other.cache_key()


class TestModelWeights:
    def test_defaults_are_bounded(self):
        w = ModelWeights()
        for name in ("oline", "pace_volume", "schedule", "injury", "age", "separation"):
            assert 0 <= getattr(w, name) <= 0.5
        assert 0 <= w.consistency_weight <= 1


class TestUnmodelledSlotsStayContained:
    """An IDP slot is tracked in starters but must never reach the model.

    This is the same contract K and DST already have: the tool needs to know the
    slot exists so round arithmetic is right, but LB is not in FANTASY_POSITIONS
    and nothing position-keyed should grow an LB entry. Widening that reach is
    what would fabricate fpa_LB / sos_LB_z -- "points a defense allows to
    opposing linebackers" -- and multiply real QB/RB/WR/TE projections by it.
    """

    def test_replacement_ranks_ignores_the_idp_slot(self):
        without = LeagueSettings(teams=10)
        with_lb = LeagueSettings(teams=10, starters={
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "IDP": 1,
        })
        assert with_lb.replacement_ranks() == without.replacement_ranks()

    def test_replacement_ranks_never_emits_an_idp_key(self):
        lg = LeagueSettings(starters={
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "IDP": 1,
        })
        assert "IDP" not in lg.replacement_ranks()

    def test_positional_need_never_emits_an_idp_key(self):
        from ffdraft.model import _positional_need
        lg = LeagueSettings(starters={
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "IDP": 1,
        })
        need = _positional_need(lg, roster={"RB": 1, "IDP": 1})
        assert "IDP" not in need


class TestModellableRounds:
    """Rounds the model can actually make a recommendation for.

    A draft round spent on a kicker, a defence unit or a defensive player is a
    real round -- it consumes a pick -- but the model projects none of those, so
    simulating it means inventing a skill-position pick the drafter will never
    get to make.
    """

    def test_subtracts_kicker_and_defence(self):
        lg = LeagueSettings(rounds=16)  # default starters carry K=1, DST=1
        assert lg.modellable_rounds() == 14

    def test_subtracts_idp_slots_too(self):
        lg = LeagueSettings(rounds=16, starters={
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "IDP": 1,
        })
        assert lg.modellable_rounds() == 13

    def test_counts_multiple_idp_slots(self):
        # A DL/LB/DB league gives up three rounds, not one.
        lg = LeagueSettings(rounds=16, starters={
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1, "IDP": 3,
        })
        assert lg.modellable_rounds() == 11

    def test_league_without_kicker_or_defence_loses_no_rounds(self):
        lg = LeagueSettings(rounds=15, starters={
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 0, "DST": 0, "IDP": 0,
        })
        assert lg.modellable_rounds() == 15

    def test_never_returns_less_than_one(self):
        # A pathological config must not produce a zero or negative round count.
        lg = LeagueSettings(rounds=2, starters={"K": 1, "DST": 1, "IDP": 5})
        assert lg.modellable_rounds() == 1
