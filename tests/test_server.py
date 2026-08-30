"""The MCP tool layer's pure helpers: scoring detection, JSON row coercion,
drafted-flagging, and the pointer that sends defender questions to idp_report.

The tools themselves drive the whole board pipeline, so only the helpers that
stand on their own are covered here -- offline like the rest of the suite.
"""
import numpy as np
import pandas as pd

from ffdraft import server, sources
from ffdraft.config import LeagueSettings, Scoring


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
