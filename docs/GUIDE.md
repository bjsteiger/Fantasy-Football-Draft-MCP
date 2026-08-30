# Fantasy Draft MCP — Full Setup & Tool Guide

One document covering everything: installing the server, connecting it to Claude,
configuring a league, and every tool it exposes with arguments and defaults. For a
faster path to your first recommendation, see [quickstart.md](quickstart.md); this
is the reference version.

---

## 1. Prerequisites

- Python 3.10+
- ~250 MB free disk (cached play-by-play, weekly stats, snap counts, injuries,
  rosters, schedules, plus one small parquet board per league format)
- A Claude Desktop (or other MCP-compatible client) install, to actually talk to it

---

## 2. Install

```bash
git clone https://github.com/bjsteiger/Fantasy-Football-Draft-MCP.git
cd Fantasy-Football-Draft-MCP
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

**macOS + Homebrew Python**: a bare `pip install` outside a venv fails with
`error: externally-managed-environment` — the venv above is required, not optional.

**iCloud-synced folders (Desktop/Documents on macOS)**: if this repo sits under a
folder iCloud syncs, put the venv somewhere that isn't synced instead —
`python3 -m venv ~/.venvs/ffdraft-mcp` — and point every command below
(`pip install -e .`, `pytest`, and the Claude Desktop config) at that venv's
`bin/python`/`bin/pip`. iCloud's file-provider daemon tags newly written files
hidden while syncing, including the `.pth` file the editable install creates, and
Python 3.13 silently skips hidden `.pth` files at import time — the symptom is
`ModuleNotFoundError: No module named 'ffdraft'` from Claude's logs even though
`pip install -e .` succeeded and `python -m ffdraft.server` runs fine by hand from
an activated shell. Full detail in [troubleshooting.md](troubleshooting.md).

If you'd rather keep the venv in-repo, the alternative fix is adding
`"PYTHONPATH": "/absolute/path/to/repo/src"` to the server's `env` block in step 4 —
that bypasses the `.pth` mechanism entirely, though it only patches the MCP server
launch, not `setup_data.py` or other commands run directly from your shell.

---

## 3. Build the data cache

```bash
python setup_data.py
```

One-time, ~3-6 minutes: downloads five seasons of play-by-play, weekly stats, snap
counts, injury reports, rosters, schedules, and historical draft picks from
[nflverse](https://github.com/nflverse) (~200 MB), then builds the initial player
board. Everything after this is served from local parquet cache — no network calls
during a live draft unless you explicitly `refresh_data`.

Expected output:

```
  weekly_stats        28,026 rows   (2s)
  snap_counts        131,003 rows   (3s)
  play_by_play       247,284 rows   (94s)

Building player board...
  631 players: {'WR': 232, 'RB': 168, 'QB': 118, 'TE': 113}
```

---

## 4. Connect it to Claude Desktop

Add a server entry to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fantasy-draft": {
      "command": "/absolute/path/to/Fantasy-Football-Draft-MCP/.venv/bin/python",
      "args": ["-m", "ffdraft.server"],
      "env": {
        "FFDRAFT_SEASON": "2026",
        "PYTHONPATH": "/absolute/path/to/Fantasy-Football-Draft-MCP/src",
        "ESPN_SWID": "optional-for-private-espn-leagues",
        "ESPN_S2": "optional-for-private-espn-leagues"
      }
    }
  }
}
```

Rules that matter:

- **Absolute paths only**, for both `command` and `PYTHONPATH`. Claude Desktop
  launches the server from an undefined working directory, so `python` or a
  relative path won't resolve.
- If you used a separate venv (`~/.venvs/ffdraft-mcp`), point `command` at
  *that* venv's `bin/python`, not `.venv/bin/python` in the repo.
- `PYTHONPATH` is belt-and-suspenders against the iCloud `.pth` issue above —
  harmless to include even if you're not on macOS.
- `ESPN_SWID`/`ESPN_S2` are only needed for **private** ESPN leagues. Never commit
  them; see [SECURITY.md](../SECURITY.md).

