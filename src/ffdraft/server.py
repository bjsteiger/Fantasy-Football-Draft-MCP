"""MCP server: a live fantasy football draft analyst.

Run with:  python -m ffdraft.server
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:  # mcp SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import adp as adp_mod
from . import board as bd
from . import features, model, sources
from .config import (
    CURRENT_SEASON,
    DATA_DIR,
    STATE_DIR,
    LeagueSettings,
    ModelWeights,
    Scoring,
    board_cache_key,
    delete_league,
    load_settings,
    save_settings,
    set_active,
)
from .config import list_leagues as cfg_list_leagues

mcp = _Server("fantasy-draft-analyst")

_CACHE: dict[str, Any] = {"league": None, "weights": None, "adp_csv": {}}
# Boards are keyed by the settings that actually change them, so a 10-team full-PPR
# league and a 13-team half-PPR league each keep their own and switching between
# them is instant rather than an eight-second rebuild.
_BOARDS: dict[str, pd.DataFrame] = {}


def _scoring_label(league: LeagueSettings) -> str:
    """ppr / half_ppr / standard. Anything unusual is treated as standard, which is
    the conservative choice: it assumes no reception credit rather than inventing one."""
    r = float(league.scoring.rec)
    if r >= 0.9:
        return "ppr"
    if r >= 0.35:
        return "half_ppr"
    return "standard"


def _board_path(league: LeagueSettings, weights: ModelWeights) -> Path:
    return DATA_DIR / f"board_{board_cache_key(league, weights)}.parquet"


# ---------------------------------------------------------------- internals

class LeagueIdError(ValueError):
    """A league id that cannot be used, caught at the tool boundary."""


def _league_id(value: str | int | None) -> str | None:
    """Normalise a league id arriving from any MCP client.

    League ids are digit-only, so a client serialising JSON faithfully sends a
    number rather than a string. A strict `str` annotation rejects that before
    the tool body ever runs, and the obvious recovery -- quoting the value --
    used to be worse: nothing normalised the id, so `"1431833696"` was
    interpolated into the ESPN URL as `%221431833696%22` and came back an
    opaque 400. Both are the caller doing something reasonable, so accept both
    and strip the quoting rather than making them guess.

    Validated here, before the id reaches a URL, so a malformed one says what is
    wrong instead of surfacing as an HTTP error. Every consumer is an ESPN
    endpoint (`espn_scoring_items`, `sync_espn`, `espn_league_context`) and ESPN
    ids are digits, so that check is safe.
    """
    if value is None:
        return None
    text = str(value).strip().strip("\"'").strip()
    if not text:
        return None
    if not text.isdigit():
        raise LeagueIdError(
            f"league_id must be digits, got {text!r} -- it is the number in "
            "your league's URL, e.g. .../leagues/1431833696"
        )
    return text


def _step_failure(exc: Exception) -> str:
    """Describe a failed prewarm step.

    The class name alone had to cover a malformed league id, expired
    espn_s2/SWID cookies, a league the logged-in user can't see, and ESPN being
    down -- four problems with four different fixes, reported as one word.
    prewarm is meant to be run shortly before a draft, which is exactly when an
    unactionable diagnosis costs the most, so keep the message. Truncated
    because a stray HTML error page would otherwise swamp the response.
    """
    detail = str(exc).strip()
    return (f"failed: {type(exc).__name__}: {detail[:200]}"
            if detail else f"failed: {type(exc).__name__}")


def _settings() -> tuple[LeagueSettings, ModelWeights]:
    if _CACHE["league"] is None:
        _CACHE["league"], _CACHE["weights"] = load_settings()
    return _CACHE["league"], _CACHE["weights"]


def _build_board(force: bool = False) -> pd.DataFrame:
    league, weights = _settings()
    key = board_cache_key(league, weights)
    path = _board_path(league, weights)

    if not force and key in _BOARDS:
        return _BOARDS[key]
    if not force and path.exists():
        b = pd.read_parquet(path)
        _BOARDS[key] = b
        return b

    tbl = model.build_player_table(league, weights)
    proj = model.project(tbl, league, weights)
    try:
        adp = bd.load_adp(
            csv_path=(_CACHE["adp_csv"] or {}).get(league.name),
            superflex=bool(getattr(league, "superflex", 0)),
        )
    except Exception as exc:
        print(f"ADP unavailable ({type(exc).__name__}); using model rank as proxy")
        adp = None
    proj = bd.attach_adp(proj, adp)
    proj = bd.convert_adp_format(proj, _scoring_label(league))
    # One row per player. A player who changed teams mid-window came through
    # twice -- Gabe Davis appeared as both BUF and JAX. Both rows shared a _key,
    # so drafting him correctly retired both and he could not be taken twice,
    # but he still occupied two board slots and could be listed twice. Keep the
    # better-projected row, which is the one carrying his fuller season.
    if "_key" in proj.columns:
        proj = (proj.sort_values("proj_points", ascending=False)
                    .drop_duplicates("_key", keep="first")
                    .reset_index(drop=True))
    proj.to_parquet(path, index=False)
    _BOARDS[key] = proj
    return proj


def _state() -> bd.DraftState:
    league, _ = _settings()
    return bd.DraftState(league)


def _mark_drafted(b: pd.DataFrame, state: bd.DraftState) -> pd.DataFrame:
    b = b.copy()
    b["drafted"] = b["_key"].isin(state.taken_keys())
    return b


def _rows(df: pd.DataFrame, cols: list[str], n: int) -> list[dict]:
    out = []
    for _, r in df.head(n).iterrows():
        d = {}
        for c in cols:
            v = r.get(c)
            if isinstance(v, (np.floating, float)):
                v = None if not np.isfinite(v) else round(float(v), 3)
            elif isinstance(v, (np.integer,)):
                v = int(v)
            elif isinstance(v, (np.bool_,)):
                v = bool(v)
            d[c] = v
        out.append(d)
    return out



def _roster_needs(league: LeagueSettings, counts: dict) -> dict:
    from . import idp as idp_mod
    return idp_mod.roster_needs(league.starters, counts,
                                flex_eligible=league.flex_eligible)



def _idp_pointer(*, name: str | None = None, position: str | None = None) -> dict | None:
    """A signpost to idp_report when a caller asks the offence board about a defender.

    Without this the answer is a dead end that reads as a different problem
    entirely: asking player_report about a linebacker returns "no match for
    'Fred Warner'", which says the name is wrong rather than that defenders live
    on another board. best_available and separation_report just returned nothing
    at all. Being told where to look is the whole point -- the split between the
    two boards is an implementation detail, and a caller should not have to know
    it exists to get an answer.
    """
    from . import idp as idp_mod
    if position and str(position).upper() in idp_mod.DEFENSIVE_GROUPS:
        return {"error": f"{str(position).upper()} is a defensive position, which "
                         f"this board does not cover.",
                "use_instead": "idp_report",
                "why": "Defensive players are projected separately -- none of the "
                       "offence model's inputs (target share, route separation, "
                       "O-line) mean anything for a defender."}
    if name:
        try:
            defenders = idp_mod.defender_names(sources.weekly_stats([CURRENT_SEASON - 1]))
        except Exception:
            return None
        if name in defenders:
            return {"error": f"{name} is a defensive player, so he is not on this board.",
                    "use_instead": "idp_report",
                    "why": "Defensive players are projected separately -- none of the "
                           "offence model's inputs mean anything for a defender."}
    return None
def _roster_with_idp(state, b: pd.DataFrame, league: LeagueSettings) -> dict:
    """Your roster counts, including defenders the offence board cannot see.

    DraftState.my_roster resolves each pick against the offence board and skips
    what it cannot find, so a drafted linebacker leaves no trace. Shared by
    draft_status and the recommendation path so they cannot disagree about
    whether your IDP slot is filled -- which they did before this existed.
    """
    counts = dict(state.my_roster(b))
    if not int(league.starters.get("IDP", 0) or 0):
        return counts
    from . import idp as idp_mod
    try:
        defenders = idp_mod.defender_names(sources.weekly_stats([CURRENT_SEASON - 1]))
    except Exception:
        return counts
    resolved = set(b["_key"]) if "_key" in b.columns else set()
    for p in (pk for pk in state.picks if pk["slot"] == state.my_slot):
        if bd.norm_name(p["name"]) not in resolved and p["name"] in defenders:
            counts["IDP"] = counts.get("IDP", 0) + 1
    return counts


def _idp_option(league: LeagueSettings, roster: dict, current_pick: int,
                league_id: str | None = None) -> dict | None:
    """The best defender still worth taking, when the IDP slot is still open.

    Reported alongside the ranked recommendations rather than inside them, and
    that is a deliberate limit rather than laziness. The offence recommendations
    are ranked by opportunity cost -- value weighed against the chance a player
    survives to your next pick -- and survival needs a draft market. Defenders
    do not have one: published IDP consensus correlated 0.30 with actual pick in
    a real league, so there is no honest survival probability to compute. Slotting
    a defender into that ranked list would imply a comparability that does not
    exist.

    What can be compared is value over replacement, because a weekly score sums
    starters wherever they line up. So the defender is shown with his VOR next to
    the offensive alternatives, and the reader makes the call with both in view --
    which is the whole point of not making them run a second tool.
    """
    if not int(league.starters.get("IDP", 0) or 0):
        return None
    if int(roster.get("IDP", 0) or 0) >= int(league.starters.get("IDP", 0) or 0):
        return None  # slot already filled
    if not league_id:
        return {"note": "This league starts a defensive player and the slot is "
                        "still open. Pass league_id to rank defenders -- scoring "
                        "differs too much between leagues to assume.",
                "use": "idp_report"}
    try:
        from . import idp as idp_mod
        items = bd.espn_scoring_items(league_id, CURRENT_SEASON - 1)
        scoring = idp_mod.scoring_from_espn(items)
        if not scoring:
            return None
        seasons = list(range(CURRENT_SEASON - 5, CURRENT_SEASON))
        board = idp_mod.build_board(sources.weekly_stats(seasons), scoring,
                                    seasons=seasons, teams=league.teams,
                                    idp_slots=int(league.starters.get("IDP", 1) or 1))
        if board.empty:
            return None
        top = board.iloc[0]
        return {
            "best_available": top["name"],
            "position": top.get("position"),
            "vor": float(top["vor"]),
            "proj_points": float(top["proj_points"]),
            "seasons_of_evidence": int(top.get("seasons_used", 1) or 1),
            "round": (current_pick - 1) // league.teams + 1,
            "how_to_read": "vor is directly comparable with the offensive "
                           "recommendations above -- a weekly score sums starters "
                           "regardless of position. There is no survival estimate "
                           "for defenders because they have no reliable draft "
                           "market, so this is not ranked against them.",
            "detail": "idp_report",
        }
    except Exception:
        return None



def _idp_plan(league: LeagueSettings, league_id: str | None) -> dict | None:
    """Which defender to target, and roughly when.

    Reported next to the plan rather than as one of its rounds. The plan is
    built pick by pick from ADP -- who realistically falls to you at each turn --
    and defenders have no usable draft position, so there is no honest way to
    say which round one lands in. What the league's own history does support is
    a window, so that is what is given.

    mock_draft is deliberately left alone. Its opponents are ADP bots, and
    without a defensive market there is nothing for them to draft against;
    inventing bot behaviour for a position whose real timing correlated 0.30
    with consensus would add noise and call it a simulation.
    """
    if not int(league.starters.get("IDP", 0) or 0):
        return None
    if not league_id:
        return {"note": "This league starts a defensive player. Pass league_id "
                        "to see which one to target and when.",
                "use": "idp_report"}
    try:
        from . import idp as idp_mod
        items = bd.espn_scoring_items(league_id, CURRENT_SEASON - 1)
        scoring = idp_mod.scoring_from_espn(items)
        if not scoring:
            return None
        seasons = list(range(CURRENT_SEASON - 5, CURRENT_SEASON))
        board = idp_mod.build_board(sources.weekly_stats(seasons), scoring,
                                    seasons=seasons, teams=league.teams,
                                    idp_slots=int(league.starters.get("IDP", 1) or 1))
        if board.empty:
            return None
        top = board.head(3)[["name", "position", "proj_points", "vor"]].to_dict("records")
        return {
            "targets": top,
            "note": "Not placed in a round above, because defenders have no "
                    "usable draft position -- consensus correlated 0.30 with "
                    "real picks. Use idp_report with timing_seasons to see when "
                    "they actually went in your league.",
            "detail": "idp_report",
        }
    except Exception:
        return None

# ---------------------------------------------------------------- tools

@mcp.tool()
def configure_league(name: str = "default", teams: int | None = None,
                     draft_slot: int | None = None, rounds: int | None = None,
                     scoring: str | None = None, snake: bool | None = None,
                     qb: int | None = None, rb: int | None = None,
                     wr: int | None = None, te: int | None = None,
                     flex: int | None = None, idp: int | None = None,
                     superflex: int | None = None,
                     te_premium_bonus: float | None = None,
                     consistency_weight: float | None = None,
                     adp_csv_path: str | None = None) -> str:
    """Set up a league, or change one you already have. Makes it the active league.

    Only the settings you pass are changed. Anything you leave out keeps the
    value it already had, so you can change one thing without retyping the rest:
    configure_league(name="home", idp=1) changes the defensive slot and touches
    nothing else. A brand-new name starts from the defaults below.

    New-league defaults: 12 teams, 16 rounds, pick 6, half PPR, snake, and
    1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX / 1 K / 1 DST / 0 IDP.

    scoring: ppr, half_ppr, or standard. superflex=1 adds a slot where a second
    quarterback may start. te_premium_bonus adds points per tight end catch.
    consistency_weight trades upside against week-to-week reliability (0 = pure
    upside, 1 = pure floor). Your other model weights are never touched here --
    tune those with model_settings.

    idp is how many individual defensive players you start (a linebacker,
    defensive back, edge rusher, or a generic defensive slot). This tool does
    not project defenders, so the count is used to keep the round math right --
    those rounds are left out of simulations instead of being filled with a
    recommendation the model cannot make. Rank defenders with idp_report.

    The response echoes every setting the league ended up with, so you can see
    what was kept and what changed.
    """
    # Every setting is merged into what the league already has rather than
    # rebuilt from parameter defaults. Rebuilding meant a call that named one
    # setting reset all the others: configure_league(name="x", idp=1) on a
    # 10-team full-PPR league silently made it a 12-team half-PPR league, which
    # moves replacement levels, the pick list and the board cache key. Same
    # trap as issue #32, which covered the model weights; this is issue #37 for
    # the league's own shape. An unknown name loads defaults, so creating a
    # league behaves exactly as before.
    known, _ = cfg_list_leagues()
    existed = name in known
    stored, weights = load_settings(name)

    def pick(passed, current):
        return current if passed is None else passed

    teams = int(pick(teams, stored.teams))
    draft_slot = int(pick(draft_slot, stored.draft_slot))
    rounds = int(pick(rounds, stored.rounds))
    snake = bool(pick(snake, stored.snake))
    superflex = int(pick(superflex, stored.superflex))
    te_premium_bonus = float(pick(te_premium_bonus, stored.te_premium_bonus))
    # Validated against the team count the league actually ends up with, not
    # the one that happened to be passed -- dropping to a 10-team league without
    # moving a slot-12 pick has to fail here rather than later.
    if not 1 <= draft_slot <= teams:
        return json.dumps({"error": f"draft_slot {draft_slot} is outside a {teams}-team league"})

    # K and DST come from the stored roster too. They are not parameters, so
    # hardcoding them here would have quietly rewritten a league that had been
    # set up with different counts some other way.
    starters = dict(stored.starters)
    for slot, passed in (("QB", qb), ("RB", rb), ("WR", wr), ("TE", te),
                         ("FLEX", flex), ("IDP", idp)):
        if passed is not None:
            starters[slot] = int(passed)

    league = LeagueSettings(
        name=name, teams=teams, rounds=rounds, draft_slot=draft_slot, snake=snake,
        scoring=stored.scoring if scoring is None else Scoring.preset(scoring),
        starters=starters, superflex=superflex, te_premium_bonus=te_premium_bonus,
    )
    if consistency_weight is not None:
        weights.consistency_weight = float(consistency_weight)
    save_settings(league, weights)

    csvs = dict(_CACHE.get("adp_csv") or {})
    if adp_csv_path:
        csvs[name] = adp_csv_path
    _CACHE.update({"league": league, "weights": weights, "adp_csv": csvs})

    known, _ = cfg_list_leagues()
    reused = _board_path(league, weights).exists()
    return json.dumps({
        "league": name,
        # Says plainly whether settings were inherited or started fresh.
        "status": "updated existing league" if existed else "created new league",
        "active": True, "teams": teams, "your_slot": draft_slot,
        "rounds": rounds, "snake": snake,
        "scoring": _scoring_label(league), "superflex": superflex,
        "te_premium_bonus": te_premium_bonus,
        # The full roster and weights, every time. Merging settings means a
        # short call can inherit values you set months ago, so the response has
        # to show what the league actually is rather than what you just typed.
        "starters": starters,
        "weights": asdict(weights),
        "your_picks": league.picks_for_slot()[:rounds],
        "replacement_levels": league.replacement_ranks(),
        "all_leagues": known,
        "board": "already cached for these settings" if reused
                 else "will build on your next query",
    }, indent=2)


@mcp.tool()
def list_leagues() -> str:
    """Every league you've set up, and which one is active."""
    known, active = cfg_list_leagues()
    out = []
    for nm in known:
        lg, _ = load_settings(nm)
        state = bd.DraftState(lg)
        out.append({
            "name": nm, "active": nm == active, "teams": lg.teams,
            "scoring": ("ppr" if lg.scoring.rec >= 1 else
                        "standard" if lg.scoring.rec == 0 else "half_ppr"),
            "your_slot": lg.draft_slot, "superflex": lg.superflex,
            "picks_recorded": len(state.picks),
        })
    return json.dumps({"active": active, "leagues": out}, indent=2)


