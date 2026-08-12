# Quickstart

From nothing to a live draft assistant in about ten minutes, most of which is a
one-time data download.

## 1. Install

```bash
git clone https://github.com/OWNER/ff-draft-mcp.git
cd ff-draft-mcp
pip install -e .
```

Python 3.10 or newer.

## 2. Build the data cache

```bash
python setup_data.py
```

Downloads five seasons of play-by-play, weekly stats, snap counts, injury reports,
rosters, schedules and draft picks — roughly 200 MB, three to six minutes. Everything
after this is served from cache.

You should see something like:

```
  weekly_stats        28,026 rows   (2s)
  snap_counts        131,003 rows   (3s)
  play_by_play       247,284 rows   (94s)

Building player board...
  631 players: {'WR': 232, 'RB': 168, 'QB': 118, 'TE': 113}
```

## 3. Connect it to Claude

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fantasy-draft": {
      "command": "python",
      "args": ["-m", "ffdraft.server"],
      "env": { "FFDRAFT_SEASON": "2026" }
    }
  }
}
```

Restart Claude. You should see the fantasy-draft tools available.

## 4. Set up your leagues

```
Set up my home league: 10 teams, full PPR, I pick 4th.
Set up my work league: 13 teams, half PPR, I pick 11th.
```

Each keeps its own board, replacement levels and draft state.

## 5. Before draft day

```
Run prewarm.
```

Builds every cache so nothing computes while you're on the clock. Do this an hour
before, not during.

## 6. During the draft

```
Switch to my home league. Sync my Sleeper draft, id 1234567890.
Who should I pick?
```

A typical answer:

> **Take Amon-Ra St. Brown.** WR4 by projection (245 pts, 16.1/gm); consistency 0.63,
> startable in 58% of weeks; volume/pace +2.3%; schedule +2.2%; injury risk 16%
> (~15 games); 8% chance he lasts to your next pick.
>
> Josh Allen grades higher overall, but he has a 73% chance of surviving to pick 19 and
> St. Brown does not.

Then re-sync between picks:

```
Sync again. Who's the best available now?
```

If you're not on Sleeper, record picks as they happen:

```
Record: Bijan, then CMC, then JSN.
```

Shorthand works — bare surnames, nicknames and initialisms all resolve.

## Useful questions mid-draft

```
Best available running back.
Compare Jonathan Taylor and Breece Hall.
What's Trey McBride's injury risk?
Show me undervalued players.
Who are the rookies worth taking?
What does my roster look like?
```

## If something looks wrong

Check the name resolved to who you meant:

```
Resolve these names: JSN, CMC, Bijan
```

And see [troubleshooting](troubleshooting.md).