**Restart Claude Desktop fully** — quit and reopen, not just close the window — so
it re-reads the config. You should then see the `fantasy-draft` tools available.

Verify the server runs standalone before troubleshooting Claude integration:

```bash
.venv/bin/python -m ffdraft.server
```

It should start and idle on stdio (no output, no crash) — Ctrl-C to exit.

---

## 5. Set up a league

Everything after this is done conversationally in Claude — you don't call tools by
name, you ask in plain language and the model picks the right one. This guide shows
the underlying tool signatures so you know what's actually happening and what every
argument means.

```
Set up my home league: 10 teams, full PPR, I pick 4th.
```

maps to:

```python
configure_league(
    name="home", teams=10, draft_slot=4, rounds=16,
    scoring="full_ppr", snake=True,
    qb=1, rb=2, wr=2, te=1, flex=1, idp=0, superflex=0,
    te_premium_bonus=0.0, consistency_weight=0.35,
)
```

| Argument | Default | Notes |
|---|---|---|
| `name` | `"default"` | Any label. Reusing a name updates that league in place. |
| `teams` | 12 | Any size. |
| `draft_slot` | 6 | Your 1-indexed pick in round 1. Validated against `teams`. |
| `rounds` | 16 | |
| `scoring` | `"half_ppr"` | `ppr` / `full_ppr`, `half_ppr`, `standard` / `non_ppr`. |
| `snake` | `true` | `false` for a linear (non-snake) draft. |
| `qb`, `rb`, `wr`, `te`, `flex` | 1 / 2 / 2 / 1 / 1 | Starting roster slots. K and DST are fixed at 1 each and not modelled. |
| `idp` | 0 | Individual defensive player slots — LB, DL, DB, edge, or a generic defensive flex. Set it if your league starts defenders; rank them with `idp_report`. |
| `superflex` | 0 | Extra slots where a QB may also start. Shifts replacement level and roster-need logic — quarterbacks become genuinely scarce. |
| `te_premium_bonus` | 0.0 | Extra fantasy points per TE reception, on top of `scoring`. |
| `consistency_weight` | 0.35 for a new league, unchanged for an existing one | 0 = pure expected points (upside), 1 = pure week-to-week reliability (floor). Omitting it leaves the league's current value alone; the other model weights (`schedule`, `injury`, `oline`, `td_luck`, `qb_boost`…) are always preserved, so reconfiguring a league to change `idp` or `rounds` does not undo tuning done with `model_settings`. |
| `adp_csv_path` | — | Path to your platform's ADP export, if you want it instead of consensus ECR. |

You can hold as many leagues as you like side by side:

```
Set up my work league: 13 teams, half PPR, I pick 11th.
List my leagues.
Switch to home.
```

Two leagues with identical `teams`/`scoring`/`starters`/`superflex`/`te_premium_bonus`
share one cached board (`cache_key()` hashes exactly those fields) — switching between
them is instant. Draft state (picks recorded, your roster) is always kept separate per
league name, regardless of whether the board is shared.

### If your league starts defenders

Say so, and the round arithmetic stays honest:

```
Set up my ESPN league: 10 teams, full PPR, I pick 4th, and 1 LB slot.
```

adds `idp=1`. Defensive players are deliberately *not* projected by the offence
model — none of its environment multipliers (O-line, pace, separation, red zone
role) mean anything for a linebacker, and widening the position list to include
one would fabricate inputs like "points a defence allows to opposing
linebackers" and corrupt the real QB/RB/WR/TE projections. So the count is used
only for arithmetic: `idp`, like `K` and `DST`, is subtracted from your
modellable rounds, and those rounds are skipped in simulations rather than
filled with a recommendation the model can't make.

Ranking the defenders themselves is a separate call, `idp_report` (§8), which
needs your ESPN `league_id`:

```
Rank the linebackers for my league.
```

Two things worth knowing about IDP support, up front:

- **It's ESPN-only, and `league_id` is required.** IDP scoring varies too much
  between leagues to guess — tackles alone range from 0.5 to 2 points, and some
  leagues score assists double — so scoring is read from your own league
  settings rather than assumed.
- **There is no ADP for defenders**, so nothing can tell you whether a linebacker
  will last to your next pick. Every other recommendation in this tool weighs
  value against survival odds; for defenders only the value half exists.

---

## 6. Before draft day: prewarm

```
Run prewarm.
```

Builds every cache the model touches — play-by-play, weekly stats, snap counts,
injuries, rosters, schedules, the board itself, O-line ratings, pace/split — up
front. Run this **an hour before** your draft, not during it: the first cold query
of a session can pay for a multi-minute network download if a cache entry expired
(see `max_age_days` behavior under Tool reference → `refresh_data`); every query
after `prewarm` is served from memory.

---

## 7. During the draft

The one-call workflow:

```
I'm on the clock. Sync my Sleeper draft, id 1234567890.
```

runs `on_the_clock`, which chains: `sync_draft` → `draft_status` →
`who_should_i_pick` → `value_picks` (scoped to your current round and next) →
`separation_report` (only appended if the top rec is a WR/TE). One call, full
picture.

Re-sync between picks as the draft moves:

```
Sync again. Who's the best available now?
```

If you're not on Sleeper/ESPN, record manually as picks happen:

```
Record: Bijan, then CMC, then JSN.
```

Shorthand resolves — bare surnames, nicknames, and initialisms all match. Check a
name before trusting a paste sync:

```
Resolve these names: JSN, CMC, Bijan
```

A typical `who_should_i_pick` answer:

> **Take Amon-Ra St. Brown.** WR4 by projection (245 pts, 16.1/gm); consistency
> 0.63, startable in 58% of weeks; volume/pace +2.3%; schedule +2.2%; injury risk
> 16% (~15 games); 8% chance he lasts to your next pick.
>
> Josh Allen grades higher overall, but he has a 73% chance of surviving to pick 19
> and St. Brown does not.

---

## 8. Full tool reference

### Setup & league management

**`configure_league(name, teams, draft_slot, rounds, scoring, snake, qb, rb, wr, te, flex, idp, superflex, te_premium_bonus, consistency_weight, adp_csv_path)`**
Create or update a named league; makes it active. See the table in §5. Updating an
existing league keeps its model weights — including `consistency_weight` when you
don't pass one — so changing the league's shape never resets tuning.

**`list_leagues()`**
Every league you've set up, which is active, teams/scoring/slot/superflex, and how
many picks are recorded in each.

**`switch_league(name)`**
Make a different league active. Board and in-progress draft resume where you left
them.

**`remove_league(name)`**
Delete a league and its draft history. The board cache is left alone — other
leagues sharing the same format may still need it.

**`prewarm(verbose=True, league_id=None)`**
Build every cache before draft day (§6). `verbose=false` suppresses the
per-step timing breakdown. In an IDP league pass `league_id` to build the
defender board too — it's skipped without one, since ranking defenders needs
your league's own scoring and there is no safe default.

**`refresh_data(force_download=False)`**
Rebuild the board from source data. `force_download=true` deletes every cached
parquet and re-downloads from nflverse — use when new data is published mid-season
(a new week of stats, an updated depth chart) and you want it reflected immediately
rather than waiting on the cache's natural expiry.

**`model_settings(consistency_weight, injury_weight, oline_weight, schedule_weight, pace_weight, td_luck_weight, qb_boost)`**
Retune how much each factor moves a player off his baseline projection, then
rebuild the board. All are `None` by default (leaves current value unchanged).