@mcp.tool()
def switch_league(name: str) -> str:
    """Make a different league active. Its board and draft resume where you left them."""
    if not set_active(name):
        known, _ = cfg_list_leagues()
        return json.dumps({"error": f"no league named '{name}'", "available": known})
    league, weights = load_settings(name)
    _CACHE.update({"league": league, "weights": weights})
    state = bd.DraftState(league)
    return json.dumps({
        "active": name, "teams": league.teams, "your_slot": league.draft_slot,
        "scoring": ("ppr" if league.scoring.rec >= 1 else
                    "standard" if league.scoring.rec == 0 else "half_ppr"),
        "board": "cached" if _board_path(league, weights).exists() else "will build on next query",
        **state.summary(),
    }, indent=2)


@mcp.tool()
def remove_league(name: str) -> str:
    """Delete a league and its draft history. The board cache is left alone, since
    other leagues with the same format may share it."""
    if not delete_league(name):
        known, _ = cfg_list_leagues()
        return json.dumps({"error": f"no league named '{name}'", "available": known})
    p = STATE_DIR / f"draft_{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.json"
    if p.exists():
        p.unlink()
    if (_CACHE.get("league") or LeagueSettings()).name == name:
        _CACHE.update({"league": None, "weights": None})
    known, active = cfg_list_leagues()
    return json.dumps({"removed": name, "remaining": known, "active": active}, indent=2)


