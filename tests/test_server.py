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
class TestLeagueId:
    """Normalising the league id at the tool boundary (issue #38)."""

    def test_an_integer_is_accepted(self):
        # Every league id is digit-only, so a client serialising JSON faithfully
        # sends a number. Rejecting that put the burden on the caller.
        assert server._league_id(1431833696) == "1431833696"

    def test_a_plain_string_passes_through(self):
        assert server._league_id("1431833696") == "1431833696"

    def test_surrounding_quotes_are_stripped(self):
        # What a caller reaches for after a string-type rejection. Left alone,
        # this reached the ESPN URL as %221431833696%22 and returned a bare 400.
        assert server._league_id('"1431833696"') == "1431833696"
        assert server._league_id("'1431833696'") == "1431833696"

    def test_surrounding_whitespace_is_stripped(self):
        assert server._league_id("  1431833696  ") == "1431833696"

    def test_quotes_and_whitespace_together_are_stripped(self):
        assert server._league_id('  "1431833696"  ') == "1431833696"

    def test_none_stays_none(self):
        assert server._league_id(None) is None

    def test_an_empty_or_blank_id_reads_as_absent(self):
        # Tools already have a "league_id required" branch; blank should land
        # there rather than being sent to ESPN as an empty path segment.
        assert server._league_id("") is None
        assert server._league_id("   ") is None
        assert server._league_id('""') is None

    def test_a_non_numeric_id_is_rejected_before_it_reaches_a_url(self):
        with pytest.raises(server.LeagueIdError) as exc:
            server._league_id("not-a-league")
        assert "digits" in str(exc.value)

    def test_a_url_pasted_instead_of_an_id_is_rejected(self):
        with pytest.raises(server.LeagueIdError):
            server._league_id("https://fantasy.espn.com/football/league?leagueId=1431833696")

    def test_the_rejection_names_the_offending_value(self):
        with pytest.raises(server.LeagueIdError) as exc:
            server._league_id("abc123")
        assert "abc123" in str(exc.value)


class TestPrewarmStepFailures:
    """prewarm keeping the exception message, not just its class (issue #39)."""

    def test_a_failed_step_reports_the_message_not_just_the_class(self):
        detail = "401 Client Error: Unauthorized for url: https://example.test"
        out = server._step_failure(RuntimeError(detail))
        assert "RuntimeError" in out
        assert "401" in out and "Unauthorized" in out

    def test_a_long_message_is_truncated(self):
        out = server._step_failure(RuntimeError("x" * 500))
        assert len(out) < 260

    def test_a_message_less_exception_still_names_the_class(self):
        out = server._step_failure(RuntimeError())
        assert out == "failed: RuntimeError"
