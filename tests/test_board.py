"""ESPN id crosswalk: tested offline with synthetic weekly_rosters data."""
import pandas as pd

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
