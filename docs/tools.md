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

### `resolve_names`
Check how names resolve before trusting a paste sync. Reports match type per name.

## Environment variables

| Variable | Purpose |
|---|---|
| `FFDRAFT_SEASON` | Season being drafted (default 2026) |
| `FFDRAFT_SEASONS` | Override lookback, e.g. `2021,2022,2023,2024,2025` |
| `FFDRAFT_CACHE` / `FFDRAFT_DATA` / `FFDRAFT_STATE` | Storage paths |
| `ESPN_SWID` / `ESPN_S2` | Private ESPN leagues — see [SECURITY.md](../SECURITY.md) |