@mcp.tool()
def refresh_data(force_download: bool = False) -> str:
    """Rebuild the player board from source data. Run once before draft day."""
    if force_download:
        for p in sources.CACHE_DIR.glob("*.parquet"):
            p.unlink()
    sources.clear_memory_cache()
    features.clear_derived_cache()
    _BOARDS.clear()
    b = _build_board(force=True)
    league, _ = _settings()
    return json.dumps({
        "players_modelled": len(b),
        "by_position": b["position"].value_counts().to_dict(),
        "seasons": sorted(int(s) for s in sources.weekly_stats()["season"].unique()),
        "datasets_cached": sources.cache_status(),
        "top_10": _rows(b, ["name", "position", "team", "proj_points", "consistency", "adp"], 10),
    }, indent=2, default=str)


@mcp.tool()
def best_available(position: str | None = None, limit: int = 15,
                   sort_by: str = "draft_score") -> str:
    """The next best players still on the board.

    sort_by: draft_score (balanced), vor (raw value), consistency (floor),
    proj_points, or value (biggest gap between ADP and model rank).
    """
    ptr = _idp_pointer(position=position)
    if ptr:
        return json.dumps(ptr, indent=2)
    b = _mark_drafted(_build_board(), _state())
    avail = b[~b["drafted"]]
    if position:
        avail = avail[avail["position"] == position.upper()]
    key = {"value": "adp_delta"}.get(sort_by, sort_by)
    if key not in avail.columns:
        key = "draft_score"
    avail = avail.sort_values(key, ascending=False)
    cols = ["name", "position", "team", "overall_rank", "pos_rank", "adp", "adp_delta",
            "proj_points", "adj_ppg", "consistency", "startable_rate", "injury_risk", "vor"]
    return json.dumps({"sorted_by": key, "players": _rows(avail, cols, limit)}, indent=2)


@mcp.tool()
def who_should_i_pick(limit: int = 6, league_id: str | int | None = None) -> str:
    """The live draft-analyst call: who to take right now, and why.

    Weighs projected value, week-to-week consistency, your roster's open starting
    slots, and the odds each player survives to your next pick.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    league, _ = _settings()
    state = _state()
    b = _mark_drafted(_build_board(), state)
    nxt = state.next_pick_for_me()
    on_clock = state.on_the_clock
    if nxt is not None and nxt > on_clock:
        current = nxt  # you're not up yet; evaluate for your actual next pick
    else:
        current = on_clock
    after = state.pick_after_next() if nxt == current else nxt

    roster = _roster_with_idp(state, b, league)
    recs = model.recommend(b, league, current_pick=current, next_pick=after,
                           roster=roster, top_n=limit)

    picks = []
    for _, r in recs.iterrows():
        picks.append({
            "player": r["name"], "position": r["position"], "team": r.get("team"),
            "adp": round(float(r["adp"]), 1),
            "proj_points": round(float(r["proj_points"]), 1),
            "consistency": round(float(r["consistency"]), 3),
            "survives_to_next_pick": round(float(r["p_available_next"]), 2),
            "why": model.explain(r),
        })
    return json.dumps({
        "evaluating_pick": current,
        "round": (current - 1) // league.teams + 1,
        "your_next_pick_after_this": after,
        "picks_you_wait": (after - current) if after else None,
        "your_roster": roster,
        "recommendations": picks,
        "idp_option": _idp_option(league, roster, current, league_id),
        "headline": (f"Take {picks[0]['player']} — {picks[0]['why']}" if picks else "Board empty"),
    }, indent=2)


@mcp.tool()
def record_pick(player_name: str, overall_pick: int | None = None,
                team_slot: int | None = None) -> str:
    """Log a pick that just happened. Use after every pick if you aren't auto-syncing."""
    state = _state()
    b = _build_board()
    row = bd.match_player(player_name, b)
    resolved = row["name"] if row is not None else player_name
    pick = state.record(resolved, overall_pick, team_slot)
    return json.dumps({
        "recorded": pick,
        "matched_to": resolved if row is not None else "no model match (logged as typed)",
        "position": (row["position"] if row is not None else None),
        **state.summary(),
    }, indent=2)