| Argument | Meaning |
|---|---|
| `consistency_weight` | Floor vs. ceiling trade in the final ranking. |
| `injury_weight` | How hard injury history + workload burden discount a projection. |
| `oline_weight` | O-line quality's effect (run block for RB, pass block for QB/pass-catchers). |
| `schedule_weight` | 5-year opponent defensive strength, recency-weighted. |
| `pace_weight` | Team plays/game and run-pass split vs. the player's role. |
| `td_luck_weight` | How hard a red-zone TD rate regresses toward the position baseline. 0 = raw history, no correction. |
| `qb_boost` | Direct fractional lift on QB `draft_score` (e.g. `0.12` = +12%) — a belief you supply, not a derived signal. Verify with `champion_strategies`/`draft_backtest` on your actual league before setting above 0. Stacks with, doesn't replace, the roster-need discount that already prevents the model wanting a second QB in a 1-QB league. |

### During the draft

**`on_the_clock(platform, league_id, draft_id, pasted_board, season, limit)`**
The full workflow in one call — see §7. Takes exactly `sync_draft`'s arguments plus
`limit` (default 6) for how many recommendations to return. `league_id` is passed
through to `who_should_i_pick`, so on ESPN an open IDP slot surfaces its `idp_option`
here too — this is the call you make under a pick clock, and a defender option that
only appeared in a separate tool wouldn't be seen in time to matter.

**`who_should_i_pick(limit=6, league_id=None)`**
The core recommendation call. Weighs projected value, week-to-week consistency,
your roster's open starting slots, and each player's odds of surviving to your next
pick. Returns the pick being evaluated, your current roster, ranked recommendations
with plain-language reasoning, and a one-line headline.

With an IDP slot still open, pass `league_id` and the best available defender comes
back alongside as `idp_option` — so you don't have to run a second tool with the
clock running. It sits *beside* the ranked list rather than inside it, because the
ranking trades value against survival odds and defenders have no draft market to
estimate survival from. Its `vor` is directly comparable with the offensive rows: a
weekly score sums your starters wherever they line up.

**`best_available(position=None, limit=15, sort_by="draft_score")`**
Next best players still on the board. `sort_by`: `draft_score` (balanced), `vor`
(raw value over replacement), `consistency` (floor), `proj_points`, or `value`
(biggest gap between ADP and model rank). Filter with `position` (`QB`/`RB`/`WR`/`TE`).

**`sync_draft(platform, league_id=None, draft_id=None, pasted_board=None, season=CURRENT_SEASON)`**
Pull the live board.
- `platform="sleeper"` + `draft_id` — fully automatic, public API, no credentials.
- `platform="espn"` + `league_id` — public leagues work as-is; private need
  `ESPN_SWID`/`ESPN_S2` set before the server starts.
- `platform="paste"` + `pasted_board` — works for any platform; handles numbered
  lists, "Round 3, Pick 7 — Name", comma-separated runs, trailing team/position tags.

**`record_pick(player_name, overall_pick=None, team_slot=None)`**
Log one pick manually. Accepts shorthand names. Use this every pick if you aren't
auto-syncing from a platform.

**`undo_pick()`**
Remove the most recently recorded pick.

**`reset_draft()`**
Clear all recorded picks for the active league and start over.

**`draft_status()`**
Round, on-the-clock pick, your recorded roster, and per-pick detail (player,
position, projected points) for what you've drafted so far.

**`plan_my_draft(strategy="balanced", league_id=None)`**
Simulates your entire remaining draft from your slot, pick by pick, modelling who
realistically falls to you at each turn from ADP. `strategy`: `balanced`,
`zero_rb`, `hero_rb`, `robust_rb`. Returns a full projected final roster and
starter-points total. Only modellable rounds are planned — your total rounds minus
the `K`, `DST` and `idp` starting slots — and passing `league_id` in an IDP league
adds an `idp_pick` for the defender to target. Treat as preparation, not a script —
`who_should_i_pick` live will deviate from this the moment your real draft room does.

### Player & team research

**`player_report(player_name)`**
Every modelled factor for one player: production, role, injury components,
separation/route metrics, red zone role vs. position baseline, and every
environment multiplier (`m_oline`, `m_volume`, `m_schedule`, `m_divisional`,
`m_injury`, `m_age`, `m_separation`, `m_td_luck`), plus a plain-language summary.

