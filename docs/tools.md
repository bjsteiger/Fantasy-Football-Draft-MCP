# Tool reference

Every tool the MCP server exposes. You won't call these by name in practice — ask in
plain language and the model picks — but this is what's available and what each returns.

## Setup

### `configure_league`
Create or update a named league and make it active.

| Argument | Default | Notes |
|---|---|---|
| `name` | `"default"` | Any label. Reusing a name updates that league. |
| `teams` | 12 | Any size. |
| `draft_slot` | 6 | Your first-round pick, 1-indexed. Validated against `teams`. |
| `rounds` | 16 | |
| `scoring` | `"half_ppr"` | `ppr`, `half_ppr`, `standard`. |
| `snake` | `true` | `false` for linear drafts. |
| `qb` `rb` `wr` `te` `flex` | 1/2/2/1/1 | Starting slots. |
| `superflex` | 0 | Slots where a QB may also start. |
| `te_premium_bonus` | 0.0 | Extra points per TE reception. |
| `consistency_weight` | 0.35 | 0 = pure upside, 1 = pure floor. |
| `adp_csv_path` | — | Your platform's ADP export. Beats consensus. |

### `list_leagues` / `switch_league` / `remove_league`
Manage multiple leagues. Switching is instant — boards are cached per format.

### `prewarm`
Build every cache. **Run before draft day.** The first query otherwise pays ~8 seconds,
or minutes on a genuinely cold cache.

### `refresh_data`
Rebuild from source. `force_download=true` re-downloads everything; use when nflverse
publishes new data mid-season.

### `model_settings`
Retune factor weights: `consistency_weight`, `injury_weight`, `oline_weight`,
`schedule_weight`, `pace_weight`. Rebuilds the board.

## During the draft

### `on_the_clock`
The whole on-the-clock workflow in one call: `sync_draft` (fresh pull, no cached
state) → `draft_status` (round, on-the-clock, roster, confirmed against the sync)
→ `who_should_i_pick` (recommendation, reasoning, survival odds) → `value_picks`
(scoped to your current round and next) → `separation_report`, appended only when
the top recommendation is a WR or TE, for that player's route efficiency and
schedule context. Takes `sync_draft`'s arguments (`platform`, `league_id`,
`draft_id`, `pasted_board`, `season`) plus `limit` for how many recommendations
`who_should_i_pick` returns. Use this instead of the five calls separately when
you're on the clock and want the full picture at once.

### `who_should_i_pick`
The main one. Returns ranked recommendations with reasoning, the pick being evaluated,
your roster, and each player's odds of surviving to your next pick.

### `best_available`
Next best on the board. `sort_by`: `draft_score` (balanced), `vor`, `consistency`,
`proj_points`, or `value` (biggest ADP-to-model gap). Filter with `position`.

### `sync_draft`
Pull the live board.

- `platform="sleeper"`, `draft_id` — automatic, public API, no credentials.
- `platform="espn"`, `league_id` — public leagues work as-is; private need
  `ESPN_SWID` and `ESPN_S2` environment variables.
- `platform="paste"`, `pasted_board` — any platform. Handles numbered lists,
  "Round 3, Pick 7 — Name", comma-separated runs, trailing team and position tags.

### `record_pick` / `undo_pick` / `reset_draft` / `draft_status`
Manual board management. `record_pick` accepts shorthand.

### `plan_my_draft`
Simulate every remaining pick from your slot. `strategy`: `balanced`, `zero_rb`,
`hero_rb`, `robust_rb`. ADP-driven, so treat it as preparation rather than a script.

## Research

### `player_report`
Every modelled factor for one player: production, role, environment multipliers, injury
components, separation, draft capital for rookies.

### `compare_players`
Two to four players head to head, with a verdict.

### `rookie_report`
This year's class, projected from draft capital and landing spot. Widest error bars on
the board.

