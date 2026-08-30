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
                idp_slots: int = 1, min_games: int = MIN_GAMES,
                require_recent_season: bool = True) -> pd.DataFrame:
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

    # Per season first, so multiple seasons can be recency-weighted rather than
    # pooled. Pooling would treat a player's 2021 form as equal evidence to his
    # 2025 form, which is how a declining veteran keeps a rating he no longer
    # earns -- and the whole reason the offence side weights by recency too.
    per_season = df.groupby(keys + ["season"] if "season" in df.columns else keys,
                            dropna=False).agg(
        games=("week", "nunique"), points=("_pts", "sum"),
    ).reset_index()
    per_season = per_season[per_season["games"] >= max(1, int(min_games))]
    if per_season.empty:
        return _empty_board()
    per_season["ppg"] = per_season["points"] / per_season["games"]

    # Drop anyone who did not play in the most recent season on the board. A
    # multi-season average happily keeps ranking a player who has stopped
    # playing: C.J. Mosley last appeared in 2024, for four games, and came out
    # first on a 2026 board built from 2021-25 -- his three strong seasons
    # outweighing his absence. You cannot draft someone who is not playing, and
    # nothing else here would have caught it, since every test still passed.
    if require_recent_season and "season" in per_season.columns:
        latest = int(per_season["season"].max())
        active = set(per_season.loc[per_season["season"] == latest,
                                    "player_display_name"])
        per_season = per_season[per_season["player_display_name"].isin(active)]
        if per_season.empty:
            return _empty_board()

    if "season" in per_season.columns and per_season["season"].nunique() > 1:
        agg = _recency_weighted(per_season, keys)
        agg = _shrink_thin_evidence(agg)
    else:
        agg = (per_season.groupby(keys, dropna=False)
               .agg(games=("games", "sum"), ppg=("ppg", "mean")).reset_index())
    agg = agg.rename(columns={"player_display_name": "name"})

    # Rank only players with enough of a sample to have a real rate. Anyone
    # below the threshold is dropped rather than shrunk toward a mean: the
    # cohort mean here spans every rotational defender in the league, and
    # regressing toward it would drag genuine starters down (the same trap the
    # offence model hit when it regressed small samples toward an all-player
    # mean that included third-stringers).
    if agg.empty:
        return _empty_board()
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
    cols = [c for c in ("name", "player_id", "position", "games", "seasons_used",
                        "proj_points", "ppg", "vor", "pos_rank") if c in agg.columns]
    return agg[cols]