@mcp.tool()
def sync_draft(platform: str, league_id: str | int | None = None, draft_id: str | None = None,
               pasted_board: str | None = None, season: int = CURRENT_SEASON) -> str:
    """Pull the current draft board from your platform.

    platform="sleeper" with draft_id -- fully automatic, public API.
    platform="espn" with league_id -- works for public leagues; private ones need
      ESPN_SWID and ESPN_S2 environment variables from a logged-in browser session.
    platform="paste" with pasted_board -- paste the drafted list from any site.

    season defaults to the current one. A draft you already ran is under its own
    season, so pass it: an in-progress season has no picks yet.

    If the read fails you get an error saying what ESPN said -- the status code,
    what it means, and what to try -- rather than a bare failure.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    state = _state()
    b = _build_board()
    platform = platform.lower()

    if platform == "sleeper":
        if not draft_id:
            return json.dumps({"error": "draft_id required for Sleeper"})
        try:
            picks = bd.sync_sleeper(draft_id)
        except Exception as exc:
            return json.dumps({"error": f"couldn't read the Sleeper draft: {exc}",
                               "draft_id": draft_id}, indent=2)
    elif platform == "espn":
        if not league_id:
            return json.dumps({"error": "league_id required for ESPN"})
        # A failed read is answered, not raised. Raising reached the client as
        # "Error executing tool sync_draft" with the status code and reason
        # stripped off, so expired cookies, a wrong league id and an ESPN
        # outage all looked identical from the outside. See issue #46.
        try:
            picks = bd.sync_espn(league_id, season)
        except bd.EspnError as exc:
            return json.dumps({"error": str(exc), "status": exc.status,
                               "league_id": league_id, "season": season}, indent=2)
        except Exception as exc:
            return json.dumps({
                "error": f"couldn't read the ESPN draft: {type(exc).__name__}: {exc}",
                "league_id": league_id, "season": season}, indent=2)
    elif platform == "paste":
        if not pasted_board:
            return json.dumps({"error": "pasted_board text required"})
        names = bd.parse_pasted_board(pasted_board)
        picks = [{"overall": i + 1, "slot": None, "name": n} for i, n in enumerate(names)]
    else:
        return json.dumps({"error": f"unknown platform '{platform}'"})

    state.reset()
    unmatched = []
    for p in picks:
        row = bd.match_player(p["name"], b)
        if row is None:
            unmatched.append(p["name"])
        state.record(row["name"] if row is not None else p["name"],
                     p.get("overall"), p.get("slot"))
    return json.dumps({
        "platform": platform, "picks_synced": len(picks),
        "unmatched_names": unmatched[:20],
        **state.summary(),
    }, indent=2)


@mcp.tool()
def draft_status() -> str:
    """Where the draft stands and what your roster looks like."""
    state = _state()
    b = _mark_drafted(_build_board(), state)
    mine = [p for p in state.picks if p["slot"] == state.my_slot]
    idx = b.set_index("_key")
    detail = []
    for p in mine:
        k = bd.norm_name(p["name"])
        r = idx.loc[k] if k in idx.index else None
        detail.append({
            "pick": p["overall"], "player": p["name"],
            "position": (r["position"] if r is not None else None),
            "proj_points": (round(float(r["proj_points"]), 1) if r is not None else None),
        })
    league, _ = _settings()
    counts = _roster_with_idp(state, b, league)
    return json.dumps({**state.summary(), "my_team": detail,
                       "roster_counts": counts,
                       "roster_needs": _roster_needs(league, counts)}, indent=2)


@mcp.tool()
def undo_pick() -> str:
    """Remove the most recent pick — for when someone mis-enters the board."""
    state = _state()
    removed = state.undo()
    return json.dumps({"removed": removed, **state.summary()}, indent=2)


@mcp.tool()
def reset_draft() -> str:
    """Clear all recorded picks and start fresh."""
    state = _state()
    state.reset()
    return json.dumps({"reset": True, **state.summary()}, indent=2)


@mcp.tool()
def separation_report(position: str = "WR", player_name: str | None = None,
                      limit: int = 20) -> str:
    """Separation and route efficiency, plus the season-long matchup each player draws.

    avg_separation is NFL Next Gen Stats tracking data: yards of daylight between
    receiver and nearest defender when the ball arrives. YPRR and TPRR use routes
    estimated from snap share times team dropbacks. Only players who cleared 250
    routes and 50 targets in a season are included, so these are real workloads
    rather than flattering part-time rates.

    `matchup_z` is the receiver's own team's schedule difficulty for the upcoming
    season, from the same opponent-defense data that drives the model's schedule
    adjustment: positive means an easier slate (opponents allow more fantasy points
    to the position), negative means a tougher one. This is the open-data stand-in
    for a WR/CB matchup chart -- team-level and season-long rather than man-coverage
    and week-to-week, since which specific corner covers which receiver on a given
    snap isn't in any open dataset (that needs per-play charting only commercial
    providers do).

    matchup_z is informational only -- players are ranked by sep_score (talent), not
    by a blended score. A backtest (see matchup_backtest) found that folding schedule
    difficulty into a combined ranking made it a worse predictor of actual finish
    than talent alone for WR, so it isn't blended into the sort here.

    Man-versus-zone splits are not reproducible from open data — that needs
    per-play coverage charting.
    """
    from . import separation as sep_mod

    ptr = _idp_pointer(position=position, name=player_name)
    if ptr:
        # Route rather than return an empty leaderboard. Separation is a
        # receiving concept -- it has no meaning for a defender at all, so
        # "nothing found" would be the wrong answer as well as an unhelpful one.
        ptr["note"] = ("Separation measures getting open against coverage, which "
                       "is a receiving concept. There is no defensive equivalent "
                       "in this data.")
        return json.dumps(ptr, indent=2)

    prof = sep_mod.separation_profile()
    prof = prof[prof["qualified"]]
    if prof.empty:
        return json.dumps({"error": "no qualified players"})

    league, _ = _settings()
    dfn = features.defense_ratings(sc=league.scoring)
    sos = features.strength_of_schedule(CURRENT_SEASON, dfn)
    pos = position.upper()
    sos_col = f"sos_{pos}_z"
    sos_cols = ["team"] + ([sos_col] if sos_col in sos.columns else [])

    if player_name:
        row = bd.match_player(player_name, _build_board())
        target = bd.norm_name(row["name"]) if row is not None else bd.norm_name(player_name)
        hist = prof[prof["_key"] == target].sort_values("season")
        if sos_col in sos.columns:
            hist = hist.merge(sos[sos_cols], on="team", how="left") \
                       .rename(columns={sos_col: "matchup_z"})
        cols = ["season", "team", "avg_separation", "avg_cushion", "yprr", "tprr",
                "rec_targets", "rec_yards", "routes_est", "sep_score"]
        if "matchup_z" in hist.columns:
            cols.append("matchup_z")
        return json.dumps({
            "player": player_name,
            "by_season": _rows(hist, cols, 6),
        }, indent=2, default=str)

    recent = int(prof["season"].max())
    cur = prof[(prof["season"] == recent) & (prof["position"] == pos)].copy()
    cols = ["name", "team", "avg_separation", "avg_cushion", "yprr",
            "tprr", "rec_targets", "routes_est", "sep_score"]
    if sos_col in sos.columns and not cur.empty:
        cur = cur.merge(sos[sos_cols], on="team", how="left").rename(columns={sos_col: "matchup_z"})
        cols.append("matchup_z")
    cur = cur.sort_values("sep_score", ascending=False)
    return json.dumps({
        "season": recent, "position": pos, "schedule_season": CURRENT_SEASON,
        "note": "sep_score is a within-season z-score blending separation, YPRR, TPRR "
                "and YAC over expected -- players are ranked by this. matchup_z is "
                "informational only (positive = easier upcoming schedule for the "
                "position, negative = tougher): a backtest (matchup_backtest) found "
                "blending it into the ranking made predictions worse for WR, not "
                "better, so it's shown for reference but not part of the sort.",
        "players": _rows(cur, cols, limit),
    }, indent=2, default=str)


@mcp.tool()
def value_picks(limit: int = 20, direction: str = "undervalued") -> str:
    """Where the model disagrees with the draft market, on draftable players only.

    Positive gap means the model ranks a player higher than the room does — the
    players you can wait on and still get. Negative means the market is paying more
    than the model thinks they're worth.
    """
    league, _ = _settings()
    b = _mark_drafted(_build_board(), _state())
    avail = b[~b["drafted"]].copy()
    # Only players the market actually ranks. A synthetic fallback ADP means nobody
    # is drafting him, so calling him "undervalued" is meaningless — that is how a
    # fringe receiver kept surfacing next to real draft picks.
    if "adp_source" in avail.columns:
        consensus = avail["adp_source"].astype(str).str.startswith("consensus") | \
                    avail["adp_source"].astype(str).str.contains("ecr|csv", case=False)
        if consensus.any():
            avail = avail[consensus]
    avail = avail[avail["adp"] <= 220]
    avail["market_gap"] = avail["adp"] - avail["overall_rank"]
    asc = direction.lower().startswith("over")
    out = avail.sort_values("market_gap", ascending=asc)
    cols = ["name", "position", "team", "adp", "overall_rank", "pos_rank", "market_gap",
            "proj_points", "consistency", "injury_risk", "sep_score"]
    return json.dumps({
        "direction": direction,
        # mode() of an empty column has no rows to index. Late in a deep league
        # every consensus-ranked player inside the ADP cutoff can already be gone,
        # and .iloc[0] would turn that into an IndexError mid-draft.
        "adp_source": (str(avail["adp_source"].mode().iloc[0])
                       if "adp_source" in avail.columns and not avail.empty else "n/a"),
        "note": "market_gap > 0 means the model likes him more than his draft cost",
        "players": _rows(out, cols, limit),
    }, indent=2, default=str)


@mcp.tool()
def on_the_clock(platform: str, league_id: str | int | None = None, draft_id: str | None = None,
                 pasted_board: str | None = None, season: int = CURRENT_SEASON,
                 limit: int = 6) -> str:
    """The full on-the-clock workflow in one call: sync, status, pick, value, matchup.

    Runs, in order:
    1. sync_draft — a fresh pull from your platform, no cached state.
    2. draft_status — round, on-the-clock, and your roster, confirmed against the sync.
    3. who_should_i_pick — the recommendation, reasoning, and survival odds.
    4. value_picks — market-value context, scoped to this round and next.
    5. separation_report — only when the top recommendation is a WR or TE, that
       player's route efficiency and schedule context.

    Use this instead of calling each tool separately when you're on the clock and
    want the full picture in one shot. platform/league_id/draft_id/pasted_board/season
    are exactly sync_draft's arguments.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    sync = json.loads(sync_draft(platform, league_id, draft_id, pasted_board, season))
    if "error" in sync:
        return json.dumps({"step": "sync_draft", **sync}, indent=2)

    status = json.loads(draft_status())
    # Pass league_id through so an open IDP slot surfaces here too. on_the_clock
    # is the call made under a 90-second pick clock -- if the defender option
    # only appeared in a separate tool, it would not be seen in time to matter.
    rec = json.loads(who_should_i_pick(limit=limit, league_id=league_id))

    league, _ = _settings()
    rnd = rec.get("round", 1)
    lo, hi = (rnd - 1) * league.teams + 1, (rnd + 1) * league.teams
    pool = json.loads(value_picks(limit=100, direction="undervalued"))
    in_window = [p for p in pool.get("players", [])
                if p.get("adp") is not None and lo <= p["adp"] <= hi]

    result = {
        "sync": sync,
        "draft_status": status,
        "recommendation": rec,
        "value_picks_this_round": {
            "round_window": f"picks {lo}-{hi}",
            "direction": pool.get("direction"),
            "players": in_window[:8],
        },
    }

    picks = rec.get("recommendations") or []
    if picks and picks[0].get("position") in ("WR", "TE"):
        result["separation_report"] = json.loads(
            separation_report(position=picks[0]["position"], player_name=picks[0]["player"]))

    return json.dumps(result, indent=2)


