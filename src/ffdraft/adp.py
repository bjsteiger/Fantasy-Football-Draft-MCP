"""Preseason rankings vs. actual finish — the 'value pick' engine.

Draft position is a market price. What matters is which players systematically beat
that price. This module pairs FantasyPros preseason expert consensus rank (a very
close stand-in for ADP, published back to 2020) with the fantasy points each player
actually finished with, so hit rates can be measured rather than assumed.

Source: dynastyprocess/data, which mirrors FantasyPros ECR history.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import sources
from .config import FANTASY_POSITIONS, Scoring
from .sources import _cached

ECR_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr.parquet"

# A player has to be startable-relevant for a hit/bust label to mean anything.
DRAFTABLE_ECR_CUTOFF = 200


def _ecr_raw(page_type: str = "redraft-overall") -> pd.DataFrame:
    def build():
        d = pd.read_parquet(ECR_URL)
        d = d[d["page_type"].isin(["redraft-overall", "redraft-op"])]
        d = d[d["pos"].isin(FANTASY_POSITIONS)]
        keep = ["player", "pos", "tm", "ecr", "sd", "best", "worst",
                "scrape_date", "page_type"]
        return d[[c for c in keep if c in d.columns]].copy()

    d = _cached("fp_ecr_redraft", build, max_age_days=1.0)
    d = d[d["page_type"] == page_type]
    d = d.copy()
    d["scrape_date"] = pd.to_datetime(d["scrape_date"])
    return d


def preseason_ecr(season: int, superflex: bool = False) -> pd.DataFrame:
    """Consensus rank as of the last August scrape before the given season.

    August is the right snapshot: rankings before then still price in players who
    won't make a roster, and September rankings have already absorbed preseason
    injuries, which would leak information into a backtest.

    Superflex leagues get their own consensus, because the two markets barely
    resemble each other — in 2026 Josh Allen is the 26th pick in a 1-QB league and
    the 1st overall pick in superflex. Pricing a superflex draft off 1-QB rankings
    would make every quarterback look like a bargain and wreck the whole
    opportunity-cost calculation.
    """
    from .names import normalize as norm_name

    d = _ecr_raw("redraft-op" if superflex else "redraft-overall")
    aug = d[(d["scrape_date"].dt.year == season) & (d["scrape_date"].dt.month.isin([7, 8]))]
    if aug.empty:  # fall back to the earliest snapshot in that season
        aug = d[(d["scrape_date"].dt.year == season) & (d["scrape_date"].dt.month <= 9)]
    if aug.empty:
        return pd.DataFrame(columns=["name", "position", "ecr", "_key"])
    latest = aug["scrape_date"].max()
    snap = aug[aug["scrape_date"] == latest].copy()
    snap = snap.rename(columns={"player": "name", "pos": "position", "tm": "team"})
    snap["ecr"] = pd.to_numeric(snap["ecr"], errors="coerce")
    snap = snap.dropna(subset=["ecr"]).sort_values("ecr")
    snap["adp"] = snap["ecr"]
    snap["pos_ecr"] = snap.groupby("position")["ecr"].rank(method="min").astype(int)
    snap["_key"] = snap["name"].map(norm_name)
    snap["snapshot"] = latest.date().isoformat()
    return snap.drop_duplicates("_key").reset_index(drop=True)


def season_finish(season: int, sc: Scoring | None = None,
                  te_bonus: float = 0.0) -> pd.DataFrame:
    """Where each player actually finished: total points, overall and positional rank."""
    from . import features
    from .names import normalize as norm_name

    sc = sc or Scoring()
    w = sources.weekly_stats([season])
    w = w[w["position"].isin(FANTASY_POSITIONS) & (w["season_type"] == "REG")].copy()
    w["fp"] = features.fantasy_points(w, sc, te_bonus)

    thresh = w["position"].map({"QB": 18.0, "RB": 12.0, "WR": 12.0, "TE": 9.0})
    w["startable"] = (w["fp"] >= thresh).astype(float)

    fin = w.groupby(["player_id", "player_display_name", "position"]).agg(
        games=("week", "nunique"),
        points=("fp", "sum"),
        ppg=("fp", "mean"),
        startable_rate=("startable", "mean"),
    ).reset_index().rename(columns={"player_display_name": "name"})

    fin["finish_pos_rank"] = fin.groupby("position")["points"].rank(
        ascending=False, method="min").astype(int)
    fin["finish_overall"] = fin["points"].rank(ascending=False, method="min").astype(int)
    fin["_key"] = fin["name"].map(norm_name)
    fin["season"] = season
    return fin


def _resolve_unmatched(merged: pd.DataFrame, fin: pd.DataFrame) -> pd.DataFrame:
    """Second pass for players whose ECR name doesn't match the stats name exactly.

    Ranking sites and nflverse disagree constantly on given names — "Josh Palmer"
    versus "Joshua Palmer", "Cam" versus "Cameron", "Marquise" versus "Hollywood".
    Left unresolved these look like players who scored zero all season, which would
    plant fabricated busts right in the middle of the results.
    """
    from difflib import get_close_matches

    miss = merged[merged["points"].isna() | (merged["points"] == 0)]
    if miss.empty:
        return merged

    for idx, row in miss.iterrows():
        pool = fin[fin["position"] == row["position"]]
        if pool.empty:
            continue
        keys = pool["_key"].tolist()
        # Same last name plus a compatible first initial is the reliable signal.
        parts = str(row["_key"]).split()
        if len(parts) >= 2:
            last, first_i = parts[-1], parts[0][:1]
            cand = pool[pool["_key"].str.endswith(" " + last)
                        & pool["_key"].str.startswith(first_i)]
            if len(cand) == 1:
                hit = cand.iloc[0]
                for c in ("points", "ppg", "games", "startable_rate",
                          "finish_pos_rank", "finish_overall"):
                    merged.loc[idx, c] = hit[c]
                merged.loc[idx, "match"] = "alias"
                continue
        close = get_close_matches(str(row["_key"]), keys, n=1, cutoff=0.88)
        if close:
            hit = pool[pool["_key"] == close[0]].iloc[0]
            for c in ("points", "ppg", "games", "startable_rate",
                      "finish_pos_rank", "finish_overall"):
                merged.loc[idx, c] = hit[c]
            merged.loc[idx, "match"] = "fuzzy"
    return merged


def _format_shift_ecr(pre: pd.DataFrame, season: int, sc: Scoring) -> pd.DataFrame:
    """Convert PPR consensus ranks to the league's format for a historical season.

    Published consensus is PPR, but the finishes it's being scored against use this
    league's scoring. Comparing the two directly would systematically flag every
    reception-heavy receiver as a bust in a standard league and every early-down
    back as a hit, which is an artefact of the mismatch rather than a real finding.

    The reception estimate uses the *prior* season's catches — what a drafter
    actually knew in August. Using the season's own receptions would leak the
    result being measured back into the prediction.
    """
    gap = 1.0 - float(sc.rec)
    if abs(gap) < 1e-9:
        pre = pre.copy()
        pre["ecr_format"] = "ppr"
        return pre

    from .names import normalize as norm_name

    try:
        prior = sources.weekly_stats([season - 1])
    except Exception:
        pre = pre.copy()
        pre["ecr_format"] = "ppr (unconverted: no prior season)"
        return pre

    from . import features

    prior = prior[prior["season_type"] == "REG"].copy()
    prior["pts_ppr"] = features.fantasy_points(prior, Scoring.preset("ppr"))
    prior["pts_fmt"] = features.fantasy_points(prior, sc)
    tot = prior.groupby("player_display_name")[["pts_ppr", "pts_fmt"]].sum().reset_index()
    tot["_key"] = tot["player_display_name"].map(norm_name)

    p = pre.merge(tot[["_key", "pts_ppr", "pts_fmt"]], on="_key", how="left")
    # Players with no prior season sit at the median and simply don't move.
    have = p["pts_ppr"].notna() & p["pts_fmt"].notna()
    rank_ppr = p.loc[have, "pts_ppr"].rank(ascending=False, method="min")
    rank_fmt = p.loc[have, "pts_fmt"].rank(ascending=False, method="min")
    p["_shift"] = 0.0
    p.loc[have, "_shift"] = (rank_fmt - rank_ppr) * 0.6

    p["ecr_ppr"] = p["ecr"]
    p["ecr"] = (p["ecr"] + p["_shift"]).clip(lower=1.0)
    p = p.sort_values("ecr")
    p["pos_ecr"] = p.groupby("position")["ecr"].rank(method="min").astype(int)
    p["ecr_format"] = "half_ppr" if gap <= 0.6 else "standard"
    return p.reset_index(drop=True)


def adp_vs_finish(season: int, sc: Scoring | None = None) -> pd.DataFrame:
    """Join preseason rank to final finish for one season.

    Comparison is positional rank against positional rank. Overall ranks aren't
    comparable across positions — QB1 scoring 400 points and RB1 scoring 300 are
    both wild successes, but their overall finishes look nothing alike.
    """
    sc = sc or Scoring()
    pre = preseason_ecr(season)
    if pre.empty:
        return pd.DataFrame()
    pre = _format_shift_ecr(pre, season, sc)
    fin = season_finish(season, sc)

    keep = ["_key", "name", "position", "team", "ecr", "pos_ecr", "sd", "snapshot"]
    keep += [c for c in ("ecr_ppr", "ecr_format") if c in pre.columns]
    m = pre[keep].merge(
        fin[["_key", "points", "ppg", "games", "startable_rate",
             "finish_pos_rank", "finish_overall"]],
        on="_key", how="left",
    )
    m["match"] = np.where(m["points"].notna(), "exact", "none")
    m = _resolve_unmatched(m, fin)

    m["season"] = season
    # A player still unmatched after alias and fuzzy passes either genuinely never
    # took a snap, or the name is unresolvable. Either way he's excluded from hit
    # and bust rates rather than counted as a zero, which would bias them.
    m["unresolved"] = m["points"].isna()
    m["finish_pos_rank"] = m["finish_pos_rank"].fillna(999)
    m["points"] = m["points"].fillna(0.0)
    m["games"] = m["games"].fillna(0)
    m["pos_rank_delta"] = m["pos_ecr"] - m["finish_pos_rank"]
    m["draft_round"] = np.ceil(m["ecr"] / 12).astype(int)

    # Value is measured in points against what that draft slot actually returned,
    # not in rank movement. Rank movement is unfair to early picks — an RB drafted
    # RB2 cannot rise a tier, and every drafted player gets pushed down the final
    # standings by undrafted breakouts, which would label whole rounds as busts.
    # "If you spent RB5 capital, did you get RB5 production?" is the honest test.
    m["expected_points"] = np.nan
    for pos, chunk in m.groupby("position"):
        curve = fin[fin["position"] == pos].sort_values("points", ascending=False)
        pts = curve["points"].to_numpy()
        if len(pts) == 0:
            continue
        slots = chunk["pos_ecr"].clip(1, len(pts)).astype(int).to_numpy() - 1
        m.loc[chunk.index, "expected_points"] = pts[slots]

    m["value_points"] = m["points"] - m["expected_points"]
    m["value_ratio"] = m["points"] / m["expected_points"].replace(0, np.nan)
    m["hit"] = m["value_ratio"] >= 1.15
    m["bust"] = m["value_ratio"] <= 0.70
    m["value_score"] = m["value_ratio"] - 1
    return m.sort_values("ecr").reset_index(drop=True)


def value_history(seasons: list[int], sc: Scoring | None = None) -> pd.DataFrame:
    """Stack multiple seasons of preseason-rank vs finish."""
    frames = []
    for s in seasons:
        try:
            df = adp_vs_finish(s, sc)
            if not df.empty:
                frames.append(df)
        except Exception as exc:
            print(f"  ! {s}: {type(exc).__name__}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def hit_rates(hist: pd.DataFrame, by: str = "draft_round") -> pd.DataFrame:
    """Hit and bust rates by draft round or position — where value actually lives."""
    h = hist[(hist["ecr"] <= DRAFTABLE_ECR_CUTOFF) & (~hist.get("unresolved", False))]
    g = h.groupby(by).agg(
        n=("hit", "size"),
        hit_rate=("hit", "mean"),
        bust_rate=("bust", "mean"),
        median_value_ratio=("value_ratio", "median"),
        mean_games=("games", "mean"),
    ).reset_index()
    return g.sort_values(by)


def matchup_value_backtest(seasons: list[int], position: str = "WR",
                           sc: Scoring | None = None) -> pd.DataFrame:
    """Did talent + schedule-adjusted matchup predict finish better than talent alone?

    Backtests the matchup_adjusted_score exposed by separation_report, the same way
    adp_vs_finish backtests consensus rank: nothing here has seen the season it's
    scoring.

    Talent (`talent_z`) is that player's separation score from the *prior* season
    only -- what a drafter actually knew in August, not a mid-season update. Matchup
    difficulty (`matchup_z`) comes from strength_of_schedule() computed exactly the
    way the live model computes it: opponent defensive strength is a recency-weighted
    blend of seasons strictly before the one being predicted, so the defense side
    can't leak either. The schedule itself (who plays whom) is legitimately known
    in advance -- the NFL publishes it -- so using it isn't leakage.

    Actual finish is real fantasy points from that season's box scores.
    """
    from . import features
    from . import separation as sep_mod

    sc = sc or Scoring()
    position = position.upper()
    frames = []
    for season in seasons:
        try:
            prior = sep_mod.separation_profile([season - 1])
            prior = prior[prior["qualified"] & (prior["position"] == position)]
            if prior.empty:
                print(f"  ! {season}: no qualified {position}s in {season - 1} to score talent from")
                continue
            talent = prior[["player_id", "name", "sep_score"]].rename(
                columns={"sep_score": "talent_z"})

            fin = season_finish(season, sc)
            fin = fin[fin["position"] == position]
            if fin.empty:
                continue

            dfn = features.defense_ratings(sc=sc)
            sos = features.strength_of_schedule(season, dfn)
            sos_col = f"sos_{position}_z"
            if sos_col not in sos.columns:
                print(f"  ! {season}: no schedule data for {position}")
                continue

            w = sources.weekly_stats([season])
            w = w[w["position"] == position]
            team_of = (w.sort_values("week").groupby("player_id")["recent_team"]
                       .last().rename("team").reset_index())

            m = fin.merge(talent, on="player_id", how="inner")
            if m.empty:
                continue
            m = m.merge(team_of, on="player_id", how="left")
            m = m.merge(sos[["team", sos_col]], on="team", how="left")
            m = m.rename(columns={sos_col: "matchup_z"})
            m["matchup_z"] = m["matchup_z"].fillna(0.0)
            m["matchup_adjusted_score"] = m["talent_z"] + m["matchup_z"]
            m["season"] = season
            frames.append(m)
        except Exception as exc:
            print(f"  ! {season}: {type(exc).__name__}: {exc}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def matchup_backtest_summary(hist: pd.DataFrame, top_n: int = 24) -> dict:
    """Compare talent-only vs matchup-adjusted score against actual finish.

    Spearman (rank) correlation against actual fantasy points, since raw fantasy
    points are heavily right-skewed and a rank-based measure is what actually
    matters for draft decisions. Also a top-N precision check computed per season
    then averaged: of the players each metric would have ranked in the top N, what
    share actually finished top N that season. N=24 is roughly the WR2-or-better
    cutoff in a 12-team league.

    A positive `improvement_corr` / `improvement_precision` means the matchup
    adjustment helped. Near zero or negative means talent alone did just as well,
    and the adjustment isn't earning its added complexity.
    """
    h = hist.dropna(subset=["talent_z", "matchup_adjusted_score", "points"]).copy()
    if h.empty:
        return {"n_player_seasons": 0}

    def spearman(a, b):
        return float(pd.Series(a).rank().corr(pd.Series(b).rank()))

    talent_precisions, matchup_precisions = [], []
    for _season, chunk in h.groupby("season"):
        chunk = chunk.copy()
        chunk["actual_top"] = chunk["points"].rank(ascending=False, method="min") <= top_n
        talent_top = chunk.sort_values("talent_z", ascending=False).head(top_n)
        matchup_top = chunk.sort_values("matchup_adjusted_score", ascending=False).head(top_n)
        if len(talent_top):
            talent_precisions.append(talent_top["actual_top"].mean())
        if len(matchup_top):
            matchup_precisions.append(matchup_top["actual_top"].mean())

    talent_corr = spearman(h["talent_z"], h["points"])
    matchup_corr = spearman(h["matchup_adjusted_score"], h["points"])
    talent_prec = float(np.mean(talent_precisions)) if talent_precisions else float("nan")
    matchup_prec = float(np.mean(matchup_precisions)) if matchup_precisions else float("nan")

    return {
        "n_player_seasons": int(len(h)),
        "seasons": sorted(int(s) for s in h["season"].unique()),
        "top_n": top_n,
        "talent_only_corr": talent_corr,
        "matchup_adjusted_corr": matchup_corr,
        "improvement_corr": matchup_corr - talent_corr,
        "talent_only_top_n_precision": talent_prec,
        "matchup_adjusted_top_n_precision": matchup_prec,
        "improvement_precision": matchup_prec - talent_prec,
    }


def repeat_value_players(hist: pd.DataFrame, min_seasons: int = 2) -> pd.DataFrame:
    """Players who beat their draft slot repeatedly rather than once.

    One outperformance is a season; two or three is a trait. This is the closest
    thing in the data to a list of players the market persistently underrates.
    """
    h = hist[(hist["ecr"] <= DRAFTABLE_ECR_CUTOFF) & (~hist.get("unresolved", False))]
    g = h.groupby(["_key", "name", "position"]).agg(
        seasons=("season", "nunique"),
        hits=("hit", "sum"),
        busts=("bust", "sum"),
        avg_value_ratio=("value_ratio", "mean"),
        avg_ecr=("ecr", "mean"),
        avg_games=("games", "mean"),
    ).reset_index()
    g = g[g["seasons"] >= min_seasons]
    g["hit_rate"] = g["hits"] / g["seasons"]
    return g.sort_values("avg_value_ratio", ascending=False).reset_index(drop=True)
