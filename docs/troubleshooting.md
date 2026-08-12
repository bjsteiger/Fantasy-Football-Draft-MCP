# Troubleshooting

## Setup

**`ModuleNotFoundError: No module named 'ffdraft'`**
Install in editable mode from the repo root: `pip install -e .`

**`ImportError: cannot import name 'FastMCP'`**
The MCP SDK renamed its server class between 1.x and 2.x. The server handles both, so
this usually means a partial install: `pip install --upgrade mcp`

**`pyarrow` errors on first run**
Parquet needs it: `pip install pyarrow`

**The server doesn't appear in Claude**
Check the config path is right for your OS, use an absolute path to the Python that has
`ffdraft` installed, and restart Claude fully. Verify the server runs standalone:
`python -m ffdraft.server` — it should start and wait on stdio.

## Data

**First run takes minutes**
Expected. It downloads about 200 MB of play-by-play. Run `prewarm` before draft day so
this never happens on the clock.

**`no seasons loaded for .../player_stats_YYYY.parquet`**
nflverse renamed this release to `stats_player_week` from 2025. Both layouts are handled,
so this means the season isn't published yet. Set `FFDRAFT_SEASONS` to seasons that exist.

**Stale data mid-season**
`refresh_data(force_download=true)`

**Reclaiming disk space**
Delete `~/.ffdraft/cache/`. It rebuilds on the next run.

## Leagues

**Recommendations are for the wrong pick**
Check `draft_status`. If `my_slot` is wrong, re-run `configure_league` with the right
`draft_slot` — league config is authoritative.

**One league shows another's picks**
Shouldn't happen — state is per league. Confirm with `list_leagues` that you're on the
league you think, and that `picks_recorded` looks right.

**Switching leagues rebuilds the board**
Only when the format differs. Leagues sharing scoring, size and roster settings share a
cached board; only draft state and your slot differ.

## Drafting

**A drafted player still shows as available**
The name didn't resolve. Check with `resolve_names`, then record using the resolved
spelling. Report the miss as an issue — the alias map grows from real cases.

**"ambiguous (2): Justin Jefferson, Van Jefferson"**
Working as intended. Two players match; give a first name.

**Sleeper sync returns nothing**
Verify the draft id from the URL (`sleeper.com/draft/nfl/<draft_id>`) — that's the draft
id, not the league id. Mock drafts have their own ids.

**ESPN sync returns 401 or empty**
Private leagues need `ESPN_SWID` and `ESPN_S2` set before the server starts. Cookies
expire — log into ESPN and copy fresh ones. See [SECURITY.md](../SECURITY.md).

**ESPN picks show as `ESPN#12345`**
The player id didn't map. Usually a rookie missing from the crosswalk. Record manually.

## Results that look wrong

**A young player is flagged "overvalued"**
The model is backward-looking, so it fades rookies and second-year players. Check
`player_report` for games played before acting on it.

**Rookies rank lower than expected**
By design. Their consistency prior is deliberately low and projections cap at the
observed 75th percentile for their draft slot. See
[methodology](methodology.md#rookies).

**Quarterbacks rank higher than they'd go in a real draft**
Correct in raw value. `who_should_i_pick` applies opportunity cost and roster need on top,
which is why it won't recommend one early in a 1-QB league. In superflex it will.

**Someone missing from `separation_report`**
Under 250 estimated routes or 50 targets. Rate stats on part-time usage are misleading.

**Model disagrees with my rankings**
It's opinionated. Tune with `model_settings` — raise `consistency_weight` for floor,
`injury_weight` if injuries burned you.

## Getting help

Open an issue with what you ran, what you expected, what happened, plus your league
format and the output of `list_leagues`. **Never include ESPN cookies.**