@mcp.tool()
def draft_value_history(seasons: str = "2021,2022,2023,2024", group_by: str = "draft_round") -> str:
    """Backtest: how preseason consensus rank compared to where players actually finished.

    Value is measured in points against what that draft slot actually returned, so
    "did RB5 capital buy RB5 production?" Rank movement would be unfair to early
    picks and would label whole rounds as busts, because undrafted breakouts push
    every drafted player down the final standings.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.value_history(yrs, league.scoring)
    if hist.empty:
        return json.dumps({"error": "no ECR history available"})
    rates = adp_mod.hit_rates(hist, group_by)
    return json.dumps({
        "seasons": yrs,
        "players_analysed": int((hist["ecr"] <= adp_mod.DRAFTABLE_ECR_CUTOFF).sum()),
        "definitions": {"hit": "scored >=115% of the points that draft slot returned",
                        "bust": "scored <=70%"},
        "by_" + group_by: _rows(rates, list(rates.columns), 30),
    }, indent=2, default=str)


@mcp.tool()
def matchup_backtest(seasons: str = "2021,2022,2023,2024", position: str = "WR",
                     top_n: int = 24) -> str:
    """Backtest: does talent + schedule difficulty predict finish better than talent alone?

    This checks, against real seasons, whether blending schedule difficulty into a
    receiver's talent score improves how well it predicts actual finish. For each
    season, talent (`talent_z`) is that player's separation score from the *prior*
    season only, and matchup difficulty (`matchup_z`) is the same leakage-free
    strength_of_schedule the live recommender uses, both compared to actual fantasy
    points scored.

    A positive `improvement_corr` / `improvement_precision` means the schedule
    adjustment earns its keep. Near zero or negative means talent alone predicts
    just as well or better -- which is what a 2021-2024 WR backtest found, so
    separation_report ranks by talent (sep_score) alone and shows matchup_z as
    reference only. Re-run this if the underlying model changes.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.matchup_value_backtest(yrs, position.upper(), league.scoring)
    if hist.empty:
        return json.dumps({"error": "no matchup backtest data available for those seasons"})
    summary = adp_mod.matchup_backtest_summary(hist, top_n)

    swing = hist.copy()
    swing["swing"] = swing["matchup_z"].abs()
    swing_cols = ["name", "season", "team", "talent_z", "matchup_z",
                 "matchup_adjusted_score", "points", "finish_pos_rank"]
    biggest_swings = swing.sort_values("swing", ascending=False)

    return json.dumps({
        "position": position.upper(),
        "summary": summary,
        "interpretation": (
            "corr is Spearman rank correlation against actual fantasy points; "
            "top_n_precision is, of each metric's predicted top-N players, what "
            "share actually finished top-N that season, averaged across seasons"
        ),
        "biggest_schedule_swings": _rows(biggest_swings, swing_cols, 15),
    }, indent=2, default=str)


@mcp.tool()
def redzone_shift_backtest(seasons: str = "2022,2023,2024,2025", position: str = "WR",
                          top_n: int = 24) -> str:
    """Backtest: does a team's red zone play-calling identity improve on the
    touchdown-luck signal alone at predicting next season's fantasy points?

    Same idea and same discipline as matchup_backtest, applied to the
    `redzone_identity_shift` feature surfaced (informational only) through
    `team_context`. For each season, `talent_z` is the existing touchdown-luck
    signal `m_td_luck` already uses (a player's own prior-season red zone role vs.
    his position's baseline, z-scored), and `matchup_z` here is that player's
    team's red zone identity shift from that same prior season, z-scored across
    teams -- both leakage-free, both compared to real fantasy points scored.

    A positive `improvement_corr`/`improvement_precision` would mean the shift
    adjustment earns its keep and is worth wiring into `m_td_luck`. A 2022-2025
    run found the opposite for both WR (improvement_corr -0.006 across 300
    player-seasons) and TE (-0.053 across 117): red zone identity shift makes the
    prediction *worse*, not better -- the same conclusion matchup_backtest reached
    for schedule difficulty. This is why the shift stays informational-only in
    `team_context` rather than feeding `draft_score`. Re-run this if the underlying
    model or feature changes; only WR/TE are supported, since a pass-rate shift has
    no defensible sign for a running back.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.redzone_shift_backtest(yrs, position.upper(), league.scoring)
    if hist.empty:
        return json.dumps({"error": "no red zone shift backtest data available for those seasons"})
    summary = adp_mod.matchup_backtest_summary(hist, top_n)

    swing = hist.copy()
    swing["swing"] = swing["matchup_z"].abs()
    swing_cols = ["name", "season", "team", "talent_z", "matchup_z",
                 "matchup_adjusted_score", "points", "finish_pos_rank"]
    biggest_swings = swing.sort_values("swing", ascending=False)

    return json.dumps({
        "position": position.upper(),
        "summary": summary,
        "interpretation": (
            "corr is Spearman rank correlation against actual fantasy points; "
            "top_n_precision is, of each metric's predicted top-N players, what "
            "share actually finished top-N that season, averaged across seasons; "
            "matchup_z here is the team's red zone identity shift, not schedule"
        ),
        "biggest_shift_swings": _rows(biggest_swings, swing_cols, 15),
    }, indent=2, default=str)


@mcp.tool()
def draft_backtest(league_id: str | int, season: int, top_n: int = 3) -> str:
    """Replay a real past ESPN draft: the algorithm's pick, the true hindsight-best
    pick, and what you actually took, round by round.

    Give it a past season and your ESPN league id (auto-detects your team and
    draft slot from ESPN_SWID/ESPN_S2) and it rebuilds the board leak-free for
    that season -- only data from strictly before it, the same discipline
    matchup_backtest uses -- then replays the real draft in order. At each of
    your picks it reports three things: what who_should_i_pick's algorithm would
    have recommended given the real board at that exact moment, the true
    hindsight-optimal pick by value over replacement (QB capped at 1 -- a second
    quarterback can't start, so it isn't ranked against real RB/WR/TE need), and
    what you actually took. All three are scored on real points from that season.

    Each of the three picks also carries a value verdict (preseason ECR against
    actual finish -- the value_picks steal/bust framing, against real outcomes
    instead of projections) and team context (that player's team's O-line,
    pace, and schedule difficulty for the season being tested, leak-free --
    what team_context reports, but for a past season instead of always today).

    K/DST aren't modelled anywhere in this tool, so those rounds report your
    actual pick only, same as everywhere else. Only ESPN is supported.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    out = adp_mod.draft_backtest(league_id, season, top_n=top_n)
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def mock_draft(season: int, n_trials: int = 30, top_n: int = 5) -> str:
    """Monte Carlo mock draft: the live algorithm against many simulated
    opponents, averaged, using your active league's settings.

    No real draft needed -- the other teams are bots that pick by that season's
    real preseason ADP with realistic reach/fall noise (bigger swings plausible
    late, tight consensus at the top) rather than following it exactly, so
    who's actually on the board at your turn varies draw to draw. Your slot
    (from your active league's draft_slot) runs the exact same recommend()
    logic who_should_i_pick uses live. The board is leak-free -- only data from
    strictly before `season` feeds the projections, same discipline
    draft_backtest uses -- so passing the current season runs this against the
    real live board (this year's projections, history through last season)
    instead of a past, already-decided one.

    Scored on real points from `season` when they exist; for a season that
    hasn't been played yet, falls back to the model's own proj_points instead
    (check `scored_on` in the result) -- a forecast of the algorithm's typical
    outcome, not a validated backtest.

    One draw can make the algorithm look better or worse than its true average
    just from bot luck, which is why this runs n_trials and reports the mean,
    not a single result. For each round it also reports the most common picks
    and how often each showed up -- rounds with no real consensus (usually
    round 6+, once enough upstream bot randomness has compounded) should be
    read as "plausible outcomes," not "the pick."

    K/DST aren't modelled, so only skill-position rounds are simulated
    (your league's total rounds minus its K and DST starting slots).
    """
    league, weights = _settings()
    out = adp_mod.mock_draft(league, weights, season, n_trials=n_trials, top_n=top_n)
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def champion_strategies(league_id: str | int,
                        seasons: str = "2020,2021,2022,2023,2024,2025") -> str:
    """What actually won your ESPN league, season by season, and which specific
    pick made the difference.

    For each season, finds whichever team finished 1st and pulls their real
    draft. Every pick gets a value verdict -- preseason ECR against actual
    finish, the same steal/bust framing value_picks and draft_backtest use --
    so this answers "what draft-cost bet actually paid off for the winner,"
    not just "what did the champion draft." Reports each champion's opening two
    picks, first QB/TE round, RB/WR volume, and biggest steal, plus
    cross-season patterns: how often champions opened RB-RB, and the median
    round of their first QB.

    biggest_steal also explains *why* it was a steal: usage_trend is that
    player's real early- vs. late-season carries/targets/target share (a role
    expansion actually visible in the box scores), and team_environment is his
    team's O-line ranks, pace, and pass/rush split that season. Most value
    picks turn out to be a volume or role story, not raw talent beating a
    forecast -- this is what shows it concretely.

    ECR history only goes back to 2020 -- seasons before that get position and
    timing data but no value verdicts or steal context. ESPN only.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    out = adp_mod.champion_strategies(league_id, yrs)
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def persistent_value_players(seasons: str = "2021,2022,2023,2024",
                             min_seasons: int = 3, limit: int = 20) -> str:
    """Players who beat their draft cost repeatedly, not once.

    One outperformance is a season; three is a trait. This is the closest the data
    comes to naming players the market persistently misprices.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.value_history(yrs, league.scoring)
    if hist.empty:
        return json.dumps({"error": "no ECR history available"})
    rep = adp_mod.repeat_value_players(hist, min_seasons)
    cols = ["name", "position", "seasons", "hits", "busts", "hit_rate",
            "avg_value_ratio", "avg_ecr", "avg_games"]
    return json.dumps({
        "min_seasons": min_seasons,
        "best_value": _rows(rep, cols, limit),
        "worst_value": _rows(rep.tail(limit).iloc[::-1], cols, limit),
    }, indent=2, default=str)