def _recency_weighted(per_season: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Blend a player's seasons, weighting recent ones far more heavily.

    Uses the same RECENCY_WEIGHTS the offence model does, so a defender and a
    receiver are being projected on comparable terms rather than one of them
    reading last season alone.

    Chosen on evidence, not preference: predicting 2025 for 536 defenders from
    2021-24, recency weighting beat both using only the latest season (MAE 2.54
    vs 2.67) and a flat mean (2.60), and ranked them better too (0.712 vs 0.702
    and 0.695). No age curve is applied on top -- linebacker production is close
    to flat from 27 through 32 in this data, and what decline is visible is
    confounded by survivorship, since only defenders still playing well stay in
    the league. Recency weighting already captures a real decline, because a
    declining player's recent seasons are simply worse.
    """
    from .config import RECENCY_WEIGHTS

    out = []
    for vals, grp in per_season.groupby(keys, dropna=False):
        grp = grp.sort_values("season")
        n = len(grp)
        w = RECENCY_WEIGHTS[-n:] if n <= len(RECENCY_WEIGHTS) else (
            [RECENCY_WEIGHTS[0]] * (n - len(RECENCY_WEIGHTS)) + list(RECENCY_WEIGHTS))
        total = float(sum(w)) or 1.0
        ppg = float(sum(p * q for p, q in zip(grp["ppg"], w)) / total)
        row = dict(zip(keys, vals if isinstance(vals, tuple) else (vals,)))
        row["ppg"] = ppg
        row["games"] = int(grp["games"].sum())
        row["seasons_used"] = n
        out.append(row)
    return pd.DataFrame(out)


# How far a thin-evidence projection is pulled toward the qualified-starter
# mean, by how many seasons it rests on. Fitted, not chosen: this pair beat
# doing nothing on both mean absolute error and rank correlation across all
# three folds (2023 from 2021-22, 2024 from 2021-23, 2025 from 2021-24), had the
# best average error rank of every candidate tried, and each value was at or
# within 0.001 of its fold-by-fold optimum. Stronger pulls (0.35, 0.40) stopped
# beating doing nothing, and a third tier for three-season players was tested
# and left out -- it moved error by 0.0005 and split the folds on ranking, which
# is not evidence for a constant. See _shrink_thin_evidence.
EVIDENCE_SHRINK = {1: 0.20, 2: 0.10}


def _shrink_thin_evidence(agg: pd.DataFrame) -> pd.DataFrame:
    """Discount a projection built on one or two seasons toward the starter mean.

    A player with one season and a player with five are not equally knowable,
    and the data says so: predicting a held-out season, one-season defenders
    landed at 2.80 mean absolute error and 0.607 rank correlation against 2.30
    and 0.854 for four-season players.

    The pull is toward the mean of the *upper half* of the board, not the mean
    of every qualified defender. Regressing toward an all-player mean is the
    trap the offence model already fell into -- that mean is dragged down by
    rotational players, and shrinking real starters toward it cut genuine
    starters by a third. Retested here against the tiers below: the all-player
    mean does slightly better on error and worse on ranking in all three folds,
    and ranking is what a draft board is for. A top-quartile anchor was worse on
    both.

    Where the error actually lives is the top of the board, not the pool. Across
    the pool a one-season defender is *under*-projected on average (bias -0.33
    and -0.57 ppg on the two live-scoring folds). Among the twenty players the
    board ranks highest -- the only ones anyone drafts -- it inverts: one-season
    players came in +9.07 ppg over their actual 2025 rate against +1.42 for
    multi-season players, and finished 172nd on average against 27th. That is
    the failure issue #33 reported, measured.

    Deliberately gentle: nothing here reorders players who have the evidence.
    Over the three folds this moved mean absolute error 2.581 -> 2.535,
    0.546 -> 0.530 and 2.444 -> 2.387, and rank correlation 0.7112 -> 0.7188,
    0.6868 -> 0.6967 and 0.7393 -> 0.7466. Roughly a 2.3% error improvement --
    real, and small, which is the honest summary.
    """
    if "seasons_used" not in agg.columns or agg.empty:
        return agg
    thin = agg["seasons_used"].isin(EVIDENCE_SHRINK)
    if not thin.any():
        return agg
    anchor = float(agg[agg["ppg"] >= agg["ppg"].median()]["ppg"].mean())
    agg = agg.copy()
    for seasons, k in EVIDENCE_SHRINK.items():
        rows = agg["seasons_used"] == seasons
        if rows.any():
            agg.loc[rows, "ppg"] = agg.loc[rows, "ppg"] * (1 - k) + anchor * k
    return agg


def draft_timing(defender_picks_by_season: dict[int, list[int]], teams: int,
                 my_picks: list[int] | None = None) -> dict:
    """When defenders actually leave the board, from a league's own draft history.

    This exists instead of a per-player IDP average draft position, and the
    reason is a measured one rather than a shortcut. Published IDP consensus
    barely predicts when defenders go: across two real seasons of one league,
    consensus rank against actual pick correlated 0.30 (0.48 on ranks), versus
    the near-lockstep relationship the offence side relies on. In one season the
    16th-ranked linebacker went first and the 2nd-ranked fell thirteen picks
    later; in the next, the consensus IDP1 went fifth among defenders.

    Attaching a per-player ADP to that would dress up noise as a market. What
    *is* stable is the envelope -- defenders start going in a predictable window
    and are gone by another -- so that is what this reports: how many are off the
    board by a given pick, which is what "can I wait?" actually depends on.

    Needs the league's own history. A different league drafts defenders on a
    completely different schedule, and there is no general answer to substitute.
    """
    seasons = {int(k): sorted(v or []) for k, v in (defender_picks_by_season or {}).items()}
    if not seasons or not any(seasons.values()):
        return {"seasons": [], "note": "no defender picks found in this league's history"}

    rounds = {}
    max_pick = max((max(v) for v in seasons.values() if v), default=0)
    for cut in range(teams, max_pick + teams, teams):
        gone = {s: sum(1 for p in v if p <= cut) for s, v in seasons.items()}
        vals = list(gone.values())
        rounds[cut] = {"round": (cut - 1) // teams + 1, "by_season": gone,
                       "mean_gone": round(sum(vals) / len(vals), 1)}

    firsts = {s: min(v) for s, v in seasons.items() if v}
    lasts = {s: max(v) for s, v in seasons.items() if v}
    at_my_picks = None
    if my_picks:
        at_my_picks = [
            {"pick": p, "round": (p - 1) // teams + 1,
             "mean_gone_before": round(
                 sum(sum(1 for x in v if x < p) for v in seasons.values()) / len(seasons), 1)}
            for p in my_picks
        ]
    return {
        "seasons": sorted(seasons),
        "first_defender_pick": firsts,
        "last_defender_pick": lasts,
        "gone_by_pick": rounds,
        "at_my_picks": at_my_picks,
        "caveat": "From this league's own drafts only, and a small sample -- two "
                  "seasons is roughly twenty picks. Treat it as a window, not a "
                  "schedule. Published IDP consensus is deliberately not used to "
                  "predict individual timing: it correlated 0.30 with actual pick "
                  "here, so which specific defender goes when is close to noise. "
                  "The useful signal is how many are gone, not which.",
    }


def defender_names(weekly: pd.DataFrame) -> set[str]:
    """Every player in the data whose position group is defensive.

    Used to tell an IDP pick apart from a kicker or a defence unit. All three
    are missing from the offence board, so without this a drafted linebacker is
    indistinguishable from a drafted kicker -- and DraftState.my_roster drops
    both silently, which is why an IDP slot could look filled when it was not.
    """
    if weekly is None or weekly.empty:
        return set()
    col = "position_group" if "position_group" in weekly.columns else "position"
    if col not in weekly.columns or "player_display_name" not in weekly.columns:
        return set()
    d = weekly[weekly[col].isin(DEFENSIVE_GROUPS)]
    return set(d["player_display_name"].dropna().unique())


def roster_needs(starters: dict, filled: dict,
                 flex_eligible: tuple = ("RB", "WR", "TE")) -> dict:
    """Required versus filled per starting slot, including the IDP slot.

    FLEX is filled by spare eligible players rather than by anyone whose position
    is literally "FLEX", so counting it the way every other slot is counted meant
    it could never fill: no player ever carries that position, so `filled` was
    always zero. A roster with six running backs and two required still reported
    an empty flex and would have nagged for a slot filled in the third round.

    Surplus is counted across the eligible positions and applied to flex, which
    is what a lineup actually does with it.
    """
    counts = dict(filled or {})
    out = {}
    surplus = sum(max(0, int(counts.get(p, 0) or 0) - int((starters or {}).get(p, 0) or 0))
                  for p in flex_eligible)
    for slot, need in (starters or {}).items():
        need = int(need or 0)
        if need <= 0:
            continue
        have = surplus if slot == "FLEX" else int(counts.get(slot, 0) or 0)
        out[slot] = {"required": need, "filled": min(have, need) if slot == "FLEX" else have,
                     "still_needed": max(0, need - have)}
    return out


def season_finish(weekly: pd.DataFrame, scoring: dict[str, float],
                  season: int, min_games: int = 1) -> pd.DataFrame:
    """What each defender actually scored in one season, with positional rank.

    The defensive counterpart to adp.season_finish, which filters to
    FANTASY_POSITIONS and therefore reports a linebacker as having no season at
    all. Used to give a real verdict to a draft pick the offence board cannot
    see -- not to feed a projection, so no minimum-games gate is applied by
    default: a pick that busted through injury is exactly the outcome worth
    reporting, not one to filter away.
    """
    if weekly is None or weekly.empty:
        return pd.DataFrame(columns=["name", "position", "games", "points", "pos_rank"])
    df = weekly
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    if "season" in df.columns:
        df = df[df["season"] == season]
    col = "position_group" if "position_group" in df.columns else "position"
    df = df[df[col].isin(DEFENSIVE_GROUPS)]
    if df.empty:
        return pd.DataFrame(columns=["name", "position", "games", "points", "pos_rank"])
    df = df.copy()
    df["_pts"] = fantasy_points(df, scoring)
    keys = ["player_display_name"] + (["position"] if "position" in df.columns else [])
    out = (df.groupby(keys, dropna=False)
             .agg(games=("week", "nunique"), points=("_pts", "sum"))
             .reset_index().rename(columns={"player_display_name": "name"}))
    out = out[out["games"] >= max(0, int(min_games))]
    out = out.sort_values("points", ascending=False).reset_index(drop=True)
    out["pos_rank"] = out.index + 1
    return out
