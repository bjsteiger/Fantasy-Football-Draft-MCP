"""Individual defensive players: scoring, projection and board.

Deliberately separate from the offence model rather than folded into it. The
projection pipeline in model.py is built around concepts that have no meaning
for a defender -- target share, air yards, O-line pass protection, route
separation -- and two of its multipliers would be actively wrong rather than
merely irrelevant: m_oline would grade a linebacker on his own team's pass
blocking, and m_volume on his own team's pass rate, when defensive production
is driven by the opponent. Widening FANTASY_POSITIONS would also fabricate
fpa_LB / sos_LB_z ("points a defence allows to opposing linebackers", a
category error) and multiply real QB/RB/WR/TE projections by it.

So IDP gets its own module, the same way defense_report is its own tool. See
docs/idp-research.md for the full argument and docs/idp-scoring-derivation.md
for how the ESPN scoring below was derived.

Accuracy, measured rather than assumed: reproducing ESPN's own IDP totals from
nflverse gives 3.5% mean absolute error and 0.97 Spearman rank correlation over
38 linebackers. That ranks a board reliably. It does NOT support quoting an
exact projected total, or separating two players within about 12 points -- the
residual is almost entirely the tackle split described below.
"""
from __future__ import annotations

import pandas as pd

# ESPN statId -> the stat it actually is. Every entry here was matched against
# real player-seasons; see docs/idp-scoring-derivation.md. IDs that could not be
# identified are deliberately absent rather than guessed, because a wrong
# mapping is silent -- it yields a plausible total that is simply wrong.
ESPN_STAT_IDS = {
    "95": "interceptions",       # verified, 21 players
    "99": "sacks",               # verified, 32 players
    "106": "forced_fumbles",     # verified, 23 players
    "109": "tackles_total",      # verified, all 38 players
    "113": "passes_defended",    # verified, 37 players
    # 107/108 are ESPN's solo/assisted split. Their SUM always equals 109, but
    # neither side matches nflverse's own split: the two providers agree on how
    # many tackles a player made and disagree on how many were solo, by ~10.45
    # per season. Tackle attribution is an unofficial, human-scored stat, so
    # this is a provider disagreement rather than a bug in either source. Kept
    # because the split carries real scoring weight (assists are often worth
    # double), and approximating it beats dropping it -- but it is the reason
    # for the 3.5% error quoted above, not an exact figure.
    "107": "tackles_solo",       # approximate
    "108": "tackles_assisted",   # approximate
}

# Which nflverse columns make up each stat. Summed when more than one: ESPN's
# assisted-tackle count corresponds to nflverse's assists plus its separate
# "with assist" column.
NFLVERSE_COLUMNS = {
    "interceptions": ("def_interceptions",),
    "sacks": ("def_sacks",),
    "forced_fumbles": ("def_fumbles_forced",),
    "passes_defended": ("def_pass_defended",),
    "tackles_solo": ("def_tackles_solo",),
    "tackles_assisted": ("def_tackle_assists", "def_tackles_with_assist"),
    "tackles_total": ("def_tackles_solo", "def_tackle_assists",
                      "def_tackles_with_assist"),
}

# ESPN's lineup slot for a team defence. A scoring item that pays out only via
# an override for this slot is scoring the D/ST unit, not an individual player.
_DST_SLOT = "16"

# Defensive position groups in nflverse. `position` fragments linebackers four
# ways (LB/OLB/MLB/ILB) and filtering on it drops about a fifth of the pool, so
# every filter here uses `position_group`.
DEFENSIVE_GROUPS = ("LB", "DL", "DB")


def scoring_from_espn(scoring_items: list[dict]) -> dict[str, float]:
    """League scoring, keyed by stat name, for the defensive stats we can model.

    Unknown statIds are skipped rather than guessed at. An item that scores only
    through a slot-16 override is the team defence's scoring, not a player's.
    """
    out: dict[str, float] = {}
    for item in scoring_items or []:
        sid = str(item.get("statId"))
        name = ESPN_STAT_IDS.get(sid)
        if name is None:
            continue
        pts = float(item.get("points", 0.0) or 0.0)
        if pts:
            out[name] = pts
    return out


def league_scores_idp(scoring_items: list[dict]) -> bool:
    """Whether this league awards points to individual defensive players."""
    return bool(scoring_from_espn(scoring_items))