**`compare_players(names)`**
Head to head, 2-4 players, comma-separated string. Returns each player's full
comparison row plus a one-line verdict for who grades highest.

**`rookie_report(limit=20, position=None)`**
This year's incoming class, projected from NFL draft capital and landing spot
(pace/O-line of the team they landed on) rather than history, since they have
none. Consistency is deliberately low — rookie roles move mid-season.

**`separation_report(position="WR", player_name=None, limit=20)`**
NFL Next Gen Stats separation/cushion, plus route-derived YPRR/TPRR. Pass
`player_name` for one player's season-by-season history, or leave it blank with
`position` for a leaderboard, ranked by `sep_score` (talent). Only players
clearing 250 estimated routes and 50 targets. Also returns `matchup_z` (that
player's team's upcoming-schedule difficulty at the position) for reference only —
`matchup_backtest` found blending it into the ranking made WR predictions *worse*.

**`team_context(team)`**
One NFL team's offensive environment: O-line run/pass block ranks (current +
history), pace and run/pass split, schedule difficulty, divisional game count, drive
efficiency (`pct_td`/`pct_fg`/`pct_punt` — share of drives ending in each outcome),
and red zone identity (`shift` — neutral-field pass rate minus red zone pass rate; a
large positive shift means the offense goes run-heavy near the goal line). The last
two are informational only, like `matchup_z` in `separation_report` — not folded into
any player's projection, since blending an unvalidated new signal into `draft_score`
is exactly what `matchup_backtest` exists to catch. Needs the `fixed_drive_result`/
`drive` play-by-play columns; run `refresh_data(force_download=true)` if either comes
back empty for a season that should have data.

**`defense_report(position="RB", limit=32)`**
Fantasy points allowed *to* a position by NFL defences, current season and 5-year
average, both ranked. Rank 1 = toughest matchup — this is what drives the model's
schedule adjustment. It's a matchup-strength tool, not a draft board: to draft
defensive players, use `idp_report`.

**`idp_report(league_id, season=None, limit=15, position=None, min_games=8, timing_seasons=None)`**
Ranks individual defensive players, for leagues with an IDP roster slot (§5). A
separate board on purpose — defenders aren't projected by the offence model, and
none of its multipliers apply to them.

`league_id` (ESPN) is required, not a convenience: IDP scoring differs enormously
between leagues, and a guessed scoring system would produce a confident, wrong
ranking. Ranking is by per-game rate carried over a 17-game season, so it answers
who is best *per game* — a player who missed time isn't penalised for it. `min_games`
gates that rate; without it a defender with one big game projects a rate no starter
sustains and lands first. `vor` is the only figure comparable against offensive
players, since raw defensive totals are far larger and mean nothing across positions.

**Read the order, not the totals.** Reproducing ESPN's own IDP figures from public
data carries ~3.5% mean error — ESPN and nflverse disagree on how many of a player's
tackles were solo versus assisted, an unofficial, human-scored stat. Rank correlation
is 0.97, so the ordering holds; two players within ~12 points are not meaningfully
separated. Derivation in [idp-scoring-derivation.md](idp-scoring-derivation.md).

`timing_seasons` (e.g. `"2024,2025"`) adds when defenders actually left the board in
your league's own past drafts — which is what "can I wait?" really depends on. There
is deliberately no per-player IDP draft position: published IDP consensus correlated
0.30 with actual pick across two real seasons, so which specific defender goes when is
close to noise. How many are gone by a given pick is the part that holds.

