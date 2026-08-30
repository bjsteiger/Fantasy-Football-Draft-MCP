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

from ffdraft import board as bd
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


class TestSyncDraftFailures:
    """A failed sync answers with the reason (issue #46).

    `sync_espn` raised straight through the tool layer, so the client saw the
    MCP framework's generic "Error executing tool sync_draft" with the status
    code and ESPN's own words stripped off. Expired cookies, a wrong league id
    and an ESPN outage were indistinguishable from the outside.
    """

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch):
        # sync_draft builds the offence board first; that is not what is under
        # test here, so it is stubbed rather than built.
        monkeypatch.setattr(server, "_build_board", lambda *a, **k: pd.DataFrame(
            [{"name": "Josh Allen", "_key": "joshallen"}]))

    def test_an_espn_error_comes_back_as_an_answer_not_a_crash(self, monkeypatch):
        def boom(league_id, season):
            raise bd.EspnError("ESPN returned 401 for ... -- they have expired", status=401)
        monkeypatch.setattr(bd, "sync_espn", boom)

        out = json.loads(server.sync_draft(platform="espn", league_id="12345"))

        assert out["status"] == 401
        assert "401" in out["error"] and "expired" in out["error"]
        assert out["league_id"] == "12345"

    def test_an_unexpected_failure_still_names_itself(self, monkeypatch):
        def boom(league_id, season):
            raise ValueError("something else entirely")
        monkeypatch.setattr(bd, "sync_espn", boom)

        out = json.loads(server.sync_draft(platform="espn", league_id="12345"))

        assert "ValueError" in out["error"]
        assert "something else entirely" in out["error"]

    def test_a_sleeper_failure_is_answered_too(self, monkeypatch):
        def boom(draft_id):
            raise RuntimeError("sleeper is down")
        monkeypatch.setattr(bd, "sync_sleeper", boom)

        out = json.loads(server.sync_draft(platform="sleeper", draft_id="abc"))

        assert "sleeper is down" in out["error"]


class TestIdpCaches:
    """prewarm has to actually warm something (issue #44).

    The IDP step built a board and dropped it: `build_board` has no cache and
    `espn_scoring_items` is a plain requests.get, so every who_should_i_pick,
    plan_my_draft and idp_report re-read ESPN over the network and rebuilt the
    defender board from five seasons. Measured at ~0.15s of live ESPN plus
    ~0.15s of rebuild on every pick, with a network dependency and a silent
    failure mode attached to each one.
    """

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr(server, "_IDP_SCORING", {})
        monkeypatch.setattr(server, "_IDP_BOARDS", {})

    @pytest.fixture
    def espn(self, monkeypatch):
        calls = []

        def fake_items(league_id, season):
            calls.append((league_id, season))
            return [{"statId": 109, "points": 1.0}]
        monkeypatch.setattr(bd, "espn_scoring_items", fake_items)
        return calls

    @pytest.fixture
    def built(self, monkeypatch):
        builds = []

        def fake_build(weekly, scoring, **kw):
            builds.append(kw)
            return pd.DataFrame([{"name": "Fred Warner", "position": "LB", "vor": 20.0,
                                  "proj_points": 300.0, "seasons_used": 4, "pos_rank": 1}])
        from ffdraft import idp as idp_mod
        monkeypatch.setattr(idp_mod, "build_board", fake_build)
        monkeypatch.setattr(sources, "weekly_stats", lambda seasons: pd.DataFrame())
        return builds

    def test_scoring_is_read_from_espn_once(self, espn):
        for _ in range(3):
            server._idp_scoring("12345")
        assert len(espn) == 1

    def test_a_different_season_is_read_separately(self, espn):
        server._idp_scoring("12345", 2024)
        server._idp_scoring("12345", 2025)
        assert len(espn) == 2

    def test_a_failed_read_is_not_cached(self, monkeypatch):
        # Otherwise one blip during a draft means every later call is answered
        # from a cached failure, and retrying cannot help.
        calls = []

        def flaky(league_id, season):
            calls.append(season)
            if len(calls) == 1:
                raise bd.EspnError("ESPN returned 503", status=503)
            return [{"statId": 109, "points": 1.0}]
        monkeypatch.setattr(bd, "espn_scoring_items", flaky)

        with pytest.raises(bd.EspnError):
            server._idp_scoring("12345")
        assert server._idp_scoring("12345") == {"tackles_total": 1.0}

    def test_the_board_is_built_once(self, espn, built):
        league = LeagueSettings(teams=10, starters={**LeagueSettings().starters, "IDP": 1})
        for _ in range(3):
            server._idp_board(league, "12345")
        assert len(built) == 1

    @pytest.mark.parametrize("second", [
        {"teams": 12},
        {"starters": {**LeagueSettings().starters, "IDP": 2}},
    ])
    def test_a_league_that_changed_shape_builds_its_own_board(self, espn, built, second):
        base = {"teams": 10, "starters": {**LeagueSettings().starters, "IDP": 1}}
        server._idp_board(LeagueSettings(**base), "12345")
        server._idp_board(LeagueSettings(**{**base, **second}), "12345")
        assert len(built) == 2

    def test_a_different_league_builds_its_own_board(self, espn, built):
        league = LeagueSettings(teams=10, starters={**LeagueSettings().starters, "IDP": 1})
        server._idp_board(league, "12345")
        server._idp_board(league, "99999")
        assert len(built) == 2

    def test_a_different_games_floor_builds_its_own_board(self, espn, built):
        league = LeagueSettings(teams=10, starters={**LeagueSettings().starters, "IDP": 1})
        server._idp_board(league, "12345", min_games=8)
        server._idp_board(league, "12345", min_games=4)
        assert len(built) == 2

    def test_a_league_with_no_defensive_scoring_gets_an_empty_board(self, monkeypatch):
        monkeypatch.setattr(bd, "espn_scoring_items", lambda league_id, season: [])
        league = LeagueSettings(teams=10)
        assert server._idp_board(league, "12345").empty

    def test_a_failed_board_is_reported_not_silently_dropped(self, monkeypatch):
        # Returning None made a broken ESPN read look exactly like "no defender
        # is worth taking here", which on the clock is the wrong thing to believe.
        def boom(*a, **kw):
            raise bd.EspnError("ESPN returned 401", status=401)
        monkeypatch.setattr(server, "_idp_board", boom)
        league = LeagueSettings(teams=10, starters={**LeagueSettings().starters, "IDP": 1})

        out = server._idp_option(league, {"IDP": 0}, current_pick=85, league_id="12345")

        assert out is not None
        assert "401" in out["detail"]
        assert out["use"] == "idp_report"


