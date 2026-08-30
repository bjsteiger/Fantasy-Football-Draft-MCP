"""The MCP tool layer's pure helpers: scoring detection, JSON row coercion,
drafted-flagging, and the pointer that sends defender questions to idp_report.

The tools themselves drive the whole board pipeline, so only the helpers that
stand on their own are covered here -- offline like the rest of the suite.
"""
import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from ffdraft import server, sources
from ffdraft.config import LeagueSettings, ModelWeights, Scoring


class TestScoringLabel:
    def test_each_preset_is_recognised(self):
        for label in ("ppr", "half_ppr", "standard"):
            league = LeagueSettings(scoring=Scoring.preset(label))
            assert server._scoring_label(league) == label

    def test_an_unusual_reception_value_falls_to_the_nearest_band(self):
        assert server._scoring_label(
            LeagueSettings(scoring=Scoring(rec=0.5))) == "half_ppr"
        assert server._scoring_label(
            LeagueSettings(scoring=Scoring(rec=1.0))) == "ppr"

    def test_a_tiny_reception_value_is_treated_as_standard(self):
        # The conservative choice: assume no reception credit rather than
        # inventing one the league does not actually award.
        assert server._scoring_label(
            LeagueSettings(scoring=Scoring(rec=0.1))) == "standard"

    def test_a_richer_than_ppr_league_still_reads_as_ppr(self):
        assert server._scoring_label(
            LeagueSettings(scoring=Scoring(rec=1.5))) == "ppr"


class TestRows:
    """Board rows have to survive json.dumps, which numpy scalars do not."""

    def test_numpy_scalars_become_plain_python(self):
        df = pd.DataFrame([{"a": np.float64(1.5), "b": np.int64(3),
                            "c": np.bool_(True)}])
        row = server._rows(df, ["a", "b", "c"], 1)[0]
        assert isinstance(row["a"], float) and not isinstance(row["a"], np.floating)
        assert isinstance(row["b"], int) and not isinstance(row["b"], np.integer)
        assert isinstance(row["c"], bool) and not isinstance(row["c"], np.bool_)

    def test_non_finite_floats_become_null(self):
        # NaN and inf are not valid JSON; emitting them produces a payload the
        # caller cannot parse.
        df = pd.DataFrame([{"a": np.nan, "b": np.inf, "c": -np.inf}])
        assert server._rows(df, ["a", "b", "c"], 1)[0] == {"a": None, "b": None,
                                                           "c": None}

    def test_floats_are_rounded(self):
        df = pd.DataFrame([{"a": 1.23456789}])
        assert server._rows(df, ["a"], 1)[0]["a"] == 1.235

    def test_a_missing_column_comes_back_as_none_rather_than_raising(self):
        # Different frames reach _rows with different columns.
        df = pd.DataFrame([{"a": 1.0}])
        assert server._rows(df, ["a", "nope"], 1)[0]["nope"] is None

    def test_only_the_requested_columns_are_emitted(self):
        df = pd.DataFrame([{"a": 1.0, "secret": 2.0}])
        assert set(server._rows(df, ["a"], 1)[0]) == {"a"}

    def test_the_row_limit_is_respected(self):
        df = pd.DataFrame([{"a": float(i)} for i in range(10)])
        assert len(server._rows(df, ["a"], 3)) == 3

    def test_an_empty_frame_yields_no_rows(self):
        assert server._rows(pd.DataFrame(columns=["a"]), ["a"], 5) == []


class _FakeState:
    def __init__(self, keys):
        self._keys = set(keys)

    def taken_keys(self):
        return self._keys


class TestMarkDrafted:
    def test_taken_players_are_flagged_and_others_are_not(self):
        from ffdraft.names import normalize
        b = pd.DataFrame([{"_key": normalize("Josh Allen")},
                          {"_key": normalize("Bijan Robinson")}])
        out = server._mark_drafted(b, _FakeState({normalize("Josh Allen")}))
        assert list(out["drafted"]) == [True, False]

    def test_the_original_board_is_not_mutated(self):
        # The board is cached and reused across tool calls; writing a per-draft
        # column onto it in place would leak one draft's state into the next.
        from ffdraft.names import normalize
        b = pd.DataFrame([{"_key": normalize("Josh Allen")}])
        server._mark_drafted(b, _FakeState({normalize("Josh Allen")}))
        assert "drafted" not in b.columns

    def test_an_empty_draft_marks_nobody(self):
        from ffdraft.names import normalize
        b = pd.DataFrame([{"_key": normalize("Josh Allen")}])
        assert not server._mark_drafted(b, _FakeState(set()))["drafted"].any()