@mcp.tool()
def rookie_report(limit: int = 20, position: str | None = None) -> str:
    """Projected rookies for this season, from draft capital and landing spot.

    Rookies have no NFL history, so they're projected off a curve fitted to how draft
    pick converted to first-year production across the last ten classes, then adjusted
    for the offence they landed in. Consistency is deliberately low for all of them:
    rookie roles move mid-season and the floor is a healthy scratch.

    Treat these as the widest error bars on the board.
    """
    b = _mark_drafted(_build_board(), _state())
    r = b[b.get("is_rookie", False) == True]  # noqa: E712
    if position:
        r = r[r["position"] == position.upper()]
    if r.empty:
        return json.dumps({"error": "no rookies on the board — draft class may not be published yet"})
    r = r.sort_values("draft_score", ascending=False)
    cols = ["name", "position", "team", "pick", "draft_round", "college", "adp",
            "overall_rank", "proj_points", "adj_ppg", "exp_games", "consistency",
            "drafted"]
    return json.dumps({
        "rookies": len(r),
        "note": "pick is NFL draft position; adp is fantasy market cost",
        "players": _rows(r, [c for c in cols if c in r.columns], limit),
    }, indent=2, default=str)


@mcp.tool()
def resolve_names(names_csv: str) -> str:
    """Check how names resolve against the board — useful before trusting a paste sync.

    Reports the match type for each name so silent mismatches surface. A name that
    fails to resolve looks like a player who scored zero, which is the single most
    damaging failure mode in this whole pipeline.
    """
    b = _build_board()
    queries = [q.strip() for q in names_csv.split(",") if q.strip()]
    out = []
    for q in queries:
        row, how = bd.match_player_verbose(q, b)
        out.append({
            "query": q,
            "resolved_to": (row["name"] if row is not None else None),
            "position": (row["position"] if row is not None else None),
            "team": (str(row["team"]) if row is not None else None),
            "match_type": how,
        })
    return json.dumps({
        "resolved": sum(1 for o in out if o["resolved_to"]),
        "of": len(out),
        "results": out,
    }, indent=2, default=str)


