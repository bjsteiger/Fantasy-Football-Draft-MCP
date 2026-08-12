"""The draft model.

Pipeline: baseline projection -> environment adjustments -> consistency blend ->
value over replacement -> pick-aware urgency.

The last step is the one that actually wins drafts. Knowing a player is good is easy;
knowing whether he'll still be there when you pick again is what decides who to take now.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import features, sources
from .config import CURRENT_SEASON, FANTASY_POSITIONS, LeagueSettings, ModelWeights, Scoring


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _season_weighted(profiles: pd.DataFrame, col: str) -> pd.Series:
    wmap = features._season_weights(sorted(profiles["season"].unique()))
    p = profiles.copy()
    p["w"] = p["season"].map(wmap).fillna(0)
    p["wv"] = p[col].fillna(0) * p["w"]
    agg = p.groupby("player_id").agg(num=("wv", "sum"), den=("w", "sum"))
    return (agg["num"] / agg["den"].replace(0, np.nan)).rename(col)


def build_player_table(league: LeagueSettings, weights: ModelWeights,
                       season: int = CURRENT_SEASON) -> pd.DataFrame:
    """Assemble every modelled feature into one row per player."""
    sc = league.scoring
    profiles = features.player_season_profiles(sc, league.te_premium_bonus)

    # --- recency-weighted production and reliability
    agg = pd.concat([
        _season_weighted(profiles, "fp_mean"),
        _season_weighted(profiles, "startable_rate"),
        _season_weighted(profiles, "spike_rate"),
        _season_weighted(profiles, "floor"),
        _season_weighted(profiles, "ceiling"),
        _season_weighted(profiles, "fp_cv"),
        _season_weighted(profiles, "snap_share"),
        _season_weighted(profiles, "touches"),
        _season_weighted(profiles, "target_share"),
        _season_weighted(profiles, "rec_per_game"),
    ], axis=1).reset_index()

    latest = profiles.sort_values("season").groupby("player_id").agg(
        name=("player_display_name", "last"),
        position=("position", "last"),
        team=("team", "last"),
        last_season=("season", "max"),
        seasons_played=("season", "nunique"),
        games_last=("games", "last"),
    ).reset_index()
    tbl = latest.merge(agg, on="player_id", how="left")

    # Keep players who were active recently AND held a real offensive role. Without
    # the role filter the board fills with special-teamers and emergency starters
    # whose tiny samples produce noisy, flattering rates.
    tbl = tbl[tbl["last_season"] >= profiles["season"].max() - 1]
    role = (
        (tbl["games_last"].fillna(0) >= 6)
        | (tbl["snap_share"].fillna(0) >= 0.40)
        | (tbl["touches"].fillna(0) >= 45)
    )
    tbl = tbl[role].reset_index(drop=True)

    # --- injury & age
    risk = features.injury_risk(profiles)
    tbl = tbl.merge(risk.drop(columns=["position"]), on="player_id", how="left")
    tbl["injury_risk"] = tbl["injury_risk"].fillna(0.25)
    tbl["age"] = tbl["age"].fillna(26.0) + (season - profiles["season"].max())

    # --- team environment
    pbp = sources.play_by_play()
    ol = features.oline_ratings(pbp)
    pace = features.team_pace_and_split(pbp)
    dfn = features.defense_ratings(pbp, sc=sc)
    sos = features.strength_of_schedule(season, dfn)

    recent = int(pace["season"].max())
    ol_r = ol[ol["season"] == recent][["team", "run_block_z", "pass_block_z",
                                       "run_block_rank", "pass_block_rank"]]
    pace_r = pace[pace["season"] == recent][["team", "plays_per_game", "pass_rate",
                                             "rush_rate", "neutral_pass_rate"]]
    tbl = tbl.merge(ol_r, on="team", how="left").merge(pace_r, on="team", how="left")
    sos_cols = ["team", "divisional_games"] + [c for c in sos.columns if c.endswith("_z")]
    tbl = tbl.merge(sos[sos_cols], on="team", how="left")

    # --- separation and route efficiency (pass catchers only)
    try:
        from . import separation as sep_mod
        sep = sep_mod.separation_summary()
        if not sep.empty:
            tbl = tbl.merge(
                sep[["player_id", "sep_score", "avg_separation", "avg_cushion",
                     "yprr", "tprr", "yac_oe", "seasons_qualified"]],
                on="player_id", how="left",
            )
    except Exception as exc:
        print(f"separation data unavailable ({type(exc).__name__})")
    for c in ("sep_score", "yprr", "tprr", "avg_separation"):
        if c not in tbl.columns:
            tbl[c] = np.nan
    tbl["is_rookie"] = False

    # --- rookies, projected from draft capital rather than history
    try:
        from . import rookies as rk
        curves = rk.fit_draft_curves(sc)
        rb = rk.rookie_board(season, sc, curves)
        if not rb.empty:
            rb["rookie_consistency"] = [
                rk.rookie_consistency_prior(p, k, curves)
                for p, k in zip(rb["position"], rb["pick"])
            ]
            # Rookies get the same landing-spot context veterans do.
            rb = rb.merge(ol_r, on="team", how="left").merge(pace_r, on="team", how="left")
            rb = rb.merge(sos[sos_cols], on="team", how="left")
            # Drop anyone already modelled from real production (returning players
            # occasionally appear in a later draft class through supplemental entry).
            rb = rb[~rb["player_id"].isin(tbl["player_id"])]
            tbl = pd.concat([tbl, rb], ignore_index=True)
            tbl["is_rookie"] = tbl["is_rookie"].fillna(True)
    except Exception as exc:
        print(f"rookie projections unavailable ({type(exc).__name__}: {exc})")

    # Rookies join after the veteran fills, so backfill anything the scorer needs.
    # A single NaN here propagates through every multiplier and voids the whole rank.
    defaults = {
        "injury_risk": 0.22, "games_missed_rate": 0.10, "report_rate": 0.12,
        "heavy_seasons": 0.0, "recent_burden": 0.5, "seasons_played": 0,
        "games_last": 0, "age": 22.0, "startable_rate": np.nan, "fp_mean": np.nan,
        "divisional_games": 6.0,
    }
    for col, val in defaults.items():
        if col in tbl.columns:
            tbl[col] = tbl[col].fillna(val)
        else:
            tbl[col] = val
    return tbl


def project(tbl: pd.DataFrame, league: LeagueSettings, weights: ModelWeights) -> pd.DataFrame:
    """Turn features into a projection, a reliability score, and a draft value."""
    t = tbl.copy()
    w = weights

    # ---- baseline: recency-weighted PPG, regressed toward the mean for small
    # samples so a two-game hot streak doesn't outrank a proven starter.
    #
    # The regression target has to be the mean of *starter-caliber* players at the
    # position, not the mean of everyone who logged a snap. Including third-string
    # backs drags the target down so far that regressing toward it would cut a
    # genuine RB1 by a third. We take the top N by weighted PPG as the comparison set.
    starter_n = {"QB": 24, "RB": 36, "WR": 48, "TE": 20}
    pos_target = {}
    for pos, chunk in t.groupby("position"):
        ranked = chunk.sort_values("fp_mean", ascending=False)
        n = min(starter_n.get(pos, 30), max(3, len(ranked)))
        pos_target[pos] = float(ranked["fp_mean"].head(n).mean())
    t["pos_target"] = t["position"].map(pos_target)

    # Shrink on games played, not seasons: 17 healthy games is a real sample,
    # three cameo appearances across two years is not.
    # Rookies arrive with a baseline already fitted from draft capital, so stash it
    # before the veteran regression runs. Letting that regression touch them would
    # replace the fitted value with a positional average and erase the whole
    # distinction between a top-five pick and a sixth-rounder.
    is_rook = t.get("is_rookie", pd.Series(False, index=t.index)).fillna(False).astype(bool)
    rookie_baseline = t["baseline_ppg"].copy() if "baseline_ppg" in t.columns else None

    games = t["games_last"].fillna(0) + 8 * (t["seasons_played"].fillna(1) - 1).clip(lower=0)
    shrink = (games / (games + 10)).clip(0.15, 0.93)
    t["baseline_ppg"] = t["fp_mean"].fillna(t["pos_target"]) * shrink + t["pos_target"] * (1 - shrink)

    if rookie_baseline is not None and is_rook.any():
        t.loc[is_rook, "baseline_ppg"] = rookie_baseline[is_rook]
        # Rookie availability is already capital-scaled; don't shrink them like vets.
        shrink = shrink.mask(is_rook, 0.60)

    # ---- environment multipliers, each bounded by its configured weight
    def bounded(series: pd.Series, weight: float) -> pd.Series:
        z = series.fillna(0).clip(-2.5, 2.5) / 2.5
        return 1 + z * weight

    is_rb = t["position"].eq("RB")
    block_z = np.where(is_rb, t["run_block_z"].fillna(0), t["pass_block_z"].fillna(0))
    t["m_oline"] = bounded(pd.Series(block_z, index=t.index), w.oline)

    # Volume: pace helps everyone; run/pass split helps the side of the ball you're on.
    pace_z = features._zscore(t["plays_per_game"].fillna(t["plays_per_game"].mean()))
    split = np.where(is_rb, t["rush_rate"].fillna(0.43), t["neutral_pass_rate"].fillna(0.57))
    split_z = features._zscore(pd.Series(split, index=t.index))
    t["m_volume"] = bounded((pace_z + split_z) / 2, w.pace_volume)

    # Schedule: positive z = opponents allow more points to this position.
    sos_z = pd.Series(0.0, index=t.index)
    for pos in FANTASY_POSITIONS:
        col = f"sos_{pos}_z"
        if col in t.columns:
            sos_z = sos_z.where(t["position"] != pos, t[col].fillna(0))
    t["m_schedule"] = bounded(sos_z, w.schedule)

    # Divisional games: six games against defenses that game-plan for you specifically.
    # Modelled as a small drag that scales with how tough those division defenses are.
    div = t["divisional_games"].fillna(6)
    t["m_divisional"] = 1 - w.divisional * ((div - 6) / 6 + 0.5) * (-sos_z.fillna(0).clip(-2, 2) / 2)

    # Injury: expected games available out of 17. The risk score is a relative
    # ranking, not a literal miss probability, so it's scaled before converting.
    t["exp_games"] = (17 * (1 - t["injury_risk"] * 0.62)).clip(7, 17)
    if is_rook.any() and "exp_games" in tbl.columns:
        # Rookie availability comes from draft capital, not injury history they
        # don't have yet.
        t.loc[is_rook, "exp_games"] = tbl["exp_games"][is_rook].values
        t.loc[is_rook, "m_injury"] = 1.0
    t["m_injury"] = 1 - (t["injury_risk"] - 0.22).clip(-0.2, 0.6) * w.injury

    t["m_age"] = [features.age_adjustment(p, a) for p, a in zip(t["position"], t["age"])]

    # Separation and route efficiency, for pass catchers only. This is the signal
    # that distinguishes a receiver whose production came from genuine route-winning
    # from one riding target volume he may not keep. It only applies to players who
    # qualified on route count — everyone else sits at neutral.
    sep_z = pd.Series(0.0, index=t.index)
    if "sep_score" in t.columns:
        catchers = t["position"].isin(["WR", "TE"]) & t["sep_score"].notna()
        if catchers.any():
            sep_z.loc[catchers] = t.loc[catchers, "sep_score"].clip(-2.5, 2.5)
    t["m_separation"] = bounded(sep_z, w.separation)

    t["adj_ppg"] = (t["baseline_ppg"] * t["m_oline"] * t["m_volume"] * t["m_schedule"]
                    * t["m_divisional"] * t["m_injury"] * t["m_age"] * t["m_separation"])
    t["proj_points"] = t["adj_ppg"] * t["exp_games"]

    # Full-PPR equivalent of the same projection. Published consensus rankings are
    # PPR, so converting the market's opinion into this league's format needs each
    # player's reception volume. The conversion itself is exact arithmetic — half
    # PPR is simply PPR minus half a point per catch — so the only estimate involved
    # is the reception count, which the projection already implies.
    rec_pg = t["rec_per_game"] if "rec_per_game" in t.columns else pd.Series(
        np.nan, index=t.index)
    # Rookies and anyone without a reception history fall back to positional norms.
    pos_rec = {"WR": 3.4, "TE": 2.8, "RB": 2.2, "QB": 0.0}
    rec_pg = rec_pg.fillna(t["position"].map(pos_rec)).fillna(0.0).clip(lower=0)
    t["proj_receptions"] = rec_pg * t["exp_games"]
    ppr_gap = 1.0 - float(league.scoring.rec)
    t["proj_points_ppr"] = t["proj_points"] + ppr_gap * t["proj_receptions"]

    # ---- consistency: the "will he give me a usable week" score.
    # Startable rate is the backbone; low variance and a high floor reinforce it;
    # injury risk cuts it, because an absent player is a zero.
    cv = t["fp_cv"].fillna(t["fp_cv"].median())
    cv_score = 1 - features._zscore(cv).clip(-2, 2) / 4
    floor_ratio = (t["floor"] / t["fp_mean"].replace(0, np.nan)).fillna(0.4).clip(0, 1)
    raw_consistency = (
        0.45 * t["startable_rate"].fillna(0.35)
        + 0.25 * floor_ratio
        + 0.15 * cv_score.clip(0, 1.5) / 1.5
        + 0.15 * (1 - t["injury_risk"])
    ).clip(0, 1)

    # Consistency needs the same small-sample regression the projection gets.
    # A backup who caught two touchdowns in his only three appearances will show a
    # perfect startable rate and near-zero variance; without this he outranks
    # established starters on "reliability" he has never actually demonstrated.
    cons_target = t["position"].map(
        t.assign(_c=raw_consistency).groupby("position").apply(
            lambda g: g.nlargest(40, "fp_mean")["_c"].mean(), include_groups=False
        ).to_dict()
    ).fillna(raw_consistency.mean())
    t["consistency"] = (raw_consistency * shrink + cons_target * (1 - shrink)).clip(0, 1)
    if is_rook.any() and "rookie_consistency" in t.columns:
        # Rookies get an explicit prior instead. They are the most volatile group in
        # fantasy: roles move mid-season and the floor is a healthy scratch.
        t.loc[is_rook, "consistency"] = t.loc[is_rook, "rookie_consistency"].fillna(0.35)
    t["consistency_sample_games"] = games

    # ---- value over replacement, then a consistency-weighted blend.
    repl = league.replacement_ranks()
    t["pos_rank"] = t.groupby("position")["proj_points"].rank(ascending=False, method="min")
    baselines = {}
    for pos in FANTASY_POSITIONS:
        chunk = t[t["position"] == pos].sort_values("proj_points", ascending=False)
        n = min(repl.get(pos, 24), max(1, len(chunk)))
        baselines[pos] = float(chunk["proj_points"].iloc[n - 1]) if len(chunk) else 0.0
    t["replacement_points"] = t["position"].map(baselines)
    t["vor"] = t["proj_points"] - t["replacement_points"]

    # Scale consistency onto the VOR distribution so the blend is apples to apples.
    vor_sd = t["vor"].std(ddof=0) or 1.0
    consistency_pts = (t["consistency"] - t["consistency"].mean()) / (
        t["consistency"].std(ddof=0) or 1.0) * vor_sd
    cw = weights.consistency_weight
    t["draft_score"] = (1 - cw) * t["vor"] + cw * consistency_pts
    # Any player whose score is undefined would silently sort to the bottom rather
    # than announcing itself, so fail loudly instead.
    if t["draft_score"].isna().any():
        bad = t.loc[t["draft_score"].isna(), ["name", "position", "is_rookie"]]
        raise ValueError(f"{len(bad)} players scored NaN, e.g. "
                         f"{bad.head(3).to_dict('records')}")
    t["overall_rank"] = t["draft_score"].rank(ascending=False, method="min").astype(int)
    return t.sort_values("draft_score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- pick-aware layer

def survival_probability(adp: float, current_pick: int, next_pick: int,
                         sd_floor: float = 5.0) -> float:
    """Chance a player is still on the board at your next pick.

    ADP is treated as the centre of a normal distribution whose spread widens later
    in the draft, which matches how real draft variance behaves: pick 3 goes where
    pick 3 goes, pick 90 is a coin flip across twenty names.
    """
    if not np.isfinite(adp):
        return 0.5
    sd = max(sd_floor, 0.22 * adp)
    # P(this player's realised draft slot lands after our next pick)
    p_survive = 1 - _norm_cdf((next_pick - adp) / sd)
    p_gone_now = _norm_cdf((current_pick - adp) / sd)
    if p_gone_now >= 0.999:
        return 0.0
    return float(np.clip(p_survive / max(1e-6, 1 - p_gone_now), 0.0, 1.0))


def survival_probability_vec(adp: np.ndarray, current_pick: int, next_pick: int,
                             sd_floor: float = 5.0) -> np.ndarray:
    """Vectorised form of survival_probability. plan_my_draft evaluates the whole
    board once per round, so the scalar version ran thousands of times per call."""
    adp = np.asarray(adp, dtype=float)
    sd = np.maximum(sd_floor, 0.22 * adp)
    # erf is available elementwise via numpy's own implementation path.
    _erf = np.vectorize(math.erf, otypes=[float])
    ncdf = lambda z: 0.5 * (1 + _erf(z / math.sqrt(2)))  # noqa: E731
    p_survive = 1 - ncdf((next_pick - adp) / sd)
    p_gone_now = ncdf((current_pick - adp) / sd)
    out = np.where(p_gone_now >= 0.999, 0.0,
                   p_survive / np.maximum(1e-6, 1 - p_gone_now))
    return np.clip(np.nan_to_num(out, nan=0.5), 0.0, 1.0)


def recommend(board: pd.DataFrame, league: LeagueSettings, current_pick: int,
              next_pick: int | None, roster: dict[str, int] | None = None,
              top_n: int = 8) -> pd.DataFrame:
    """Rank available players for the pick that's on the clock.

    Two ideas drive the ordering beyond raw value:
      * Opportunity cost — a player you're confident survives to your next pick is
        worth less right now than an equally good player who certainly won't.
      * Roster need — value is discounted once a position is full and the player
        would only be a bench body, and boosted when a starting slot is still open.
    """
    avail = board[~board["drafted"]].copy() if "drafted" in board.columns else board.copy()
    if avail.empty:
        return avail

    roster = roster or {}
    if next_pick:
        avail["p_available_next"] = survival_probability_vec(
            avail["adp"].to_numpy(), current_pick, next_pick)
    else:
        avail["p_available_next"] = 0.0

    # The heart of it: what a position is expected to still offer at your next turn.
    # Raw value says take the best player; that's wrong in a snake draft, because
    # passing on an elite QB costs you almost nothing (the QB you get two rounds
    # later is nearly as good) while passing on an elite RB costs a great deal.
    # Only the *marginal* gain over the wait matters.
    fallback = expected_best_at_next_pick(avail)
    avail["fallback_value"] = avail["position"].map(fallback).fillna(0.0)
    avail["marginal_value"] = avail["draft_score"] - avail["fallback_value"]
    avail["urgency"] = 1 - avail["p_available_next"]

    need = _positional_need(league, roster)
    avail["need_mult"] = avail["position"].map(need).fillna(0.7)

    # A small share of raw value is retained so a truly generational player still
    # rises even when his position is deep behind him.
    avail["pick_value"] = (
        0.80 * avail["marginal_value"] + 0.20 * avail["draft_score"]
    ) * avail["need_mult"]
    return avail.sort_values("pick_value", ascending=False).head(top_n)


def expected_best_at_next_pick(avail: pd.DataFrame) -> dict[str, float]:
    """Expected draft_score of the best player at each position who survives to your
    next pick.

    Walks each position from the top down, accumulating the chance every better
    player is already gone. That product is the probability this player is the best
    one left, and the sum over players is the expected value of waiting.
    """
    out: dict[str, float] = {}
    for pos, chunk in avail.groupby("position"):
        chunk = chunk.sort_values("draft_score", ascending=False)
        expected, p_all_gone = 0.0, 1.0
        for _, r in chunk.iterrows():
            p = float(r.get("p_available_next", 0.0))
            expected += float(r["draft_score"]) * p * p_all_gone
            p_all_gone *= (1 - p)
            if p_all_gone < 0.005:
                break
        # If the position empties out entirely, waiting is worth the worst on the board.
        out[pos] = expected + p_all_gone * float(chunk["draft_score"].min() if len(chunk) else 0)
    return out


# How much a bench player at each position is worth relative to the one ahead of him.
# RB and WR depth holds real value because injuries and byes force them into lineups
# constantly. A second QB starts a handful of times a year; a third never does.
BACKUP_DECAY = {"QB": 0.20, "TE": 0.28, "RB": 0.72, "WR": 0.70}
# Past these counts a player cannot realistically help you, whatever his projection.
ROSTER_CAP = {"QB": 2, "TE": 2, "RB": 6, "WR": 7}


def _positional_need(league: LeagueSettings, roster: dict[str, int]) -> dict[str, float]:
    """Multiplier per position, driven by the chance the player ever starts for you.

    An empty starting slot is worth a premium. A bench spot is worth only as much as
    the odds it gets pressed into the lineup, which decays fast at positions with one
    starting slot — the reason a great backup quarterback wins you nothing.
    """
    need = {}
    superflex = getattr(league, "superflex", 0) or 0
    flex_slots = league.starters.get("FLEX", 0)
    flex_filled = sum(max(0, roster.get(p, 0) - league.starters.get(p, 0))
                      for p in league.flex_eligible)

    # In superflex the second quarterback is a starter, not a bench body, so every
    # rule that assumes one QB slot has to shift: more of them are required, they
    # stay valuable deeper, and the early-round dampener must not apply.
    required_qb = league.starters.get("QB", 0) + superflex
    caps = dict(ROSTER_CAP)
    decay = dict(BACKUP_DECAY)
    if superflex:
        caps["QB"] = league.starters.get("QB", 0) + superflex + 1
        decay["QB"] = 0.55

    for pos in FANTASY_POSITIONS:
        required = required_qb if pos == "QB" else league.starters.get(pos, 0)
        have = roster.get(pos, 0)
        cap = caps.get(pos, 6)
        if have >= cap:
            need[pos] = 0.02
        elif have < required:
            need[pos] = 1.18
        elif pos in league.flex_eligible and flex_filled < flex_slots:
            need[pos] = 1.05
        else:
            depth = have - required + (1 if pos in league.flex_eligible
                                       and flex_filled >= flex_slots else 0)
            need[pos] = max(0.03, decay.get(pos, 0.5) ** max(1, depth))

    # Don't let a lone QB/TE slot pull you into reaching in the early rounds, where
    # the elite RB and WR you'd be passing on are the scarcer resource. This is a
    # 1-QB argument only — in superflex, quarterbacks genuinely are the scarce
    # resource and reaching for one early is correct.
    filled = sum(roster.values())
    if not superflex and roster.get("QB", 0) == 0 and filled < 5:
        need["QB"] = min(need["QB"], 0.80)
    if roster.get("TE", 0) == 0 and filled < 3:
        need["TE"] = min(need["TE"], 0.88)
    return need


def explain(row: pd.Series) -> str:
    """Plain-language reasoning for a recommendation."""
    bits = []
    if row.get("pos_rank"):
        bits.append(f"{row['position']}{int(row['pos_rank'])} by projection "
                    f"({row['proj_points']:.0f} pts, {row['adj_ppg']:.1f}/gm)")
    c = row.get("consistency")
    if c is not None and np.isfinite(c):
        sr = row.get("startable_rate")
        bits.append(f"consistency {c:.2f}" + (f", startable in {sr:.0%} of weeks" if np.isfinite(sr or np.nan) else ""))
    for label, key, good_high in [
        ("O-line", "m_oline", True), ("volume/pace", "m_volume", True),
        ("schedule", "m_schedule", True), ("age curve", "m_age", True),
        ("separation", "m_separation", True),
    ]:
        v = row.get(key)
        if v is not None and np.isfinite(v) and abs(v - 1) > 0.02:
            bits.append(f"{label} {'+' if v > 1 else ''}{(v - 1) * 100:.1f}%")
    ir = row.get("injury_risk")
    if ir is not None and np.isfinite(ir):
        bits.append(f"injury risk {ir:.0%} (~{row.get('exp_games', 17):.0f} games)")
    p = row.get("p_available_next")
    if p is not None and np.isfinite(p):
        bits.append(f"{p:.0%} chance he lasts to your next pick")
    return "; ".join(bits)