class TestResolveLeagueId:
    """Tools fall back to the active league's stored ESPN id (#50)."""

    def _active(self, monkeypatch, league):
        monkeypatch.setitem(server._CACHE, "league", league)
        monkeypatch.setitem(server._CACHE, "weights", ModelWeights())

    def test_the_stored_id_is_used_when_none_is_passed(self, monkeypatch):
        self._active(monkeypatch, LeagueSettings(name="home", league_id="1431833696"))
        assert server._resolve_league_id(None) == "1431833696"

    def test_a_passed_id_wins_over_the_stored_one(self, monkeypatch):
        # A one-off question about another league must not need reconfiguring.
        self._active(monkeypatch, LeagueSettings(name="home", league_id="1431833696"))
        assert server._resolve_league_id("999999") == "999999"

    def test_an_integer_still_works(self, monkeypatch):
        self._active(monkeypatch, LeagueSettings(name="home"))
        assert server._resolve_league_id(1431833696) == "1431833696"

    def test_no_stored_id_and_none_passed_stays_none(self, monkeypatch):
        # Sleeper and pasted boards never need one, so this is not an error.
        self._active(monkeypatch, LeagueSettings(name="home"))
        assert server._resolve_league_id(None) is None

    def test_a_junk_stored_id_is_reported_not_sent_to_espn(self, monkeypatch):
        self._active(monkeypatch, LeagueSettings(name="home", league_id="not-a-number"))
        with pytest.raises(server.LeagueIdError):
            server._resolve_league_id(None)


class TestConfigureLeagueId:
    """configure_league is where the id gets set, and it merges like the rest."""

    @pytest.fixture
    def store(self, monkeypatch):
        saved: dict[str, tuple] = {}

        def fake_load(name=None):
            entry = saved.get(name)
            if entry is None:
                return LeagueSettings(), ModelWeights()
            league, weights = entry
            return replace(league), replace(weights)

        monkeypatch.setattr(server, "load_settings", fake_load)
        monkeypatch.setattr(server, "save_settings",
                            lambda lg, w, make_active=True: saved.__setitem__(
                                lg.name, (replace(lg), replace(w))))
        monkeypatch.setattr(server, "cfg_list_leagues", lambda: (sorted(saved), None))
        monkeypatch.setitem(server._CACHE, "league", None)
        monkeypatch.setitem(server._CACHE, "weights", None)
        return saved

    def test_it_is_stored_and_echoed(self, store):
        out = json.loads(server.configure_league(name="home", league_id="1431833696"))
        assert out["league_id"] == "1431833696"
        assert store["home"][0].league_id == "1431833696"

    def test_an_integer_is_accepted(self, store):
        server.configure_league(name="home", league_id=1431833696)
        assert store["home"][0].league_id == "1431833696"

    def test_leaving_it_out_keeps_the_stored_one(self, store):
        store["home"] = (LeagueSettings(name="home", league_id="1431833696", teams=10),
                         ModelWeights())

        server.configure_league(name="home", idp=1)

        assert store["home"][0].league_id == "1431833696"

    def test_each_league_keeps_its_own(self, store):
        server.configure_league(name="home", league_id="111")
        server.configure_league(name="work", league_id="222")
        assert store["home"][0].league_id == "111"
        assert store["work"][0].league_id == "222"

    def test_a_junk_id_is_refused_before_anything_is_saved(self, store):
        out = json.loads(server.configure_league(name="home", league_id="abc123"))
        assert "error" in out
        assert "home" not in store