@mcp.tool()
def prewarm(verbose: bool = True, league_id: str | int | None = None) -> str:
    """Build every cache before draft day so nothing computes while you're on the clock.

    The first query of a session pays for downloading and modelling five seasons.
    Every query after it is served from memory. Run this an hour before your draft,
    not during it.

    In a league with an IDP slot, pass league_id to build the defender board too.
    It is skipped otherwise, because ranking defenders needs your league's own
    scoring and there is no safe default -- tackles alone range from 0.5 to 2
    points between leagues.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    import time as _time

    timings, t0 = {}, _time.time()
    steps = [
        ("play_by_play", lambda: sources.play_by_play()),
        ("weekly_stats", lambda: sources.weekly_stats()),
        ("snap_counts", lambda: sources.snap_counts()),
        ("injuries", lambda: sources.injuries()),
        ("rosters", lambda: sources.weekly_rosters()),
        ("schedules", lambda: sources.schedules()),
        ("board", lambda: _build_board()),
        ("oline", lambda: features.oline_ratings()),
        ("pace", lambda: features.team_pace_and_split()),
    ]
    # The defender board is a separate build and was not covered here, so the
    # first idp_report of a live draft paid full cost -- with 90 seconds on the
    # clock, exactly what prewarm exists to prevent.
    league_for_idp, _ = _settings()
    if league_id and int(league_for_idp.starters.get("IDP", 0) or 0):
        def _idp_board():
            from . import idp as idp_mod
            items = bd.espn_scoring_items(league_id, CURRENT_SEASON - 1)
            scoring = idp_mod.scoring_from_espn(items)
            if not scoring:
                return None
            seasons = list(range(CURRENT_SEASON - 5, CURRENT_SEASON))
            return idp_mod.build_board(
                sources.weekly_stats(seasons), scoring, seasons=seasons,
                teams=league_for_idp.teams,
                idp_slots=int(league_for_idp.starters.get("IDP", 1) or 1))
        steps.append(("idp_board", _idp_board))
    for name, fn in steps:
        s = _time.time()
        try:
            fn()
            timings[name] = round(_time.time() - s, 2)
        except Exception as exc:
            timings[name] = _step_failure(exc)

    b = _build_board()
    out = {
        "total_seconds": round(_time.time() - t0, 1),
        "players": len(b),
        "rookies": int(b.get("is_rookie", pd.Series(dtype=bool)).sum()),
        "ready": True,
        "note": "All subsequent tool calls are served from memory.",
    }
    if verbose:
        out["step_seconds"] = timings
        out["disk_cache"] = sources.cache_status()
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def player_report(player_name: str) -> str:
    """Full breakdown of one player: production, role, environment, injury, consistency."""
    b = _build_board()
    r = bd.match_player(player_name, b)
    if r is None:
        ptr = _idp_pointer(name=player_name)
        return json.dumps(ptr or {"error": f"no match for '{player_name}'"}, indent=2)
    fields = ["name", "position", "team", "age", "overall_rank", "pos_rank", "adp", "adp_delta",
              "proj_points", "adj_ppg", "baseline_ppg", "exp_games",
              "consistency", "startable_rate", "spike_rate", "floor", "ceiling", "fp_cv",
              "snap_share", "target_share", "touches",
              "injury_risk", "games_missed_rate", "report_rate", "heavy_seasons", "recent_burden",
              "run_block_rank", "pass_block_rank", "plays_per_game", "neutral_pass_rate",
              "rush_rate", "divisional_games",
              "sep_score", "avg_separation", "avg_cushion", "yprr", "tprr", "yac_oe",
              "is_rookie", "pick", "draft_round", "college",
              "rz_touches", "rz_td", "rz_td_rate", "rz_baseline_rate",
              "m_oline", "m_volume", "m_schedule", "m_divisional", "m_injury", "m_age",
              "m_separation", "m_td_luck", "vor"]
    out = _rows(pd.DataFrame([r]), [f for f in fields if f in r.index], 1)[0]
    out["summary"] = model.explain(r)
    return json.dumps(out, indent=2)


@mcp.tool()
def compare_players(names: str) -> str:
    """Compare 2-4 players head to head. Pass a comma-separated list."""
    b = _build_board()
    rows = []
    for n in [x.strip() for x in names.split(",") if x.strip()][:4]:
        r = bd.match_player(n, b)
        if r is not None:
            rows.append(r)
    if not rows:
        return json.dumps({"error": "no matches"})
    df = pd.DataFrame(rows)
    cols = ["name", "position", "team", "adp", "proj_points", "adj_ppg", "consistency",
            "startable_rate", "spike_rate", "injury_risk", "exp_games", "vor", "draft_score"]
    best = df.sort_values("draft_score", ascending=False).iloc[0]
    return json.dumps({
        "players": _rows(df.sort_values("draft_score", ascending=False), cols, 4),
        "verdict": f"{best['name']} — {model.explain(best)}",
    }, indent=2)


@mcp.tool()
def team_context(team: str) -> str:
    """Offensive environment for an NFL team: O-line, pace, run/pass split, schedule,
    drive efficiency, and red zone play-calling identity.

    `drive_efficiency` and `redzone_identity` are informational context, not folded into
    any player's projection or draft_score -- same convention as `matchup_z` in
    separation_report. `drive_efficiency.pct_td` is the share of that team's drives
    ending in a touchdown (a multiplier on how many scoring chances its players get,
    already reflected in their raw points, so treat this as a confidence check on a
    role rather than an extra adjustment). `redzone_identity.shift` is that team's
    neutral-field pass rate minus its red zone pass rate: a large positive shift means
    the offense gets meaningfully more run-heavy inside the 20 (receiving volume there,
    and the touchdown equity that comes with it, is less trustworthy for that team's
    pass catchers); near zero or negative means the passing game keeps its role even in
    the scoring area.
    """
    league, _ = _settings()
    # No pbp argument: that routes through the memoised builders instead of
    # recomputing a full pass over play-by-play on every call.
    ol = features.oline_ratings()
    pace = features.team_pace_and_split()
    dfn = features.defense_ratings(sc=league.scoring)
    sos = features.strength_of_schedule(CURRENT_SEASON, dfn)
    drive_eff = features.team_drive_efficiency()
    rz_shift = features.redzone_identity_shift()
    t = team.upper()
    recent = int(pace["season"].max())
    out = {
        "team": t,
        "oline": _rows(ol[(ol["team"] == t) & (ol["season"] == recent)],
                       ["season", "run_block_rank", "pass_block_rank", "adj_line_yards",
                        "stuff_rate", "sack_rate"], 1),
        "oline_history": _rows(ol[ol["team"] == t].sort_values("season"),
                               ["season", "run_block_rank", "pass_block_rank"], 6),
        "pace_and_split": _rows(pace[(pace["team"] == t) & (pace["season"] == recent)],
                                ["plays_per_game", "pass_rate", "rush_rate",
                                 "neutral_pass_rate", "off_epa"], 1),
        "schedule": _rows(sos[sos["team"] == t],
                          ["divisional_games"] + [c for c in sos.columns if c.endswith("_z")], 1),
        "drive_efficiency": _rows(
            drive_eff[(drive_eff.get("team") == t) & (drive_eff.get("season", pd.Series(dtype=int)) == recent)]
            if not drive_eff.empty else drive_eff,
            ["season", "drives", "pct_td", "pct_fg", "pct_punt"], 1),
        "redzone_identity": _rows(
            rz_shift[(rz_shift.get("team") == t) & (rz_shift.get("season", pd.Series(dtype=int)) == recent)]
            if not rz_shift.empty else rz_shift,
            ["season", "neutral_pass_rate", "rz_pass_rate", "shift"], 1),
    }
    return json.dumps(out, indent=2, default=str)


@mcp.tool()
def defense_report(position: str = "RB", limit: int = 32) -> str:
    """Defensive rankings against a position — fantasy points allowed, 5-year view.

    Rank 1 = toughest matchup. This is what drives the schedule adjustment.
    """
    league, _ = _settings()
    dfn = features.defense_ratings(sc=league.scoring)
    pos = position.upper()
    col = f"fpa_{pos}"
    if col not in dfn.columns:
        return json.dumps({"error": f"no data for position {pos}"})
    recent = int(dfn["season"].max())
    cur = dfn[dfn["season"] == recent][["team", col, f"{col}_rank", "def_epa_play", "def_rank"]]
    multi = dfn.groupby("team")[col].mean().rename(f"{col}_5yr_avg").reset_index()
    multi[f"{col}_5yr_rank"] = multi[f"{col}_5yr_avg"].rank(method="min").astype(int)
    out = cur.merge(multi, on="team").sort_values(f"{col}_5yr_rank")
    return json.dumps({
        "position": pos, "recent_season": recent,
        "note": "rank 1 = allows fewest fantasy points = toughest matchup",
        "defenses": _rows(out, list(out.columns), limit),
    }, indent=2)


@mcp.tool()
def plan_my_draft(strategy: str = "balanced", league_id: str | int | None = None) -> str:
    """Simulate your whole draft from your slot and return the projected lineup.

    Runs the board forward pick by pick, using ADP to model who realistically falls
    to you at each turn, and applies the same recommendation logic at every stop.
    strategy: balanced, zero_rb, hero_rb, or robust_rb.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    league, weights = _settings()
    state = _state()
    b = _mark_drafted(_build_board(), state).copy()
    b = b[~b["drafted"]]

    tilt = {
        "zero_rb": {"RB": 0.72, "WR": 1.12, "TE": 1.05, "QB": 0.95},
        "hero_rb": {"RB": 1.0, "WR": 1.06, "TE": 1.0, "QB": 0.95},
        "robust_rb": {"RB": 1.16, "WR": 0.94, "TE": 0.98, "QB": 0.92},
        "balanced": {},
    }.get(strategy, {})

    # Only plan the rounds the model can fill. The rounds a kicker, defence unit
    # or defensive player will take are real picks, but nothing here projects
    # them -- planning a skill player for those rounds hands back a roster with
    # more starters than the league can field.
    my_picks = [p for p in state.my_picks() if p >= state.on_the_clock]
    my_picks = my_picks[:league.modellable_rounds()]
    roster: dict[str, int] = dict(state.my_roster(b))
    taken: set[str] = set()
    plan = []

    for i, pick in enumerate(my_picks):
        nxt = my_picks[i + 1] if i + 1 < len(my_picks) else None
        pool = b[~b["_key"].isin(taken)].copy()
        # Model who's realistically gone by this pick.
        pool = pool[pool["adp"] > pick - 0.55 * pick ** 0.5 * 2]
        if pool.empty:
            break
        if strategy == "hero_rb" and i > 0 and roster.get("RB", 0) >= 1:
            pool = pool.copy()
            pool.loc[pool["position"] == "RB", "draft_score"] *= 0.7
        for pos, mult in tilt.items():
            pool.loc[pool["position"] == pos, "draft_score"] *= mult

        recs = model.recommend(pool, league, current_pick=pick, next_pick=nxt,
                               roster=roster, top_n=3)
        if recs.empty:
            break
        top = recs.iloc[0]
        taken.add(top["_key"])
        roster[top["position"]] = roster.get(top["position"], 0) + 1
        plan.append({
            "round": (pick - 1) // league.teams + 1, "pick": pick,
            "player": top["name"], "position": top["position"], "team": top.get("team"),
            "adp": round(float(top["adp"]), 1),
            "proj_points": round(float(top["proj_points"]), 1),
            "consistency": round(float(top["consistency"]), 3),
            "alternates": [r["name"] for _, r in recs.iloc[1:].iterrows()],
        })

    total = sum(p["proj_points"] for p in plan)
    return json.dumps({
        "strategy": strategy, "your_slot": state.my_slot,
        "projected_starters_points": round(total, 1),
        "final_roster": roster, "plan": plan,
        "idp_pick": _idp_plan(league, league_id),
        "caveat": "ADP-driven simulation of an average draft room. Your league will "
                  "deviate — use who_should_i_pick live rather than following this script.",
    }, indent=2)


