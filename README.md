# Fantasy Football Draft Analyst (MCP Server)

A live draft assistant. Connect it to Claude, sync your ESPN or Sleeper draft board, and
ask "who should I pick?" at every turn. It answers with a recommendation, the reasoning,
and the odds each player survives to your next pick.

Built and smoke-tested end to end against live data: 481 players modelled from ~295,000
plays across five seasons, with real 2026 preseason consensus rankings.

---

## Quick start

```bash
pip install -e .

# One-time data build (~3-6 min; downloads 5 seasons of play-by-play, then caches)
python -c "from ffdraft import server as s; print(s.refresh_data())"
```

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fantasy-draft": {
      "command": "python",
      "args": ["-m", "ffdraft.server"],
      "env": {
        "FFDRAFT_SEASON": "2026",
        "ESPN_SWID": "optional-for-private-espn-leagues",
        "ESPN_S2": "optional-for-private-espn-leagues"
      }
    }
  }
}
```

Then in Claude:

```
Set up my home league: 10 teams, full PPR, I pick 4th.
Set up my work league: 13 teams, half PPR, I pick 11th.
Switch to home. Sync my Sleeper draft 1234567890. Who should I pick?
```

---

## Tools

| Tool | What it does |
|---|---|
| `configure_league` | Create or update a named league; makes it active |
| `list_leagues` | All your leagues and which is active |
| `switch_league` | Change active league; board and draft resume instantly |
| `remove_league` | Delete a league and its draft history |
| `refresh_data` | Rebuild the board from source. Run once before draft day |
| `sync_draft` | Pull the live board from Sleeper, ESPN, or pasted text |
| `who_should_i_pick` | **The main one.** Recommendation + reasoning for the pick on the clock |
| `best_available` | Next best players, sortable by value, consistency, or ADP bargain |
| `record_pick` / `undo_pick` / `reset_draft` | Manual board management |
| `draft_status` | Your roster and where the draft stands |
| `prewarm` | Build all caches before draft day so nothing computes on the clock |
| `rookie_report` | This year's rookie class, projected from draft capital |
| `resolve_names` | Check how names resolve before trusting a paste sync |
| `separation_report` | Separation, cushion, YPRR, TPRR — the open-data PFF analogue |
| `value_picks` | Where the model disagrees with the draft market |
| `draft_value_history` | Backtest: preseason rank vs actual finish, by round and position |
| `persistent_value_players` | Players who beat their draft cost year after year |
| `player_report` | Every modelled factor for one player |
| `compare_players` | Head to head, up to four |
| `team_context` | O-line ranks, pace, run/pass split, schedule for an NFL team |
| `defense_report` | Fantasy points allowed by position, 5-year view |
| `plan_my_draft` | Simulate all 16 of your picks from your slot |
| `model_settings` | Retune factor weights |

---

## Connecting your draft

**Sleeper** — fully automatic, no credentials. Get the draft id from the draft URL
(`sleeper.com/draft/nfl/<draft_id>`) and call `sync_draft("sleeper", draft_id=...)`.
Re-sync between picks.

**ESPN** — `sync_draft("espn", league_id=...)`. Public leagues work as-is. Private ones
need the `SWID` and `espn_s2` cookies from a logged-in browser session, set as the
`ESPN_SWID` and `ESPN_S2` environment variables. ESPN publishes no real-time draft push
API, so this polls the league's draft-detail endpoint — re-sync every few picks.

**Anything else** (Yahoo, NFL.com, in-person) — copy the drafted list and call
`sync_draft("paste", pasted_board="...")`. The parser handles numbered lists,
"Round 3, Pick 7 — Name", comma-separated runs, and trailing team/position tags.

---

## How the recommendation works

**1. Baseline.** Recency-weighted fantasy points per game across five seasons
(40% last year, decaying back to 5%), converted to your exact league scoring.
Small samples regress toward the mean of *starter-caliber* players at the position —
not the mean of everyone who logged a snap, which would drag a genuine RB1 down by a third.

**2. Environment multipliers.** Each bounded so no single factor can dominate:

- **O-line** — run block from Adjusted Line Yards (rushing yards credited to the line on
  a sliding scale, since the line owns the first four yards and the back owns the
  breakaway), pass block from sacks and QB hits allowed per dropback. Backs get the run
  block rating; passing-game players get pass block.
- **Pace and run/pass split** — plays per game plus *neutral-script* pass rate, measured
  only when win probability is between 20% and 80%. Raw pass rate mostly tells you
  whether a team was losing.
- **Schedule** — recency-weighted fantasy points allowed by every opponent on next
  season's slate, computed per position. Divisional games are counted separately: you
  face those six defenses twice and can't escape them.
- **Injury** — availability history, injury-report frequency even in games played, and
  workload burden. Heavy touch volume past the positional age cliff compounds.
- **Age** — positional aging curves (RB decline from 27, WR from 30).

**3. Consistency.** The thing you asked to optimize for: how often a player delivers a
usable week. Startable rate (45%), floor as a share of average (25%), inverted variance
(15%), and availability (15%). Regressed for small samples — a backup who scored twice
in three appearances shows perfect reliability he has never demonstrated.

**4. Value over replacement,** blended with consistency at your chosen weight (default 35%).

**5. Positional opportunity cost** — the step that actually wins drafts. Raw value says
take the best player. That's wrong in a snake draft. What matters is the *marginal* gain
over what the position still offers at your next turn. The model walks each position from
the top down, accumulating the chance every better player is gone, and computes the
expected value of waiting.

This is why, tested at pick 6 with the top five off the board, it recommends Amon-Ra
St. Brown over Josh Allen despite Allen grading as the single highest-value player —
Allen has a 73% chance of lasting to pick 19, and the receiver does not.

**6. Roster need.** A bench player is worth the odds he ever starts for you. That decays
fast at one-slot positions, which is why the model won't draft you a second quarterback
early and won't draft a third at all.

---

## Where the data comes from

- **[nflverse](https://github.com/nflverse)** — play-by-play, weekly stats, snap counts,
  official injury reports, rosters, schedules.
- **NFL Next Gen Stats** (via nflverse) — separation, cushion, YAC over expected, rush
  yards over expected, stacked-box rate. Player tracking data, free.
- **[dynastyprocess](https://github.com/dynastyprocess/data)** — FantasyPros expert
  consensus rank history back to 2019, which powers both live ADP and the value backtest.

Note: nflverse renamed its weekly stats release from `player_stats` to `stats_player_week`
starting in 2025 and dropped several columns. Both layouts are handled and normalised, so
the model works across the 2020-2026 span without silently losing a season.

The O-line, defense, pace and run/pass numbers are **computed from raw plays** rather than
scraped from someone's published ranking table. Those tables are paywalled, inconsistently
defined, and break whenever a site changes its HTML. Computing them keeps the model
reproducible and lets it recompute under your league's scoring.

---

## Separation data

The PFF table you shared measures route-winning. Most of it is recoverable from open
sources, and `separation_report` returns it:

- **`avg_separation`** — NFL Next Gen Stats, the tracking-measured yards between receiver
  and nearest defender when the ball arrives. Same underlying quantity as PFF's SEP,
  from chips rather than human charting.
- **`avg_cushion`** — pre-snap defender depth, which reads coverage respect.
- **`yprr` / `tprr`** — yards and targets per route run. No free source publishes route
  counts, so routes are estimated as snap share times team dropbacks, damped for backs
  and in-line tight ends who stay in to block.
- **`sep_score`** — a within-season z-score blending all of the above.

Validated against your screenshot on 2025 Indianapolis:

| | computed TPRR | PFF (man/zone) | computed YPRR | PFF (man/zone) |
|---|---|---|---|---|
| Pittman | 0.209 | 0.21 / 0.24 | 1.47 | 1.58 / 1.89 |
| Downs | 0.254 | 0.32 / 0.27 | 1.63 | 1.37 / 1.59 |
| Pierce | 0.178 | 0.24 / 0.19 | 2.13 | 2.33 / 2.41 |
| Warren | 0.241 | 0.13 / 0.24 | 1.76 | 0.67 / 2.28 |

TPRR lands within a few hundredths and the ordering is right — Pierce the highest-YPRR
receiver, Downs the highest-TPRR of the wideouts, exactly as your table shows.

Qualification is strict on purpose: 250 estimated routes and 50 targets in a season.
These are rate stats, and a part-time receiver posts a flattering YPRR that says nothing
about how he'd hold up in a real workload. The one thing genuinely not reproducible is
the **man-versus-zone split**, which needs per-play coverage classification that only
manual charting provides.

Separation feeds the projection as a bounded multiplier for WR and TE, so a receiver who
won his routes is separated from one who rode target volume he may not keep.

---

## Value picks: draft cost vs. actual finish

`draft_value_history` backtests FantasyPros preseason consensus rank against where players
really finished, 913 draftable player-seasons over 2021-2025. Value is measured in points
against what that draft slot actually returned — "did RB5 capital buy RB5 production?"
Rank movement would be unfair to early picks, since undrafted breakouts push every drafted
player down the final standings.

| Round | Hit rate | Bust rate | Median return |
|---|---|---|---|
| 1-3 | 11-12% | 16-36% | 0.81-0.89x |
| 4-6 | 12-19% | 22-38% | 0.80-0.91x |
| 7-9 | 28-38% | 18-40% | 0.81-1.03x |
| 10-13 | 30-38% | 22-41% | 0.77-1.05x |

Hit rate roughly triples after round 6. Early picks are priced efficiently and mostly
return slightly less than you paid; late picks are where the market misprices upside.

Players who beat their cost most reliably across five seasons: Tyler Allgeier (4/4 hits,
1.48x), Jakobi Meyers (4/5, 1.38x), Devin Singletary, George Pickens. Persistent busts:
Calvin Ridley (0.65x), Christian McCaffrey (0.70x on an average ECR of 4), Kyle Pitts,
Kyler Murray.

`value_picks` applies the same lens to the live board, surfacing where the model and the
room disagree today.

---

## Three things this does not do

**1. The Rotoballer chart is still unusable, and wouldn't help at a draft.**
The data on that page is a screenshot with nothing to parse. More importantly it's *Week
15* matchup data — a start/sit input, not a draft input. You cannot know in August which
corner shadows your receiver in Week 3. The separation module above covers the
season-long, draft-relevant version of the same question.

**2. Second-year players are still thin.** Rookies are now modelled (see below), but
players with one partial NFL season sit in an awkward middle: enough history to leave the
rookie curve, not enough for the veteran regression to trust. If `value_picks` calls a
young player "overvalued," check games played in `player_report` before acting on it.

**3. ADP still beats consensus.** The server now pulls FantasyPros expert consensus rank
from a maintained data file (reliable, unlike scraping their HTML), current through the
most recent August snapshot. But your league's board is what you're drafting against —
export your platform's ADP to CSV (columns `name`, `adp`) and pass `adp_csv_path` to
`configure_league`.

## Multiple leagues

Name each league and keep as many as you like side by side. A 10-team full PPR and a
13-team half PPR hold **separate boards, separate replacement levels, and separate
in-progress drafts** — switching between them is instant, and neither can see the
other's picks.

```
configure_league(name="home", teams=10, draft_slot=4, scoring="ppr")
configure_league(name="work", teams=13, draft_slot=11, scoring="half_ppr")
switch_league("home")
```

Supported: `ppr` / `half_ppr` / `standard`, any team count, any draft slot, snake or
linear, custom starter counts (`qb`, `rb`, `wr`, `te`, `flex`), `superflex`, and
`te_premium_bonus`.

The formats genuinely change the board rather than just relabelling it:

| Format | Top of board |
|---|---|
| 10-tm full PPR | Nacua (WR), Gibbs (RB), Robinson (RB), Chase (WR) |
| 13-tm half PPR | Gibbs (RB), Robinson (RB), Nacua (WR), Allen (QB) |
| 12-tm standard | Gibbs (RB), Allen (QB), Robinson (RB), Hurts (QB) |
| 12-tm superflex | Allen (QB), Hurts (QB), Gibbs (RB), Robinson (RB) |

Replacement level scales with league size, which is the point: the last startable back
in a 10-team league is a far better player than in a 13-team league, so the same player
is worth less over replacement in the smaller one (RB replacement moves from rank 34 to
45 across those two).

### Scoring-format rankings

Consensus draft rankings are the anchor for the whole opportunity-cost engine, and
**FantasyPros publishes only full PPR** for overall redraft — there is no half-PPR or
standard board upstream. Used unconverted, that misprices exactly the players the format
is about.

So PPR is the baseline, and other formats are converted from it. The market ranking stays
the anchor, because it encodes talent, situation and injury news no model captures; only
the format delta is applied, and that delta is arithmetic rather than opinion — half PPR
is PPR minus half a point per catch, with each player's reception volume coming from his
own projection. The shift is damped to 0.6, because real rooms move less than pure points
math: they also price consistency, scarcity and name recognition, none of which change
with scoring. Undamped, Derrick Henry went from ADP 38 to 1.0 in standard — right
direction, absurd magnitude.

Anything that isn't PPR or half PPR is treated as standard, which is the conservative
choice: it assumes no reception credit rather than inventing one.

| Player | Catches | PPR | Half | Standard |
|---|---|---|---|---|
| Puka Nacua | 110 | 3.1 | 13.3 | 24.1 |
| CeeDee Lamb | 96 | 8.8 | 14.8 | 29.8 |
| Trey McBride | 95 | 19.1 | 41.3 | 66.5 |
| Bijan Robinson | 61 | 3.3 | 6.3 | 10.5 |
| Derrick Henry | 18 | 37.6 | 28.0 | 5.8 |
| Josh Allen | 0 | 25.9 | 25.9 | 25.9 |

Which changes the top of the board, not just the labels:

- **PPR** — Chase, Nacua, Robinson, Gibbs, Smith-Njigba
- **Half PPR** — Robinson, Gibbs, Taylor, Chase, Smith-Njigba
- **Standard** — Henry, Robinson, Gibbs, Taylor, Barkley

The historical backtest is converted the same way, using the *prior* season's points in
both formats so nothing leaks — what a drafter actually knew in August. Without this, a
half-PPR backtest scored PPR rankings against half-PPR finishes and flagged every
reception-heavy receiver as a bust. Quarterback hit rates now hold steady across formats
(29–31%), as they must, since quarterbacks don't catch passes.

**Superflex is handled properly, not just as a roster tweak.** It pulls FantasyPros'
separate superflex consensus, because the two markets barely resemble each other — in
2026 Josh Allen is the 26th pick in a 1-QB league and the **1st overall** pick in
superflex. Pricing a superflex draft off 1-QB rankings would make every quarterback look
like a bargain and wreck the opportunity-cost calculation. The roster logic shifts too:
a second QB is a starter rather than a bench body, so the 1-QB early-round dampener is
switched off. In testing, the superflex plan opens QB-WR-QB while the same league in
1-QB format waits until round 4.

---

## Rookies

Rookies have no NFL history, so the veteran pipeline has nothing to regress — but they
aren't unknowable. Draft capital is a strong predictor of first-year production, because
it encodes both the league's talent evaluation and the opportunity a team commits to a
player it just spent a high pick on.

`rookie_report` returns the class. The curve is fitted from ten years of actual rookie
seasons, not assumed:

| Position | Sample | Played 8+ games | Pick 5 | Pick 40 | Pick 120 |
|---|---|---|---|---|---|
| RB | 215 | 63% | 14.9 | 9.1 | 4.7 |
| QB | 120 | 32% | 12.5 | 7.1 | 4.2 |
| WR | 322 | 64% | 10.6 | 5.8 | 3.2 |
| TE | 141 | 51% | 8.4 | 5.1 | 2.7 |

Two estimators are kept, because neither is safe alone. A log-linear fit is smooth and
uses every data point, but extrapolates badly at the very top of the draft where there
are only a handful of observations — for backs it predicted 19.4 PPG at pick 3 when the
top-ten bin has actually averaged 15.9 across six players in ten years. Empirical bin
medians are honest about that range but noisy. Predictions blend the two by sample size
and cap at the bin's observed 75th percentile, so the model can't promise a rookie
outcome nobody has produced. Medians rather than means throughout, since rookie outcomes
are heavily right-skewed.

Landing spot is applied on top using the same O-line, pace and schedule multipliers
veterans get. Availability scales with draft capital rather than the positional average,
because a top-five pick plays from week one and a sixth-rounder is inactive half the year.

Consistency is deliberately low for every rookie (typically 0.23-0.37 against 0.60-0.71
for established starters). Rookie roles move mid-season and the floor is a healthy
scratch. **Treat rookies as the widest error bars on the board** — the model prices them,
it doesn't pretend to know them.

---

## Name matching

Every source spells players differently, and a silent mismatch is the worst failure mode
here: an unjoined player looks like he scored zero all season, which manufactures fake
busts. All matching runs through one resolver, as a cascade — exact key, alias variants,
last name plus first initial, unique single token, initialism, then fuzzy similarity —
with position used to disambiguate.

Handled: formal/short first names (Joshua ↔ Josh, Kenneth ↔ Ken), generational suffixes,
punctuation and hyphens (`D.J. Moore` ↔ `DJ Moore`, `Jaxon Smith-Njigba` ↔
`Jaxon Smith Njigba`), known alternate names (`Hollywood Brown` → Marquise Brown), bare
surnames and first names people type mid-draft (`Bijan`, `Puka`, `Mahomes`, `CeeDee`),
and initialisms (`JSN`, `ARSB`, `MHJ` generated automatically; `CMC`, `OBJ` mapped
explicitly since they come from capitals inside a surname).

Ambiguity is never guessed. `Jefferson` returns *"ambiguous (2): Justin Jefferson, Van
Jefferson"* rather than picking one. Run `resolve_names` before trusting a paste sync.

This cut unresolved players in the historical backtest to 1.0% (889 exact, 14 alias, 1
fuzzy, 9 genuinely unresolvable), and those 9 are excluded from hit rates rather than
counted as zeros. It also fixed a real error: Joshua Palmer previously showed a 0.00
return across four seasons because FantasyPros writes "Josh" — he was actually a *value*
in 2022 and 2023.

---

## Performance

Profiled and optimised. Warm timings on a full 2026 board (631 players, 247k plays):

| Tool | Before | After |
|---|---|---|
| `who_should_i_pick` | 0.03s | 0.025s |
| `team_context` | 1.13s | 0.006s |
| `defense_report` | 0.49s | 0.010s |
| `separation_report` | 0.86s | 0.007s |
| `plan_my_draft` | — | 0.34s |
| 40 fuzzy name lookups | 0.66s | 0.08s |
| Cold build | 10.2s | 8.1s |
| Play-by-play memory | 54 MB | 22 MB |

What was actually wrong:

- **Every tool re-read parquet from disk.** A quarter-million play-by-play rows cost
  most of a second per call. Now memoised in memory on top of the disk cache.
- **Derived team frames recomputed per call.** O-line, pace, defence and schedule are
  pure functions of cached play-by-play, but each cost a full pass. Now memoised — and
  the server tools were passing `pbp` explicitly, which bypassed the cache entirely.
- **`groupby.apply` in four hot paths.** Floor/ceiling sorted each player-season
  separately; injury risk rebuilt the season-weight table once per player; schedule
  strength and separation summaries built a Series per group. All replaced with
  vectorised masked aggregations.
- **`defense_ratings` did an O(n) `.loc` into the parent frame per group**, via lambdas
  inside `agg`. Replaced with masked groupby passes.
- **Fuzzy matching scored every key in the index.** Now blocks on first letter and
  length before comparing, roughly an order of magnitude fewer comparisons.
- **Float64 and repeated strings in play-by-play.** Downcast numerics and categorised
  team codes and play types.

Two correctness bugs surfaced while fixing the above: the player-name index and the
defence cache were both keyed on `id()`. CPython recycles ids after garbage collection,
so a rebuilt board could land on a freed id and be served a stale index belonging to a
different set of players. Both now key on content.

**Run `prewarm` before your draft.** The first query of a session pays the ~8s build
(minutes on a genuinely cold cache, when it downloads five seasons); everything after is
served from memory.

---

## Tuning

```
Set consistency weight to 0.5 — I want floor over upside.
Set injury weight to 0.15 — I got wrecked by injuries last year.
```

`consistency_weight` at 0 is pure expected points; at 1, pure week-to-week reliability.
The default 0.35 leans toward consistency, per your ask.

---

## Environment variables

| Variable | Purpose |
|---|---|
| `FFDRAFT_SEASON` | Season being drafted (default 2026) |
| `FFDRAFT_SEASONS` | Override the lookback window, e.g. `2021,2022,2023,2024,2025` |
| `FFDRAFT_CACHE` / `FFDRAFT_DATA` / `FFDRAFT_STATE` | Storage paths (default `~/.ffdraft/`) |
| `ESPN_SWID` / `ESPN_S2` | Cookies for private ESPN leagues |

---

## Data attribution

This project computes everything from open sources and ships no third-party data:

- [nflverse](https://github.com/nflverse) — play-by-play, weekly stats, snap counts,
  injury reports, rosters, schedules, draft picks, combine.
- NFL Next Gen Stats (mirrored by nflverse) — separation, cushion, YAC over expected.
- [dynastyprocess/data](https://github.com/dynastyprocess/data) — FantasyPros expert
  consensus rank history.

Please respect the terms of those upstream projects. Nothing here scrapes paywalled
sources, and no proprietary data is redistributed.

## License

MIT — see `LICENSE`.
