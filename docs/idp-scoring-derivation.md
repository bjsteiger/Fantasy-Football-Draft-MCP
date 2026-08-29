# Deriving ESPN's IDP scoring from data

How individual defensive player scoring was worked out for this tool, and how
accurately it can be reproduced from nflverse. Nothing here is asserted from
memory: every `statId` below was matched against real player-seasons, and the
cases that did not match are recorded as not matching.

Worked against a real 10-team PPR league with one LB slot, 2025 actuals, 38
linebackers cross-matched between ESPN and nflverse.

## Why this had to be derived rather than looked up

ESPN publishes 63 `scoringItems` per league keyed by numeric `statId`, with no
names attached. Community `statId` tables exist but disagree with each other,
and a wrong mapping here is silent -- it produces a plausible-looking points
total that is simply wrong. Since scoring is the foundation everything else sits
on, it was worth resolving empirically.

## Method

1. Read the league's own `scoringSettings.scoringItems` (`view=mSettings`) for
   the points value attached to each `statId`.
2. Pull real 2025 season actuals per player (`view=kona_player_info`,
   `statSourceId=0`, `statSplitTypeId=0`), which carry both the raw stat counts
   and ESPN's own computed `appliedTotal`.
3. Reconcile: multiply raw counts by the league's points values and check the
   result equals `appliedTotal`. This confirms the *scoring* without yet knowing
   what any stat is called.
4. Match each `statId`'s raw values against every nflverse `def_*` column across
   all 38 players. Only a column matching *every* player is treated as
   identified.

## Step 3 result: scoring reconciles exactly

Jack Campbell, 2025:

| statId | raw | × points | = |
|---|---|---|---|
| 96 | 2.0 | 2.0 | 4.0 |
| 99 | 5.0 | 0.5 | 2.5 |
| 106 | 3.0 | 4.0 | 12.0 |
| 107 | 87.0 | 1.0 | 87.0 |
| 108 | 89.0 | 2.0 | 178.0 |
| 109 | 176.0 | 1.0 | 176.0 |
| 113 | 4.0 | 1.5 | 6.0 |

Computed **465.5**, ESPN `appliedTotal` **465.5**. Exact.

Note that tackles are counted twice by design in this league: the solo/assist
split (107/108) scores, and the total (109) scores again on top.

## Step 4 result: the mapping

Confirmed -- exact match for every player who recorded the stat:

| statId | nflverse column | players matched |
|---|---|---|
| 95 | `def_interceptions` | 21 |
| 99 | `def_sacks` | 32 |
| 106 | `def_fumbles_forced` | 23 |
| 109 | `def_tackles_solo` + `def_tackle_assists` + `def_tackles_with_assist` | 38 |
| 113 | `def_pass_defended` | 37 |

`99 = def_sacks` is worth calling out: in the first player checked, sacks and QB
hits were both 5, so that one player could not distinguish them. Across 32
players they diverge, and sacks match while QB hits do not.

### Not identified: the 107/108 tackle split

`107 + 108` always equals `109`, and `109` matches nflverse exactly for all 38
players -- so the *total* is reliable. But neither 107 nor 108 individually
matches nflverse's solo/assist columns, and the errors are equal and opposite in
every case: ESPN and nflverse agree on how many tackles a player made and
disagree on how many were solo.

Mean absolute difference: **10.45 tackles per player-season** on each side.

This is a provider disagreement, not a bug in either source. Tackle attribution
is an unofficial statistic compiled by human scorers, and it is well known to
differ between providers. It cannot be fixed from this side; it can only be
measured, which is what follows.

## What this costs in practice

Predicting ESPN's IDP points purely from nflverse, using the mapping above and
nflverse's own solo/assist split:

| metric | value |
|---|---|
| mean absolute error | 11.3 pts (3.5%) |
| median error | 3.0% |
| worst case | 11.9% |
| Spearman rank correlation | **0.9715** |
| Pearson | 0.9844 |
| top-10 overlap | 9/10 |
| top-15 overlap | 13/15 |

The error is almost entirely the tackle split, and it is nearly unbiased -- it
moves players a few percent, rarely past their neighbours. For drafting, order
is what matters rather than absolute totals, and rank correlation of 0.97 is
comfortably good enough to rank a board.

Not good enough for: reporting a projected point total as though it were exact,
or distinguishing two linebackers within ~12 points of each other.

## Positional value, for the draft itself

2025 actuals under this league's rules, which is what an IDP board has to price:

| rank | player | points |
|---|---|---|
| LB1 | Jordyn Brooks | 477.2 |
| LB3 | Devin White | 454.8 |
| LB5 | Bobby Wagner | 415.2 |
| LB10 | Bobby Okereke | 379.5 |
| LB20 | Alex Singleton | 334.5 |
| LB30 | Akeem Davis-Gaither | 302.5 |

Raw totals dwarf offensive players (the top projected QB is ~356), but absolute
points are **not comparable across positions** -- only value over replacement
is. In a 10-team league starting one LB, replacement is about LB10:

- VOR of LB1: **+97.8**
- VOR of LB3: **+75.2**
- VOR of LB5: **+35.8**

So the position is not the throwaway round it looks like: the gap between the
best linebacker and a replacement one is real. The curve is also flat after
about LB5, which is the shape that decides *when* to spend a pick rather than
whether to.

## Open questions

- Only slot id `10` (LB) is verified. DL/DB/CB/S/edge slot ids are unverified,
  which is why `starters_from_slot_counts` counts unrecognised starting slots
  generically instead of naming them.
- `position` fragments the linebacker group four ways (LB/OLB/MLB/ILB);
  filtering `position == "LB"` drops ~21% of LB player-weeks. `position_group`
  is the correct key and is not currently referenced anywhere in `src/`.
- There is no IDP market in the cached ECR/ADP data, so opportunity cost for
  defenders cannot use the same machinery offensive players do.
