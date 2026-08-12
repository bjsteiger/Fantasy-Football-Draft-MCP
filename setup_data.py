#!/usr/bin/env python3
"""One-time data build. Run before draft day so nothing downloads mid-draft."""
import warnings, time
warnings.filterwarnings("ignore")
from ffdraft import server, sources

t = time.time()
print("Downloading nflverse data (play-by-play is the slow part)...")
for fn in (sources.weekly_stats, sources.snap_counts, sources.injuries,
           sources.weekly_rosters, sources.players, sources.schedules,
           sources.play_by_play):
    d = fn()
    print(f"  {fn.__name__:<18}{d.shape[0]:>8,} rows   ({time.time()-t:.0f}s)")

print("\nBuilding player board...")
import json
r = json.loads(server.refresh_data())
print(f"  {r['players_modelled']} players: {r['by_position']}")
print("\nTop 10:")
for p in r["top_10"]:
    print(f"  {p['name']:<24}{p['position']:<4}{p['team'] or '':<5}"
          f"{p['proj_points']:>7.1f} pts   consistency {p['consistency']:.2f}")
print(f"\nDone in {time.time()-t:.0f}s. Cache is warm — drafts will be instant.")