class TestIdpPointer:
    """Asking the offence board about a defender used to dead-end in a way that
    read as a different problem: "no match for 'Fred Warner'" says the name is
    wrong, not that defenders live on another board."""

    def test_a_defensive_position_is_redirected(self):
        out = server._idp_pointer(position="LB")
        assert out is not None
        assert out["use_instead"] == "idp_report"

    def test_the_position_check_is_case_insensitive(self):
        assert server._idp_pointer(position="lb") is not None

    def test_an_offensive_position_is_left_alone(self):
        assert server._idp_pointer(position="WR") is None

    def test_a_known_defender_name_is_redirected(self, monkeypatch):
        from ffdraft import idp as idp_mod
        monkeypatch.setattr(idp_mod, "defender_names", lambda w: {"Fred Warner"})
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: pd.DataFrame())

        out = server._idp_pointer(name="Fred Warner")
        assert out is not None and out["use_instead"] == "idp_report"

    def test_an_offensive_player_name_is_left_alone(self, monkeypatch):
        from ffdraft import idp as idp_mod
        monkeypatch.setattr(idp_mod, "defender_names", lambda w: {"Fred Warner"})
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: pd.DataFrame())

        assert server._idp_pointer(name="Josh Allen") is None

    def test_an_unreachable_defender_list_does_not_break_the_caller(self, monkeypatch):
        # The pointer is a convenience. If the roster data can't be loaded the
        # tool should answer normally, not fail.
        from ffdraft import idp as idp_mod

        def boom(w):
            raise RuntimeError("weekly stats unreachable")
        monkeypatch.setattr(idp_mod, "defender_names", boom)
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: pd.DataFrame())

        assert server._idp_pointer(name="Anyone") is None

    def test_no_name_and_no_position_is_not_a_redirect(self):
        assert server._idp_pointer() is None


