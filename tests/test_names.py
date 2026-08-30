"""Name resolution tests.

These matter more than they look. An unresolved name doesn't raise — it silently
becomes a player who scored zero, which manufactures fake busts in any analysis and
leaves drafted players sitting on the board as available.
"""
import pandas as pd
import pytest

from ffdraft.names import PlayerIndex, alias_keys, normalize


@pytest.fixture
def index():
    board = pd.DataFrame([
        {"name": "Josh Palmer", "position": "WR"},
        {"name": "Marquise Brown", "position": "WR"},
        {"name": "Kenneth Walker III", "position": "RB"},
        {"name": "DJ Moore", "position": "WR"},
        {"name": "Amon-Ra St. Brown", "position": "WR"},
        {"name": "Michael Pittman", "position": "WR"},
        {"name": "Jaxon Smith-Njigba", "position": "WR"},
        {"name": "Patrick Mahomes", "position": "QB"},
        {"name": "Bijan Robinson", "position": "RB"},
        {"name": "Puka Nacua", "position": "WR"},
        {"name": "CeeDee Lamb", "position": "WR"},
        {"name": "Christian McCaffrey", "position": "RB"},
        {"name": "Justin Jefferson", "position": "WR"},
        {"name": "Van Jefferson", "position": "WR"},
        {"name": "Josh Allen", "position": "QB"},
    ])
    return PlayerIndex(board)


class TestNormalize:
    def test_strips_punctuation_and_suffix(self):
        assert normalize("Michael Pittman Jr.") == "michael pittman"
        assert normalize("Kenneth Walker III") == "kenneth walker"
        assert normalize("Ja'Marr Chase") == "jamarr chase"

    def test_hyphens_become_spaces(self):
        assert normalize("Jaxon Smith-Njigba") == "jaxon smith njigba"
        assert normalize("Amon-Ra St. Brown") == "amon ra st brown"

    def test_known_alternate_names(self):
        assert normalize("Hollywood Brown") == "marquise brown"

    def test_is_idempotent(self):
        for raw in ["Josh Palmer", "D.J. Moore", "Kenneth Walker III"]:
            assert normalize(normalize(raw)) == normalize(raw)

    def test_accents_fold_to_the_plain_letter(self):
        """Sources disagree on diacritics, and the punctuation strip used to turn
        an accented letter into a space -- "Estimé" became "estim" and never
        joined the unaccented "Estime" every ranking site publishes."""
        assert normalize("Audric Estimé") == "audric estime"
        assert normalize("Audric Estimé") == normalize("Audric Estime")

    @pytest.mark.parametrize("accented,plain", [
        ("Aymeric Boissé", "Aymeric Boisse"),
        ("José Álvarez", "Jose Alvarez"),
        ("Björn Nuñez", "Bjorn Nunez"),
    ])
    def test_accented_and_plain_spellings_share_a_key(self, accented, plain):
        assert normalize(accented) == normalize(plain)

    @pytest.mark.parametrize("raw,expected", [
        ("Bijan Robinson", "bijan robinson"),
        ("CeeDee Lamb", "ceedee lamb"),
        ("Ja'Marr Chase", "jamarr chase"),
        ("Amon-Ra St. Brown", "amon ra st brown"),
    ])
    def test_plain_ascii_names_are_untouched_by_folding(self, raw, expected):
        assert normalize(raw) == expected


class TestAliasKeys:
    def test_first_name_swaps_both_directions(self):
        assert "joshua palmer" in alias_keys("Josh Palmer")
        assert "josh palmer" in alias_keys("Joshua Palmer")

    def test_always_includes_own_normalized_form(self):
        for raw in ["Bijan Robinson", "CeeDee Lamb", "Puka Nacua"]:
            assert normalize(raw) in alias_keys(raw)


class TestResolution:
    @pytest.mark.parametrize("query,expected", [
        ("Josh Palmer", "Josh Palmer"),
        ("Joshua Palmer", "Josh Palmer"),          # formal <-> short
        ("Hollywood Brown", "Marquise Brown"),     # alternate name
        ("Ken Walker III", "Kenneth Walker III"),  # nickname + suffix
        ("Kenneth Walker", "Kenneth Walker III"),  # missing suffix
        ("D.J. Moore", "DJ Moore"),                # punctuation
        ("Amon Ra St Brown", "Amon-Ra St. Brown"), # hyphens and periods
        ("Michael Pittman Jr", "Michael Pittman"), # suffix the source lacks
        ("Jaxon Smith Njigba", "Jaxon Smith-Njigba"),
    ])
    def test_variants_resolve(self, index, query, expected):
        row, how = index.resolve(query)
        assert row is not None, f"{query!r} did not resolve ({how})"
        assert row["name"] == expected

    @pytest.mark.parametrize("query,expected", [
        ("Bijan", "Bijan Robinson"),
        ("Puka", "Puka Nacua"),
        ("Mahomes", "Patrick Mahomes"),
        ("CeeDee", "CeeDee Lamb"),
    ])
    def test_single_token_that_people_actually_type(self, index, query, expected):
        row, _ = index.resolve(query)
        assert row is not None and row["name"] == expected

    @pytest.mark.parametrize("query,expected", [
        ("JSN", "Jaxon Smith-Njigba"),
        ("CMC", "Christian McCaffrey"),
        ("ARSB", "Amon-Ra St. Brown"),
    ])
    def test_initialisms(self, index, query, expected):
        row, _ = index.resolve(query)
        assert row is not None and row["name"] == expected

    def test_accented_board_entry_resolves_from_a_plain_query(self):
        """The board comes from nflverse (accented); the query comes from a
        ranking site or a person typing (usually not)."""
        idx = PlayerIndex(pd.DataFrame([
            {"name": "Audric Estimé", "position": "RB"},
            {"name": "Bijan Robinson", "position": "RB"},
        ]))
        row, how = idx.resolve("Audric Estime")
        assert row is not None, f"unresolved ({how})"
        assert row["name"] == "Audric Estimé"
        assert how == "exact"

    def test_typos_resolve_via_fuzzy(self, index):
        for typo in ["Bijon Robinsen", "Puca Nacua", "CeDee Lamb"]:
            row, how = index.resolve(typo)
            assert row is not None, f"{typo!r} unresolved"

    def test_ambiguous_refuses_rather_than_guessing(self, index):
        """Two Jeffersons on the board. Guessing one is worse than saying so."""
        row, how = index.resolve("Jefferson")
        assert row is None
        assert "ambiguous" in how
        assert "Justin Jefferson" in how and "Van Jefferson" in how

    def test_position_disambiguates(self, index):
        row, _ = index.resolve("Jefferson", position="WR")
        # Still ambiguous within WR, but must never return a non-WR.
        if row is not None:
            assert row["position"] == "WR"

    def test_nonexistent_player_is_rejected(self, index):
        row, how = index.resolve("Zzzz Fakename")
        assert row is None
        assert how == "unmatched"

    def test_empty_query_is_safe(self, index):
        row, how = index.resolve("")
        assert row is None
