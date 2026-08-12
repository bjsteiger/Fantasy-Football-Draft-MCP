"""Rookie projections.

Rookies have no NFL history, so the veteran pipeline has nothing to regress. But they
are not unknowable: draft capital is a strong, well-behaved predictor of first-year
fantasy production, because it encodes both the league's talent evaluation and the
opportunity a team commits to a player it just spent a high pick on.

The curve here is fitted from actual rookie seasons rather than assumed. For each
position, first-year points per game is regressed on log draft pick across every draft
class in the window, then the landing spot's environment is applied on top using the
same multipliers veterans get.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import sources
from .config import CURRENT_SEASON, FANTASY_POSITIONS, Scoring
from .names import normalize
from .sources import _cached

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
# Undrafted free agents are modelled as if picked here.
UDFA_PICK = 275
# How many draft classes to fit the curve on.
FIT_SEASONS = 10

# draft_picks comes from Pro Football Reference, which uses different team codes than
# the rest of nflverse. Left unmapped, every rookie's landing-spot join (O-line, pace,
# schedule) silently returns nothing and the rookie is modelled in a vacuum.
PFR_TEAM_FIX = {
    "LVR": "LV", "NOR": "NO", "SFO": "SF", "LAR": "LA", "TAM": "TB",
    "KAN": "KC", "GNB": "GB", "NWE": "NE", "SDG": "LAC", "STL": "LA",
    "OAK": "LV", "RAI": "LV", "PHO": "ARI", "ARZ": "ARI", "CLT": "IND",
    "RAV": "BAL", "HTX": "HOU", "OTI": "TEN", "CRD": "ARI", "BLT": "BAL",
    "JAC": "JAX", "WSH": "WAS",
}


def _fix_team(s: pd.Series) -> pd.Series:
    return s.astype("string").str.upper().replace(PFR_TEAM_FIX)


def draft_capital(pick: float) -> float:
    """0-1 score for how much opportunity a pick buys. Log scale, because the gap
    between pick 3 and pick 20 is enormous and the gap between 180 and 197 is nothing."""
    pick = float(np.clip(pick if pick and np.isfinite(pick) else UDFA_PICK, 1, UDFA_PICK))
    return float(np.clip(1.0 - np.log(pick) / np.log(UDFA_PICK), 0.0, 1.0))


def draft_picks() -> pd.DataFrame:
    """Every NFL draft pick, including the most recent class, with gsis ids."""
    def build():
        return pd.read_parquet(NFLVERSE + "/draft_picks/draft_picks.parquet")
    d = _cached("draft_picks", build, max_age_days=3.0)
    d = d[d["position"].isin(FANTASY_POSITIONS)].copy()
    d["name"] = d["pfr_player_name"]
    d["_key"] = d["name"].map(normalize)
    return d


def combine() -> pd.DataFrame:
    """Combine testing, for the athleticism signal on receivers and backs."""
    def build():
        return pd.read_parquet(NFLVERSE + "/combine/combine.parquet")
    c = _cached("combine", build, max_age_days=7.0)
    c = c[c["pos"].isin(FANTASY_POSITIONS)].copy()
    c["_key"] = c["player_name"].map(normalize)
    return c


def _rookie_seasons(seasons: list[int], sc: Scoring) -> pd.DataFrame:
    """Actual first-year production for historical draft classes."""
    from . import features

    picks = draft_picks()
    picks = picks[picks["season"].isin(seasons)]
    w = sources.weekly_stats(seasons)
    w = w[w["position"].isin(FANTASY_POSITIONS) & (w["season_type"] == "REG")].copy()
    w["fp"] = features.fantasy_points(w, sc)

    prod = w.groupby(["player_id", "season"]).agg(
        rookie_games=("week", "nunique"), points=("fp", "sum"), ppg=("fp", "mean"),
    ).reset_index()

    # draft_picks ships career columns of its own (games, pass_yards, ...), so drop
    # anything that would collide and silently get suffixed out from under us.
    collide = [c for c in prod.columns if c in picks.columns and c != "season"]
    picks = picks.drop(columns=collide, errors="ignore")

    m = picks.merge(prod, left_on=["gsis_id", "season"],
                    right_on=["player_id", "season"], how="left")
    # A drafted player with no stat line played no meaningful snaps. That is a real
    # rookie outcome, not missing data, so it stays in the fit as a zero.
    m["ppg"] = m["ppg"].fillna(0.0)
    m["games"] = m["rookie_games"].fillna(0)
    m["points"] = m["points"].fillna(0.0)
    m["pick"] = m["pick"].fillna(UDFA_PICK).clip(1, UDFA_PICK)
    return m


PICK_BINS = [0, 10, 25, 50, 100, 180, UDFA_PICK + 1]
BIN_LABELS = ["1-10", "11-25", "26-50", "51-100", "101-180", "180+"]


def fit_draft_curves(sc: Scoring | None = None, seasons: list[int] | None = None) -> dict:
    """Fit rookie points-per-game against draft pick, per position.

    Two estimators are kept, because neither is trustworthy alone. The log-linear fit
    is smooth and uses every data point, but it extrapolates badly at the very top of
    the draft where there are only a handful of observations — for backs it predicted
    19.4 PPG at pick 3 when the top-ten bin has actually averaged 15.9. The empirical
    bin medians are honest about that range but jump between bins and are noisy.

    Predictions blend the two and cap at the bin's observed 75th percentile, so the
    model can't promise a rookie outcome nobody in ten draft classes has produced.
    Medians rather than means, because rookie outcomes are heavily right-skewed: a
    few hits drag the average above what a typical pick at that slot returns.
    """
    sc = sc or Scoring()
    if seasons is None:
        end = CURRENT_SEASON - 1
        seasons = list(range(end - FIT_SEASONS + 1, end + 1))

    hist = _rookie_seasons(seasons, sc)
    hist = hist.assign(bin=pd.cut(hist["pick"], PICK_BINS, labels=BIN_LABELS))

    curves = {}
    for pos, g in hist.groupby("position"):
        g = g[g["pick"].notna()]
        if len(g) < 25:
            continue
        x = np.log(g["pick"].to_numpy())
        y = g["ppg"].to_numpy()
        slope, intercept = np.polyfit(x, y, 1)
        resid = y - (slope * x + intercept)

        bins = {}
        for label, chunk in g.groupby("bin", observed=True):
            if len(chunk) == 0:
                continue
            bins[str(label)] = {
                "n": int(len(chunk)),
                "median": float(chunk["ppg"].median()),
                "mean": float(chunk["ppg"].mean()),
                "p75": float(chunk["ppg"].quantile(0.75)),
                "played_rate": float((chunk["games"] >= 8).mean()),
            }

        curves[pos] = {
            "slope": float(slope), "intercept": float(intercept),
            "resid_sd": float(np.std(resid)), "n": int(len(g)),
            "seasons": [int(s) for s in seasons],
            "played_rate": float((g["games"] >= 8).mean()),
            "mean_games": float(g.loc[g["games"] > 0, "games"].mean() or 0),
            "bins": bins,
        }
    return curves


def _bin_for(pick: float) -> str:
    for lo, hi, label in zip(PICK_BINS[:-1], PICK_BINS[1:], BIN_LABELS):
        if lo < pick <= hi:
            return label
    return BIN_LABELS[-1]


def predict_rookie_ppg(position: str, pick: float, curves: dict) -> float:
    c = curves.get(position)
    if not c:
        return 0.0
    pick = float(np.clip(pick if pick and np.isfinite(pick) else UDFA_PICK, 1, UDFA_PICK))
    fit = c["slope"] * np.log(pick) + c["intercept"]

    b = c.get("bins", {}).get(_bin_for(pick))
    if not b:
        return float(max(0.0, fit))
    # Trust the smooth fit more where the bin is well populated, the empirical
    # median more where it is thin — which is exactly the top of the draft.
    w_fit = float(np.clip(b["n"] / (b["n"] + 12), 0.25, 0.75))
    blended = w_fit * fit + (1 - w_fit) * b["median"]
    return float(np.clip(blended, 0.0, b["p75"]))


def rookie_board(season: int = CURRENT_SEASON, sc: Scoring | None = None,
                 curves: dict | None = None) -> pd.DataFrame:
    """Projected rookies for the given season, before landing-spot adjustment."""
    sc = sc or Scoring()
    curves = curves or fit_draft_curves(sc)

    picks = draft_picks()
    cls = picks[picks["season"] == season].copy()
    if cls.empty:
        return pd.DataFrame()

    cls["pick"] = cls["pick"].fillna(UDFA_PICK).clip(1, UDFA_PICK)
    cls["draft_round"] = cls["round"].fillna(8)
    cls["capital"] = cls["pick"].map(draft_capital)
    cls["baseline_ppg"] = [
        predict_rookie_ppg(p, k, curves) for p, k in zip(cls["position"], cls["pick"])
    ]
    cls["ppg_sd"] = cls["position"].map(
        {p: c["resid_sd"] for p, c in curves.items()}).fillna(4.0)
    # Availability scales with draft capital, not just position. A top-five pick is on
    # the field from week one; a sixth-rounder is inactive half the year. Using the
    # positional average for both would badly understate the early picks.
    cls["exp_games"] = (17 * (0.42 + 0.55 * cls["capital"])).clip(5, 16.5)

    # Combine athleticism nudges skill-position rookies whose testing stands out.
    try:
        cmb = combine()
        cmb = cmb[cmb["season"] == season][["_key", "forty", "vertical", "broad_jump", "wt"]]
        cls = cls.merge(cmb.drop_duplicates("_key"), on="_key", how="left")
    except Exception:
        for c in ("forty", "vertical", "broad_jump", "wt"):
            cls[c] = np.nan

    cls = cls.rename(columns={"team": "draft_team"})
    cls["team"] = _fix_team(cls["draft_team"])
    cls["is_rookie"] = True
    cls["player_id"] = cls["gsis_id"]
    cls["seasons_played"] = 0
    cls["games_last"] = 0
    cls["age"] = cls.get("age", pd.Series(22.0, index=cls.index)).fillna(22.0)

    keep = ["player_id", "name", "_key", "position", "team", "draft_team", "pick",
            "draft_round", "baseline_ppg", "ppg_sd", "exp_games", "is_rookie",
            "seasons_played", "games_last", "age", "college", "forty", "vertical",
            "capital"]
    return cls[[c for c in keep if c in cls.columns]].reset_index(drop=True)


def rookie_consistency_prior(position: str, pick: float, curves: dict) -> float:
    """Expected week-to-week reliability for a rookie.

    Deliberately conservative. Rookies are the most volatile group in fantasy: roles
    shift mid-season, snap counts move with practice performance, and the floor is a
    healthy scratch. Even first-round picks sit well below a proven veteran starter.
    """
    c = curves.get(position, {})
    played = c.get("played_rate", 0.5)
    pick = float(np.clip(pick or UDFA_PICK, 1, UDFA_PICK))
    # Draft capital buys opportunity, which is most of rookie consistency.
    capital = float(np.clip(1.0 - np.log(pick) / np.log(UDFA_PICK), 0.0, 1.0))
    base = {"QB": 0.42, "RB": 0.46, "WR": 0.42, "TE": 0.34}.get(position, 0.40)
    return float(np.clip(base * (0.55 + 0.45 * capital) * (0.7 + 0.3 * played), 0.12, 0.62))