class TestConfigureLeague:
    """Reconfiguring a league must not undo model tuning (issue #32).

    `configure_league` built a fresh `ModelWeights` on every call, so every
    weight it does not take as a parameter -- schedule, injury, oline, td_luck,
    qb_boost -- snapped back to its default the moment anyone reconfigured the
    league to change `idp`, `rounds` or `draft_slot`. Nothing in the response
    said so. The store is faked here so the suite stays offline and never
    touches the real leagues.json.
    """

    @pytest.fixture
    def store(self, monkeypatch):
        saved: dict[str, tuple] = {}

        def fake_load(name=None):
            entry = saved.get(name)
            if entry is None:
                return LeagueSettings(), ModelWeights()
            league, weights = entry
            return replace(league), replace(weights)

        def fake_save(league, weights, make_active=True):
            saved[league.name] = (replace(league), replace(weights))

        monkeypatch.setattr(server, "load_settings", fake_load)
        monkeypatch.setattr(server, "save_settings", fake_save)
        monkeypatch.setattr(server, "cfg_list_leagues", lambda: (sorted(saved), None))
        monkeypatch.setitem(server._CACHE, "league", None)
        monkeypatch.setitem(server._CACHE, "weights", None)
        return saved

    def test_tuned_weights_survive_a_reconfigure(self, store):
        # The exact sequence from the issue: tune via model_settings, then call
        # configure_league again to change one league-shape setting.
        tuned = ModelWeights(schedule=0.0, injury=0.2, qb_boost=0.12)
        store["rudy"] = (LeagueSettings(name="rudy", teams=10), tuned)

        server.configure_league(name="rudy", teams=10, idp=1)

        _, weights = store["rudy"]
        assert weights.schedule == 0.0
        assert weights.injury == 0.2
        assert weights.qb_boost == 0.12

    def test_an_omitted_consistency_weight_keeps_the_tuned_one(self, store):
        store["rudy"] = (LeagueSettings(name="rudy", teams=10),
                         ModelWeights(consistency_weight=0.6))

        server.configure_league(name="rudy", teams=10, idp=1)

        assert store["rudy"][1].consistency_weight == 0.6

    def test_an_explicit_consistency_weight_is_still_applied(self, store):
        store["rudy"] = (LeagueSettings(name="rudy", teams=10),
                         ModelWeights(consistency_weight=0.6, schedule=0.0))

        server.configure_league(name="rudy", teams=10, consistency_weight=0.1)

        weights = store["rudy"][1]
        assert weights.consistency_weight == 0.1
        assert weights.schedule == 0.0   # the rest still carries over

    def test_league_shape_survives_a_one_setting_change(self, store):
        # Issue #37: the same trap as #32, for the settings you can see. A call
        # that names only `idp` must not turn a 10-team full-PPR league into the
        # 12-team half-PPR default.
        store["rudy"] = (LeagueSettings(name="rudy", teams=10, rounds=16, draft_slot=5,
                                        scoring=Scoring.preset("ppr")),
                         ModelWeights())

        server.configure_league(name="rudy", idp=1)

        league, _ = store["rudy"]
        assert league.teams == 10
        assert league.draft_slot == 5
        assert league.scoring.rec == 1.0
        assert league.starters["IDP"] == 1

    def test_other_roster_slots_are_left_alone(self, store):
        starters = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 2,
                    "K": 1, "DST": 1, "IDP": 0}
        store["rudy"] = (LeagueSettings(name="rudy", teams=10, starters=starters),
                         ModelWeights())

        server.configure_league(name="rudy", idp=1)

        kept = store["rudy"][0].starters
        assert kept["WR"] == 3 and kept["FLEX"] == 2
        assert kept["IDP"] == 1

    def test_what_you_pass_still_wins(self, store):
        store["rudy"] = (LeagueSettings(name="rudy", teams=10, superflex=0), ModelWeights())

        server.configure_league(name="rudy", teams=13, scoring="standard", superflex=1)

        league, _ = store["rudy"]
        assert league.teams == 13
        assert league.scoring.rec == 0.0
        assert league.superflex == 1

    def test_a_slot_left_stranded_by_a_smaller_league_is_refused(self, store):
        # draft_slot is checked against the team count the league ends up with,
        # not the one that happened to be passed. Slot 12 cannot survive a move
        # to a 10-team league.
        store["rudy"] = (LeagueSettings(name="rudy", teams=14, draft_slot=12), ModelWeights())

        out = json.loads(server.configure_league(name="rudy", teams=10))

        assert "error" in out
        assert store["rudy"][0].teams == 14   # nothing was saved

    def test_the_response_shows_the_settings_that_stuck(self, store):
        store["rudy"] = (LeagueSettings(name="rudy", teams=10, draft_slot=5,
                                        scoring=Scoring.preset("ppr")), ModelWeights())

        out = json.loads(server.configure_league(name="rudy", idp=1))

        assert out["status"] == "updated existing league"
        assert out["teams"] == 10 and out["your_slot"] == 5
        assert out["scoring"] == "ppr"
        assert out["starters"]["IDP"] == 1

    def test_a_new_league_says_so_and_uses_the_documented_defaults(self, store):
        out = json.loads(server.configure_league(name="brand_new"))

        assert out["status"] == "created new league"
        assert out["teams"] == 12 and out["your_slot"] == 6
        assert out["scoring"] == "half_ppr"
        assert store["brand_new"][0].rounds == 16

    def test_a_new_league_starts_from_defaults(self, store):
        # A name that has never been configured must not inherit another
        # league's tuning -- separate leagues keep separate models.
        store["rudy"] = (LeagueSettings(name="rudy"), ModelWeights(schedule=0.0))

        server.configure_league(name="brand_new", teams=12)

        assert store["brand_new"][1].schedule == ModelWeights().schedule

    def test_the_response_says_what_the_weights_are(self, store):
        store["rudy"] = (LeagueSettings(name="rudy", teams=10),
                         ModelWeights(schedule=0.0))

        out = json.loads(server.configure_league(name="rudy", teams=10, idp=1))

        assert out["weights"]["schedule"] == 0.0
        assert out["weights"]["consistency_weight"] == ModelWeights().consistency_weight