**`value_picks(limit=20, direction="undervalued")`**
Where the model disagrees with the draft market, restricted to players the market
actually ranks (a synthetic fallback ADP doesn't count). `direction`:
`undervalued` (model ranks him better than his draft cost) or `overvalued`.

**`persistent_value_players(seasons="2021,2022,2023,2024", min_seasons=3, limit=20)`**
Players who beat (or missed) their draft cost repeatedly across multiple seasons,
not just once — the closest this tool gets to naming names the market
persistently misprices.

**`resolve_names(names_csv)`**
Check how a comma-separated list of names resolves against the board before
trusting a paste sync. Surfaces silent mismatches, which otherwise look exactly
like a player who scored zero.

### Backtesting & validation

**`draft_value_history(seasons="2021,2022,2023,2024", group_by="draft_round")`**
Backtest: preseason consensus rank vs. actual finish, converted to your league's
scoring. `group_by`: `draft_round` or `position`.

**`matchup_backtest(seasons="2021,2022,2023,2024", position="WR", top_n=24)`**
Does blending schedule difficulty into a receiver's talent score predict actual
finish better than talent alone? Leak-free — talent comes from the *prior*
season's separation score only. Reports Spearman correlation and top-N precision
for both metrics side by side. 2021-2024 result for WR: talent alone wins, which
is why `separation_report` ranks by `sep_score` alone.

**`redzone_shift_backtest(seasons="2022,2023,2024,2025", position="WR", top_n=24)`**
Does blending a team's red zone identity shift (from `team_context`) into the existing
touchdown-luck signal predict next season's points better than touchdown-luck alone?
Same leak-free discipline as `matchup_backtest`, scored by the same summary logic. A
2022-2025 run found the shift makes predictions *worse* for both WR (`improvement_corr`
-0.006, 300 player-seasons) and TE (-0.053, 117) — which is why `redzone_identity_shift`
stays informational-only in `team_context` rather than feeding `m_td_luck`/`draft_score`.
WR/TE only.

**`draft_backtest(league_id, season, top_n=3)`**
Replays one of your real past ESPN drafts round by round: what
`who_should_i_pick`'s algorithm would have recommended given the real board at
that exact moment, the true hindsight-optimal pick, and what you actually took —
all scored on that season's real points. Auto-detects your team/slot from
`ESPN_SWID`/`ESPN_S2`. Leak-free: only data strictly before that season feeds the
board. ESPN only.

**`mock_draft(season, n_trials=30, top_n=5)`**
Monte Carlo mock draft: the live `recommend()` algorithm against many simulated
ADP-driven bot opponents (with realistic reach/fall noise), averaged over
`n_trials` so one lucky/unlucky draw doesn't misrepresent the algorithm's true
average. Uses your *active* league's settings — run `configure_league` first.
Passing the current season runs it against today's real live board.

**`champion_strategies(league_id, seasons="2020,2021,2022,2023,2024,2025")`**
What actually won your ESPN league each season and which specific pick made the
difference — opening two picks, first QB/TE round, RB/WR volume, biggest steal
(with the usage-trend and team-environment context behind it), plus cross-season
patterns like how often champions opened RB-RB. ECR value verdicts only go back to
2020. ESPN only.

---

## 9. Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `FFDRAFT_SEASON` | The season being drafted | `2026` |
| `FFDRAFT_SEASONS` | Override the 5-year lookback, e.g. `2021,2022,2023,2024,2025` | `range(FFDRAFT_SEASON-5, FFDRAFT_SEASON)` |
| `FFDRAFT_CACHE` | Where downloaded source parquet lives | `~/.ffdraft/cache` |
| `FFDRAFT_DATA` | Where built player boards live | `~/.ffdraft/data` |
| `FFDRAFT_STATE` | Where league configs and draft state live | `~/.ffdraft/state` |
| `ESPN_SWID` / `ESPN_S2` | Cookies for private ESPN leagues | unset |

---

## 10. If something looks wrong

Full list in [troubleshooting.md](troubleshooting.md); the fast checks:

- **Wrong pick recommended** → `draft_status`, confirm `my_slot`; fix with
  `configure_league` (league config is authoritative).
- **A drafted player still shows available** → `resolve_names` on that name.
- **Sleeper sync returns nothing** → confirm you used the *draft* id from
  `sleeper.com/draft/nfl/<draft_id>`, not the league id.
- **First query of a session is slow** → expected on a cold cache; run `prewarm`
  ahead of time.
- **Results feel off** → the model is opinionated by design; retune with
  `model_settings` rather than assuming a bug (see [methodology.md](methodology.md)).