### `separation_report`
Separation, cushion, YPRR and TPRR. Pass `player_name` for one player's history, or
`position` for a leaderboard. Only players clearing 250 estimated routes and 50 targets.
Ranked by `sep_score` (talent). Also returns `matchup_z` (the player's team's upcoming
schedule difficulty at that position -- the season-long, team-level stand-in for a
WR/CB matchup chart), shown for reference only: a backtest (`matchup_backtest`) found
blending it into the ranking made WR predictions worse than talent alone, not better.

### `value_picks`
Where the model disagrees with the market. `direction`: `undervalued` or `overvalued`.
Restricted to players the market actually ranks.

### `team_context`
An NFL team's offensive environment: O-line ranks with history, pace, run/pass split,
schedule difficulty, divisional games.

### `defense_report`
Fantasy points allowed by position, current season and five-year. Rank 1 = toughest.

### `draft_value_history`
Backtest consensus rank against actual finish. `group_by`: `draft_round` or `position`.
Converted to your scoring format.

### `persistent_value_players`
Players who beat their draft cost repeatedly rather than once.

### `matchup_backtest`
Validates whether blending schedule difficulty into talent predicts actual finish
better than talent alone. Talent comes from the *prior* season's separation score
only, schedule difficulty from the same leakage-free `strength_of_schedule` the live
recommender uses — nothing here has seen the season it's scoring. Reports Spearman
correlation and top-N precision for both metrics side by side, plus the players
where schedule swung the pick most. 2021-2024 result for WR: talent alone wins —
`separation_report` ranks by `sep_score` accordingly. Re-run this if the model
changes to see whether that still holds.

### `draft_backtest`
Replays a real past ESPN draft round by round: what `who_should_i_pick`'s algorithm
would have recommended given the real board at that exact moment, the true
hindsight-optimal pick by value over replacement (QB capped at 1 — a second
quarterback can't start, so it isn't ranked against real RB/WR/TE need), and what
you actually took, all scored on real points from that season. `league_id` and
`season` are all it needs — your team and draft slot are auto-detected from
`ESPN_SWID`/`ESPN_S2`, and league settings (teams, scoring, roster) are read
straight from ESPN. The board is leak-free: every history-derived input is bounded
to seasons strictly before the one being predicted, same as `matchup_backtest`.

Each of the three picks in a round also carries a value verdict (preseason ECR
against actual finish — the `value_picks` steal/bust framing, against real
outcomes instead of projections) and team context (that player's team's O-line
ranks, pace, and schedule difficulty for the season being tested — the same
numbers `team_context` reports, but leak-free for a past season instead of
always reading today's). K/DST aren't modelled anywhere in this tool, so those
rounds report your actual pick only, with no value or team context. ESPN only,
for now.

### `mock_draft`
Monte Carlo mock draft: the live algorithm against many simulated opponents,
averaged. Unlike `draft_backtest`, no real draft is needed or used — the other
teams are bots that pick by that season's real preseason ADP with realistic
reach/fall noise (bigger swings plausible late, tight consensus at the top)
rather than following it exactly, so who's actually on the board at your turn
varies draw to draw. Your slot (from your *active* configured league — run
`configure_league` first) runs the same `recommend()` logic `who_should_i_pick`
uses live, and everything is scored on real points from `season` against the
same leak-free board `draft_backtest` builds.

One draw can make the algorithm look better or worse than its true average just
from bot luck, which is why this runs `n_trials` (default 30) and reports the
mean/median/range, not a single result. For each round it also reports the most
common picks and how often each showed up — rounds with no real consensus
(usually round 6+) should be read as "plausible outcomes," not "the pick." K/DST
aren't modelled, so only skill-position rounds run (your league's total rounds
minus its K and DST starting slots).

### `resolve_names`
Check how names resolve before trusting a paste sync. Reports match type per name.

## Environment variables

| Variable | Purpose |
|---|---|
| `FFDRAFT_SEASON` | Season being drafted (default 2026) |
| `FFDRAFT_SEASONS` | Override lookback, e.g. `2021,2022,2023,2024,2025` |
| `FFDRAFT_CACHE` / `FFDRAFT_DATA` / `FFDRAFT_STATE` | Storage paths |
| `ESPN_SWID` / `ESPN_S2` | Private ESPN leagues — see [SECURITY.md](../SECURITY.md) |