@mcp.tool()
def idp_report(league_id: str | int | None = None, season: int | None = None,
               limit: int = 15, position: str | None = None,
               min_games: int = 8, timing_seasons: str | None = None) -> str:
    """Rank individual defensive players for a league with an IDP roster slot.

    Separate from the main board on purpose: defensive players are not projected
    by the offence model, and none of its environment multipliers mean anything
    for a defender. Ranking is by per-game rate carried over a 17-game season, so
    it answers who is best per game rather than who accumulated most last year --
    a player who missed time is not penalised for it.

    IDP scoring varies enormously between leagues (tackles alone range from 0.5
    to 2 points, and some leagues score assists double), so scoring is read from
    your own ESPN league rather than assumed. That makes league_id required: a
    guessed scoring system would produce a confident, wrong ranking.

    Read the ranking, not the point totals. Reproducing ESPN's own IDP figures
    from public data carries about 3.5% error, because ESPN and nflverse disagree
    on how many of a player's tackles were solo versus assisted. Order is
    reliable (0.97 rank correlation); two players within ~12 points are not
    meaningfully separated.

    vor is value over replacement -- the last defender who would actually start
    in your league. It is the only figure comparable against offensive players,
    since raw defensive totals are far larger and mean nothing across positions.

    timing_seasons ("2024,2025") adds when defenders actually left the board in
    your league's own past drafts, which is what "can I wait?" depends on. There
    is deliberately no per-player IDP draft position: published IDP consensus
    correlated 0.30 with actual pick across two real seasons, so which specific
    defender goes when is close to noise. How many are gone by a given pick is
    the part that holds.
    """
    try:
        league_id = _league_id(league_id)
    except LeagueIdError as exc:
        return json.dumps({"error": str(exc)}, indent=2)
    from . import idp as idp_mod

    if not league_id:
        return json.dumps({
            "error": "league_id is required.",
            "why": "IDP scoring differs too much between leagues to assume — "
                   "tackles alone range from 0.5 to 2 points, and some leagues "
                   "score assisted tackles double. Guessing would produce a "
                   "confident but wrong ranking, so scoring is read from your "
                   "league's own ESPN settings instead.",
        }, indent=2)

    league, _ = _settings()
    season = int(season or (CURRENT_SEASON - 1))

    try:
        items = bd.espn_scoring_items(league_id, season)
    except Exception as exc:
        return json.dumps({"error": f"couldn't read league scoring: {exc}"}, indent=2)

    scoring = idp_mod.scoring_from_espn(items)
    if not scoring:
        return json.dumps({
            "league_id": league_id, "season": season, "scores_idp": False,
            "note": "This league awards no points to individual defensive "
                    "players, so there is nothing to rank.",
        }, indent=2)

    # Project across the same lookback window the recommendation path uses, not
    # the single season this once read. Building one board from 2025 and another
    # from 2021-25 meant the two disagreed about who the best defender was --
    # idp_report said Blake Cashman at 89.9 value over replacement while
    # who_should_i_pick and plan_my_draft said Alex Singleton at 34.0. Same
    # question, different answer depending on which tool you happened to ask.
    # `season` still selects which season's scoring rules to read, which matters:
    # this league sextupled its IDP scoring between 2024 and 2025.
    seasons = list(range(CURRENT_SEASON - 5, CURRENT_SEASON))
    if season not in seasons:
        seasons = [season]
    weekly = sources.weekly_stats(seasons)
    idp_slots = int(league.starters.get("IDP", 0)) or 1
    board = idp_mod.build_board(weekly, scoring, seasons=seasons,
                                teams=league.teams, idp_slots=idp_slots,
                                min_games=min_games)
    if position:
        want = str(position).upper()
        if "position" in board.columns:
            board = board[board["position"].astype(str).str.upper() == want]

    timing = None
    if timing_seasons:
        try:
            want = [int(x) for x in str(timing_seasons).replace(" ", "").split(",") if x]
            hist = sources.weekly_stats(want)
            grp = (hist.dropna(subset=["player_display_name"])
                       .drop_duplicates("player_display_name")
                       .set_index("player_display_name")["position_group"].to_dict())
            by_season = {}
            for yr in want:
                by_season[yr] = sorted(
                    pk["overall"] for pk in bd.sync_espn(league_id, season=yr)
                    if grp.get(pk["name"]) in idp_mod.DEFENSIVE_GROUPS and pk.get("overall"))
            timing = idp_mod.draft_timing(by_season, teams=league.teams,
                                          my_picks=league.picks_for_slot())
        except Exception as exc:
            timing = {"error": f"couldn't read draft history: {exc}"}

    rows = board.head(max(1, int(limit))).to_dict("records")
    return json.dumps({
        "league_id": league_id, "season": season, "scores_idp": True,
        "projected_from_seasons": seasons,
        "scoring_used": scoring,
        "teams": league.teams, "idp_slots": idp_slots,
        "replacement_rank": league.teams * idp_slots,
        "qualified_players": int(len(board)),
        "min_games": min_games,
        "players": rows,
        "draft_timing": timing,
        "caveat": "Ranking is trustworthy; point totals are approximate (~3.5% "
                  "error, 0.97 rank correlation against ESPN's own figures). The "
                  "gap is ESPN and nflverse disagreeing on the solo/assisted "
                  "tackle split, an unofficial stat. Players within ~12 points "
                  "are not meaningfully separated. Only the linebacker slot id is "
                  "verified, so a DL/DB league is counted correctly but its slots "
                  "are not individually labelled.",
    }, indent=2, default=str)


@mcp.tool()
def model_settings(consistency_weight: float | None = None, injury_weight: float | None = None,
                   oline_weight: float | None = None, schedule_weight: float | None = None,
                   pace_weight: float | None = None, td_luck_weight: float | None = None,
                   qb_boost: float | None = None) -> str:
    """Tune how much each factor moves a player. Rebuilds the board.

    td_luck_weight controls how hard a player's red zone touchdown rate gets
    regressed toward what his position converts on average (player_report shows
    rz_touches/rz_td/rz_td_rate/rz_baseline_rate so you can see the raw numbers
    behind the adjustment). Set it to 0 to score players on raw history with no
    touchdown-luck correction at all.

    qb_boost is different from the others: they all adjust the projection from a
    real per-player signal (O-line, pace, etc.); qb_boost is a direct fractional
    lift on QB draft_score you supply because you believe the position is worth
    more than the projection says, not because of any single player's own inputs.
    Comes from champion_strategies/draft_backtest analysis: check whether QB has
    actually beaten its draft cost across your league's real history (not just
    hit rate in general -- that alone doesn't justify this) before setting it
    above 0. It stacks with, and doesn't replace, the roster-need discount that
    already stops the model from wanting a second QB once you have one.
    """
    league, weights = _settings()
    # The board's cache key includes the weights, so snapshot the old key before
    # mutating them: it's the stale board that needs evicting, not the new one.
    old_key = board_cache_key(league, weights)
    for name, val in [("consistency_weight", consistency_weight), ("injury", injury_weight),
                      ("oline", oline_weight), ("schedule", schedule_weight),
                      ("pace_volume", pace_weight), ("td_luck", td_luck_weight),
                      ("qb_boost", qb_boost)]:
        if val is not None:
            setattr(weights, name, float(val))
    save_settings(league, weights)
    _CACHE.update({"weights": weights})
    _BOARDS.pop(old_key, None)
    p = DATA_DIR / f"board_{old_key}.parquet"
    if p.exists():
        p.unlink()
    return json.dumps({"league": league.name, "weights": weights.__dict__,
                       "board": "will rebuild on next query"}, indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
