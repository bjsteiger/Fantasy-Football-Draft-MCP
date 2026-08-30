"""Board assembly: id crosswalk, ESPN league settings, and pasted-board parsing.

Offline like the rest of the suite -- weekly_rosters and every ESPN endpoint are
substituted, so nothing here touches the network.
"""
import json

import pandas as pd
import pytest
import requests

from ffdraft import board, sources


class TestIdCrosswalk:
    def test_prefers_row_with_espn_id_over_earlier_null_row(self, monkeypatch):
        # weekly_rosters has one row per player per week; espn_id/sleeper_id are
        # only populated in some of those snapshots. A player whose earliest row
        # happens to lack espn_id must still resolve to the ID a later row has --
        # this is what silently dropped Bijan Robinson, Jahmyr Gibbs and De'Von
        # Achane (~23% of a real draft) before the fix.
        rosters = pd.DataFrame([
            {"gsis_id": "00-0038542", "espn_id": None, "sleeper_id": "9999",
             "full_name": "Bijan Robinson", "position": "RB"},
            {"gsis_id": "00-0038542", "espn_id": "4430807", "sleeper_id": None,
             "full_name": "Bijan Robinson", "position": "RB"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)

        x = board._id_crosswalk().set_index("gsis_id")
        assert x.loc["00-0038542", "espn_id"] == "4430807"
        assert x.loc["00-0038542", "sleeper_id"] == "9999"

    def test_one_row_per_gsis_id(self, monkeypatch):
        rosters = pd.DataFrame([
            {"gsis_id": "00-0038542", "espn_id": None, "sleeper_id": None,
             "full_name": "Bijan Robinson", "position": "RB"},
            {"gsis_id": "00-0038542", "espn_id": "4430807", "sleeper_id": None,
             "full_name": "Bijan Robinson", "position": "RB"},
            {"gsis_id": "00-0038542", "espn_id": None, "sleeper_id": "9999",
             "full_name": "Bijan Robinson", "position": "RB"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)

        x = board._id_crosswalk()
        assert len(x) == 1

    def test_drops_players_with_no_gsis_id(self, monkeypatch):
        rosters = pd.DataFrame([
            {"gsis_id": None, "espn_id": "123", "sleeper_id": None,
             "full_name": "No Gsis Guy", "position": "WR"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)

        assert board._id_crosswalk().empty


class TestEspnRosterSlots:
    """Translating ESPN's lineupSlotCounts into LeagueSettings.starters.

    Tested against the raw slot-count dict rather than the live endpoint, so this
    stays offline like the rest of the suite.
    """

    def test_base_and_flex_slots_map_to_starters(self):
        # 1 QB, 2 RB, 2 WR, 1 TE, 1 DST, 1 K, 1 FLEX(23), 5 bench(20), 1 IR(21)
        counts = {"0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1,
                  "23": 1, "20": 5, "21": 1}
        starters, _ = board.starters_from_slot_counts(counts)
        assert starters["QB"] == 1
        assert starters["RB"] == 2
        assert starters["WR"] == 2
        assert starters["TE"] == 1
        assert starters["FLEX"] == 1
        assert starters["K"] == 1
        assert starters["DST"] == 1
        assert starters["IDP"] == 0

    def test_idp_slot_is_counted_not_dropped(self):
        # Slot 10 is a linebacker. It used to fall through both the base and flex
        # branches and vanish from starters, while still being counted in
        # roster_slots -> rounds. mock_draft then subtracted only K and DST by
        # name, so a league with an LB slot was simulated one round too long.
        counts = {"0": 1, "2": 2, "4": 2, "6": 1, "10": 1, "16": 1, "17": 1,
                  "23": 1, "20": 5, "21": 1}
        starters, _ = board.starters_from_slot_counts(counts)
        assert starters["IDP"] == 1

    def test_unknown_defensive_slots_are_counted_without_being_named(self):
        # A DL/DB/CB/S/edge league uses slot ids this module has never verified.
        # Round arithmetic must still be right, so they count as unmodelled
        # starters rather than being dropped or guessed at.
        counts = {"0": 1, "2": 2, "8": 2, "12": 2, "13": 1, "24": 1, "20": 5}
        starters, _ = board.starters_from_slot_counts(counts)
        assert starters["IDP"] == 6

    def test_bench_and_ir_are_not_starting_slots(self):
        counts = {"0": 1, "20": 5, "21": 1}
        starters, _ = board.starters_from_slot_counts(counts)
        assert starters["QB"] == 1
        assert sum(v for k, v in starters.items() if k != "QB") == 0

    def test_roster_slots_counts_every_slot_including_unmodelled_ones(self):
        _, roster_slots = board.starters_from_slot_counts({"0": 1, "10": 1, "20": 5})
        assert roster_slots == 7

    def test_league_with_no_idp_slot_reports_zero_not_missing(self):
        # Callers do starters["IDP"], so the key must always exist.
        starters, _ = board.starters_from_slot_counts({"0": 1, "2": 2})
        assert starters["IDP"] == 0


class TestIdpSlotLabels:
    def test_names_the_slot_id_it_has_verified(self):
        assert board.idp_slot_labels({"0": 1, "10": 1, "20": 5}) == {"LB": 1}

    def test_unverified_slot_is_reported_raw_rather_than_guessed(self):
        # Better to surface "slot_13" than to assert it is a safety and be wrong.
        assert board.idp_slot_labels({"13": 1}) == {"slot_13": 1}

    def test_offence_only_league_has_no_idp_labels(self):
        assert board.idp_slot_labels({"0": 1, "2": 2, "23": 1, "20": 5}) == {}


class _FakeResponse:
    """Stand-in for requests.Response covering just what board.py calls."""

    def __init__(self, payload, status=200, text=None):
        self._payload = payload
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is _NOT_JSON:
            raise ValueError("not json")
        return self._payload


_NOT_JSON = object()


class TestEspnScoringItems:
    """A league's raw scoring rules. idp.py builds its whole defensive scoring
    map from this, so an empty or malformed response has to be visible."""

    def _patch(self, monkeypatch, payload, status=200):
        calls = {}

        def fake_get(url, **kw):
            calls["url"] = url
            calls.update(kw)
            return _FakeResponse(payload, status)

        monkeypatch.setattr(board.requests, "get", fake_get)
        return calls

    def test_returns_the_scoring_items_unparsed(self, monkeypatch):
        # Deliberately returned raw: callers decide what the statIds mean.
        items = [{"statId": 53, "points": 1.0}, {"statId": 99, "points": 2.0}]
        self._patch(monkeypatch, {"settings": {"scoringSettings": {
            "scoringItems": items}}})

        assert board.espn_scoring_items("12345") == items

    def test_a_response_with_no_settings_yields_no_items(self, monkeypatch):
        # The quiet failure mode: a league whose settings can't be read returns
        # an empty list, and idp.py scores every defender at zero rather than
        # raising. Pinned here so the shape of that fallback can't drift.
        self._patch(monkeypatch, {})
        assert board.espn_scoring_items("12345") == []

    def test_settings_without_scoring_settings_yields_no_items(self, monkeypatch):
        self._patch(monkeypatch, {"settings": {}})
        assert board.espn_scoring_items("12345") == []

    def test_explicit_nulls_are_treated_as_empty_not_returned(self, monkeypatch):
        # ESPN sends null rather than omitting the key on some leagues, which a
        # plain .get(key, {}) would hand straight back to the caller as None.
        self._patch(monkeypatch, {"settings": {"scoringSettings":
                                               {"scoringItems": None}}})
        assert board.espn_scoring_items("12345") == []

    def test_an_http_error_is_raised_not_swallowed(self, monkeypatch):
        # A 401 means the cookies are wrong. Returning [] there would look
        # exactly like a league with no scoring rules.
        self._patch(monkeypatch, {}, status=401)
        with pytest.raises(board.EspnError):
            board.espn_scoring_items("12345")

    def test_private_league_cookies_are_sent_with_swid_braced(self, monkeypatch):
        # ESPN rejects a SWID without its surrounding braces.
        calls = self._patch(monkeypatch, {"settings": {}})
        board.espn_scoring_items("12345", swid="ABC-DEF", espn_s2="s2value")
        assert calls["cookies"] == {"SWID": "{ABC-DEF}", "espn_s2": "s2value"}

    def test_an_already_braced_swid_is_not_double_wrapped(self, monkeypatch):
        calls = self._patch(monkeypatch, {"settings": {}})
        board.espn_scoring_items("12345", swid="{ABC-DEF}", espn_s2="s2value")
        assert calls["cookies"]["SWID"] == "{ABC-DEF}"

    def test_a_public_league_sends_no_cookies(self, monkeypatch):
        monkeypatch.delenv("ESPN_SWID", raising=False)
        monkeypatch.delenv("ESPN_S2", raising=False)
        calls = self._patch(monkeypatch, {"settings": {}})
        board.espn_scoring_items("12345")
        assert calls["cookies"] == {}

    def test_half_a_credential_pair_is_not_sent(self, monkeypatch):
        # Both cookies are required; sending one alone just looks anonymous.
        monkeypatch.delenv("ESPN_S2", raising=False)
        calls = self._patch(monkeypatch, {"settings": {}})
        board.espn_scoring_items("12345", swid="ABC-DEF")
        assert calls["cookies"] == {}

    def test_credentials_fall_back_to_the_environment(self, monkeypatch):
        monkeypatch.setenv("ESPN_SWID", "ENV-SWID")
        monkeypatch.setenv("ESPN_S2", "env-s2")
        calls = self._patch(monkeypatch, {"settings": {}})
        board.espn_scoring_items("12345")
        assert calls["cookies"] == {"SWID": "{ENV-SWID}", "espn_s2": "env-s2"}

    def test_the_league_and_season_land_in_the_url(self, monkeypatch):
        calls = self._patch(monkeypatch, {"settings": {}})
        board.espn_scoring_items("98765", season=2024)
        assert "/seasons/2024/" in calls["url"]
        assert calls["url"].endswith("/leagues/98765")
        assert calls["params"] == {"view": "mSettings"}


class TestParsePastedBoard:
    """Best-effort parse of whatever shape someone pastes into a chat."""

    def test_a_numbered_list(self):
        assert board.parse_pasted_board(
            "1. Josh Allen\n2. Bijan Robinson") == ["Josh Allen", "Bijan Robinson"]

    def test_round_and_pick_prefixes(self):
        assert board.parse_pasted_board(
            "Round 3 Pick 7 - Puka Nacua") == ["Puka Nacua"]

    def test_a_comma_inside_a_round_pick_prefix_does_not_split_the_entry(self):
        # The docstring advertises this shape, but the comma split used to sever
        # it before the prefix regex ran, yielding "Pick 7 - Puka Nacua" -- a
        # name that resolves to nobody, so the real player stayed on the board
        # as available while a phantom pick was recorded.
        assert board.parse_pasted_board(
            "Round 3, Pick 7 - Puka Nacua") == ["Puka Nacua"]
        assert board.parse_pasted_board(
            "Round 12, Pick 1 - Josh Allen") == ["Josh Allen"]

    def test_decimal_pick_notation_is_stripped_whole(self):
        # The prefix alternation is ordered: R?\d+[.):] matched the "3." of
        # "3.07" first and left "07 Puka Nacua" behind.
        assert board.parse_pasted_board("3.07 Puka Nacua") == ["Puka Nacua"]
        assert board.parse_pasted_board("R3.07 Puka Nacua") == ["Puka Nacua"]

    def test_a_comma_separated_run(self):
        assert board.parse_pasted_board(
            "Ja'Marr Chase, Justin Jefferson") == ["Ja'Marr Chase",
                                                   "Justin Jefferson"]

    def test_a_trailing_position_is_stripped(self):
        assert board.parse_pasted_board("Puka Nacua (WR)") == ["Puka Nacua"]
        assert board.parse_pasted_board("Saquon Barkley - RB") == ["Saquon Barkley"]

    def test_a_trailing_team_code_is_stripped(self):
        assert board.parse_pasted_board("Josh Allen BUF") == ["Josh Allen"]

    def test_a_single_word_is_not_a_player(self):
        # Headings like "Bench" or a stray token would otherwise be recorded as
        # picks, marking a real player drafted and removing him from the board.
        assert board.parse_pasted_board("Bench\nQB\nJosh Allen") == ["Josh Allen"]

    def test_blank_lines_and_separators_are_skipped(self):
        assert board.parse_pasted_board("\n\n;  ,\nJosh Allen\n\n") == ["Josh Allen"]

    def test_nothing_usable_yields_an_empty_list(self):
        assert board.parse_pasted_board("") == []


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """DraftState writes JSON per league; keep it out of the real state dir."""
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    return tmp_path


def _league(**kw):
    from ffdraft.config import LeagueSettings
    return LeagueSettings(**kw)


class TestDraftStateSlots:
    def test_odd_rounds_run_forward_and_even_rounds_run_back(self, state_dir):
        st = board.DraftState(_league(teams=12, snake=True))
        assert st.slot_for_pick(1) == 1
        assert st.slot_for_pick(12) == 12
        assert st.slot_for_pick(13) == 12   # round 2 reverses
        assert st.slot_for_pick(24) == 1

    def test_a_non_snake_league_runs_forward_every_round(self, state_dir):
        st = board.DraftState(_league(teams=12, snake=False))
        assert st.slot_for_pick(13) == 1
        assert st.slot_for_pick(24) == 12


class TestDraftStateRecording:
    def test_recording_advances_the_clock(self, state_dir):
        st = board.DraftState(_league(teams=12))
        assert st.on_the_clock == 1
        st.record("Josh Allen")
        assert st.on_the_clock == 2

    def test_re_recording_an_overall_replaces_it_rather_than_duplicating(
            self, state_dir):
        # Correcting a mistyped pick must not leave both players marked drafted;
        # the wrong one would stay off the board for the rest of the draft.
        st = board.DraftState(_league(teams=12))
        st.record("Wrong Guy", overall=4)
        st.record("Right Guy", overall=4)
        assert [p["name"] for p in st.picks] == ["Right Guy"]

    def test_picks_are_kept_in_overall_order(self, state_dir):
        st = board.DraftState(_league(teams=12))
        st.record("Third", overall=3)
        st.record("First", overall=1)
        assert [p["overall"] for p in st.picks] == [1, 3]

    def test_undo_returns_and_removes_the_last_pick(self, state_dir):
        st = board.DraftState(_league(teams=12))
        st.record("Josh Allen")
        assert st.undo()["name"] == "Josh Allen"
        assert st.picks == []

    def test_undo_on_an_empty_draft_is_a_no_op(self, state_dir):
        assert board.DraftState(_league(teams=12)).undo() is None

    def test_reset_clears_every_pick(self, state_dir):
        st = board.DraftState(_league(teams=12))
        st.record("A Guy")
        st.reset()
        assert st.picks == []

    def test_taken_keys_are_normalised_for_matching(self, state_dir):
        # Picks arrive from chat and from platform sync in different spellings.
        st = board.DraftState(_league(teams=12))
        st.record("Amon-Ra St. Brown")
        from ffdraft.names import normalize
        assert normalize("Amon-Ra St. Brown") in st.taken_keys()


class TestDraftStatePersistence:
    def test_picks_survive_a_reload(self, state_dir):
        league = _league(name="myleague", teams=12)
        board.DraftState(league).record("Josh Allen")
        assert [p["name"] for p in board.DraftState(league).picks] == ["Josh Allen"]

    def test_your_slot_comes_from_the_league_not_the_saved_file(self, state_dir):
        # Reconfiguring to pick 11 and still being advised for pick 6, because
        # the stale saved value won, is the bug this guards.
        league = _league(name="myleague", teams=12, draft_slot=6)
        board.DraftState(league).record("Josh Allen")

        moved = _league(name="myleague", teams=12, draft_slot=11)
        assert board.DraftState(moved).my_slot == 11

    def test_picks_from_a_different_league_size_are_discarded(self, state_dir):
        # Pick 13 means something different in a 10-team draft than a 12-team
        # one; reinterpreting them would silently mis-assign every pick.
        league = _league(name="myleague", teams=12)
        board.DraftState(league).record("Josh Allen")

        resized = _league(name="myleague", teams=10)
        assert board.DraftState(resized).picks == []

    def test_two_leagues_do_not_share_state(self, state_dir):
        board.DraftState(_league(name="league-a", teams=12)).record("Josh Allen")
        assert board.DraftState(_league(name="league-b", teams=12)).picks == []


class TestDraftStateQueries:
    def test_my_picks_follow_the_snake(self, state_dir):
        st = board.DraftState(_league(teams=12, rounds=3, draft_slot=3))
        assert st.my_picks() == [3, 22, 27]

    def test_next_pick_skips_ones_already_gone(self, state_dir):
        st = board.DraftState(_league(teams=12, rounds=3, draft_slot=3))
        for i in range(1, 5):           # picks 1-4 made, so slot 3 is spent
            st.record(f"Player {i}", overall=i)
        assert st.next_pick_for_me() == 22
        assert st.pick_after_next() == 27

    def test_no_picks_left_reports_none(self, state_dir):
        st = board.DraftState(_league(teams=12, rounds=1, draft_slot=3))
        assert st.next_pick_for_me(after=99) is None
        assert board.DraftState(
            _league(teams=12, rounds=1, draft_slot=3)).pick_after_next() is None

    def test_my_roster_counts_only_my_own_picks_by_position(self, state_dir):
        st = board.DraftState(_league(teams=12, draft_slot=1))
        st.record("My Back", overall=1)        # slot 1 -- mine
        st.record("Their Back", overall=2)     # slot 2 -- not mine
        from ffdraft.names import normalize
        table = pd.DataFrame([
            {"_key": normalize("My Back"), "position": "RB"},
            {"_key": normalize("Their Back"), "position": "RB"},
        ])
        assert st.my_roster(table) == {"RB": 1}

    def test_summary_reports_the_wait_until_your_turn(self, state_dir):
        st = board.DraftState(_league(teams=12, rounds=3, draft_slot=3))
        s = st.summary()
        assert s["picks_made"] == 0
        assert s["on_the_clock"] == 1
        assert s["round"] == 1
        assert s["my_next_pick"] == 3
        assert s["picks_until_my_turn"] == 2


def _board_rows(*rows):
    return pd.DataFrame(list(rows))


def _bp(name, position="RB", pos_rank=1, overall_rank=1, **extra):
    row = {"name": name, "position": position, "pos_rank": pos_rank,
           "overall_rank": overall_rank}
    row.update(extra)
    return row


class TestAttachAdp:
    def test_a_matched_player_takes_the_consensus_price(self):
        from ffdraft.names import normalize
        b = _board_rows(_bp("Bijan Robinson", overall_rank=4))
        adp = pd.DataFrame([{"_key": normalize("Bijan Robinson"), "adp": 6.5}])

        out = board.attach_adp(b, adp)
        assert out["adp"].iloc[0] == 6.5
        assert out["adp_source"].iloc[0] == "consensus"

    def test_an_unmatched_player_falls_back_to_the_positional_curve(self):
        b = _board_rows(_bp("Nobody Knows Him", overall_rank=200, pos_rank=60))
        adp = pd.DataFrame([{"_key": "someone-else", "adp": 6.5}])

        out = board.attach_adp(b, adp)
        assert out["adp_source"].iloc[0] == "modelled"
        assert out["adp"].notna().all()

    def test_no_adp_source_at_all_models_every_player(self):
        b = _board_rows(_bp("A Back"), _bp("A Receiver", position="WR"))
        out = board.attach_adp(b, None)
        assert set(out["adp_source"]) == {"modelled"}
        assert out["adp"].notna().all()

    def test_an_empty_adp_frame_is_treated_as_no_adp(self):
        b = _board_rows(_bp("A Back"))
        out = board.attach_adp(b, pd.DataFrame(columns=["_key", "adp"]))
        assert out["adp_source"].iloc[0] == "modelled"

    def test_adp_delta_is_market_price_minus_our_rank(self):
        from ffdraft.names import normalize
        b = _board_rows(_bp("Value Pick", overall_rank=10))
        adp = pd.DataFrame([{"_key": normalize("Value Pick"), "adp": 25.0}])

        out = board.attach_adp(b, adp)
        assert out["adp_delta"].iloc[0] == 15.0

    def test_a_player_off_every_depth_chart_is_buried_in_the_fallback(self):
        # last_season alone reads him as fresh, so without the off_roster term a
        # fallback estimate would hand a player nobody rosters a top-of-board
        # fake market price.
        b = _board_rows(
            _bp("Rostered", pos_rank=20, overall_rank=50, last_season=2025,
                off_roster=False),
            _bp("Off Roster", pos_rank=20, overall_rank=50, last_season=2025,
                off_roster=True),
        )
        out = board.attach_adp(b, None).set_index("name")
        assert out.loc["Off Roster", "adp"] > out.loc["Rostered", "adp"]

    def test_a_stale_player_is_buried_relative_to_a_current_one(self):
        b = _board_rows(
            _bp("Current", pos_rank=20, overall_rank=50, last_season=2025),
            _bp("Stale", pos_rank=20, overall_rank=50, last_season=2022),
        )
        out = board.attach_adp(b, None).set_index("name")
        assert out.loc["Stale", "adp"] > out.loc["Current", "adp"]

    def test_duplicate_adp_rows_do_not_duplicate_board_rows(self):
        from ffdraft.names import normalize
        key = normalize("Bijan Robinson")
        b = _board_rows(_bp("Bijan Robinson"))
        adp = pd.DataFrame([{"_key": key, "adp": 6.5}, {"_key": key, "adp": 7.5}])

        assert len(board.attach_adp(b, adp)) == 1


class TestPlayerIndexCache:
    def test_the_same_board_reuses_its_index(self):
        b = _board_rows(_bp("Josh Allen", position="QB"))
        assert board.player_index(b) is board.player_index(b)

    def test_a_changed_board_gets_a_fresh_index(self):
        # Keying on id() would be wrong as well as slow: CPython recycles ids,
        # so a rebuilt board can land on a freed id and be served a stale index
        # belonging to a different set of players.
        one = board.player_index(_board_rows(_bp("Josh Allen", position="QB")))
        two = board.player_index(_board_rows(_bp("Bijan Robinson")))
        assert one is not two

    def test_an_empty_board_has_a_stable_fingerprint(self):
        empty = pd.DataFrame(columns=["name", "position"])
        assert board._board_fingerprint(empty) == board._board_fingerprint(empty)

    def test_match_player_resolves_through_the_index(self):
        b = _board_rows(_bp("Amon-Ra St. Brown", position="WR"))
        assert board.match_player("Amon Ra St Brown", b)["name"] == "Amon-Ra St. Brown"

    def test_match_player_verbose_reports_how_it_matched(self):
        b = _board_rows(_bp("Amon-Ra St. Brown", position="WR"))
        row, how = board.match_player_verbose("Amon-Ra St. Brown", b)
        assert row is not None
        assert isinstance(how, str) and how


class TestEspnErrors:
    """An ESPN read that fails has to say what failed (issue #46).

    Every call ended in a bare `raise_for_status()`, and ESPN sends no reason
    phrase -- so the message was `401 Client Error:  for url: ...`, empty
    exactly where the reason belongs. Through the MCP layer even that was lost
    and the client saw only "Error executing tool sync_draft", which cannot
    tell expired cookies from a wrong league id from an ESPN outage.
    """

    def _patch(self, monkeypatch, payload, status=200, text=None, boom=None):
        def fake_get(url, **kw):
            if boom is not None:
                raise boom
            return _FakeResponse(payload, status, text)
        monkeypatch.setattr(board.requests, "get", fake_get)

    @pytest.mark.parametrize("status,expected", [
        (401, "expired"),
        (403, "member of it"),
        (404, "season"),
        (429, "rate-limiting"),
        (503, "trouble at their end"),
    ])
    def test_each_status_says_what_to_do_about_it(self, monkeypatch, status, expected):
        self._patch(monkeypatch, {}, status=status)
        with pytest.raises(board.EspnError) as exc:
            board.sync_espn("12345", 2025)
        message = str(exc.value)
        assert str(status) in message
        assert expected in message

    def test_the_status_code_is_available_on_the_error(self, monkeypatch):
        # So a caller can branch on it without parsing prose.
        self._patch(monkeypatch, {}, status=404)
        with pytest.raises(board.EspnError) as exc:
            board.espn_scoring_items("12345")
        assert exc.value.status == 404

    def test_espns_own_words_are_included_but_truncated(self, monkeypatch):
        self._patch(monkeypatch, {}, status=401, text="x" * 500)
        with pytest.raises(board.EspnError) as exc:
            board.sync_espn("12345", 2025)
        assert "xxx" in str(exc.value)
        assert len(str(exc.value)) < 700          # not the whole 500-char body

    def test_the_message_never_carries_the_cookies(self, monkeypatch):
        # The whole point of the error is that it gets pasted into a bug report.
        monkeypatch.setenv("ESPN_SWID", "{SECRET-SWID}")
        monkeypatch.setenv("ESPN_S2", "SECRET-S2-VALUE")
        self._patch(monkeypatch, {}, status=401, text="denied")
        with pytest.raises(board.EspnError) as exc:
            board.sync_espn("12345", 2025)
        assert "SECRET-SWID" not in str(exc.value)
        assert "SECRET-S2-VALUE" not in str(exc.value)

    def test_an_unreachable_espn_is_named_as_such(self, monkeypatch):
        self._patch(monkeypatch, {}, boom=requests.ConnectionError("no route"))
        with pytest.raises(board.EspnError) as exc:
            board.sync_espn("12345", 2025)
        assert "could not reach ESPN" in str(exc.value)

    def test_a_timeout_says_it_timed_out(self, monkeypatch):
        self._patch(monkeypatch, {}, boom=requests.Timeout("slow"))
        with pytest.raises(board.EspnError) as exc:
            board.sync_espn("12345", 2025)
        assert "did not respond" in str(exc.value)

    def test_a_login_page_instead_of_json_is_explained(self, monkeypatch):
        # ESPN answers 200 with HTML when a session is not what it expects.
        self._patch(monkeypatch, _NOT_JSON, status=200, text="<html>sign in</html>")
        with pytest.raises(board.EspnError) as exc:
            board.sync_espn("12345", 2025)
        assert "not JSON" in str(exc.value)

    def test_a_good_response_still_comes_back_normally(self, monkeypatch):
        self._patch(monkeypatch, {"draftDetail": {"picks": []}})
        assert board.sync_espn("12345", 2025) == []