def fantasy_points(weekly: pd.DataFrame, scoring: dict[str, float]) -> pd.Series:
    """Defensive fantasy points per row, under the given scoring.

    A stat the frame does not carry contributes zero rather than raising, so a
    league scoring something nflverse does not publish degrades quietly instead
    of failing the whole board.
    """
    if weekly is None or weekly.empty:
        return pd.Series(dtype="float64")
    total = pd.Series(0.0, index=weekly.index)
    for stat, pts in (scoring or {}).items():
        cols = NFLVERSE_COLUMNS.get(stat)
        if not cols or not pts:
            continue
        for col in cols:
            if col in weekly.columns:
                total = total + pd.to_numeric(
                    weekly[col], errors="coerce").fillna(0.0) * pts
    return total


def _empty_board() -> pd.DataFrame:
    return pd.DataFrame(columns=["name", "player_id", "position", "games",
                                 "proj_points", "ppg", "vor", "pos_rank"])


# Games a defender must have played before a per-game rate is trusted. Without
# this the board is nonsense: a player with one big game projects a rate no
# starter sustains and lands at number one. In a real 2025 build, a defensive
# back with a single 38-point game projected 646 points and outranked both of
# the genuinely best linebackers. Half a season is the floor at which the rate
# reflects a role rather than one afternoon.
MIN_GAMES = 8


def build_board(weekly: pd.DataFrame, scoring: dict[str, float],
                seasons: list[int] | None = None, teams: int = 10,
                idp_slots: int = 1, min_games: int = MIN_GAMES) -> pd.DataFrame:
    """Rank defenders by projected season points, with value over replacement.

    Projection is a per-game rate carried forward over a 17-game season, which
    is intentionally simpler than the offence model: none of the environment
    multipliers there transfer to a defender, and inventing defensive ones
    without a backtest is exactly the mistake matchup_backtest exists to catch.

    Replacement level is the last defender who would actually start in this
    league (teams x idp_slots), which is what makes the ranking mean anything:
    raw defensive totals dwarf offensive ones, so only value over replacement is
    comparable across positions.
    """
    if weekly is None or weekly.empty:
        return _empty_board()

    df = weekly
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    if seasons and "season" in df.columns:
        df = df[df["season"].isin(seasons)]
    group_col = "position_group" if "position_group" in df.columns else "position"
    df = df[df[group_col].isin(DEFENSIVE_GROUPS)]
    if df.empty:
        return _empty_board()

    df = df.copy()
    df["_pts"] = fantasy_points(df, scoring)

    keys = ["player_display_name"]
    if "player_id" in df.columns:
        keys.append("player_id")
    if "position" in df.columns:
        keys.append("position")

    agg = df.groupby(keys, dropna=False).agg(
        games=("week", "nunique"), points=("_pts", "sum"),
    ).reset_index().rename(columns={"player_display_name": "name"})

    # Rank only players with enough of a sample to have a real rate. Anyone
    # below the threshold is dropped rather than shrunk toward a mean: the
    # cohort mean here spans every rotational defender in the league, and
    # regressing toward it would drag genuine starters down (the same trap the
    # offence model hit when it regressed small samples toward an all-player
    # mean that included third-stringers).
    agg = agg[agg["games"] >= max(1, int(min_games))]
    if agg.empty:
        return _empty_board()

    agg["ppg"] = agg["points"] / agg["games"]
    # A 17-game season. Games played is not projected forward -- a player who
    # missed time is not assumed to miss it again, and one who played every week
    # is not rewarded twice for it.
    agg["proj_points"] = agg["ppg"] * 17.0

    agg = agg.sort_values("proj_points", ascending=False).reset_index(drop=True)
    agg["pos_rank"] = agg.index + 1

    starters = max(1, int(teams) * max(0, int(idp_slots)))
    idx = min(starters, len(agg)) - 1
    replacement = float(agg.loc[idx, "proj_points"])
    agg["vor"] = agg["proj_points"] - replacement

    for c in ("proj_points", "ppg", "vor"):
        agg[c] = agg[c].round(1)
    cols = [c for c in ("name", "player_id", "position", "games",
                        "proj_points", "ppg", "vor", "pos_rank") if c in agg.columns]
    return agg[cols]
