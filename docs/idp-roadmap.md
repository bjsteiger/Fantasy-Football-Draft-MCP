# IDP roadmap

Remaining work to finish individual-defensive-player support, written so each
task can be picked up and finished independently without re-deriving context.

Source of truth for what is left. Update it as tasks land.

## How a task is done here

Every task follows the same loop, and none of it is optional:

1. **Research** the actual source before changing it. Cite `file.py:line`.
2. **Design** to the seam that already exists. This codebase has strong
   precedents -- `defense_report` is its own tool; `separation_report` is a
   specialist module that `on_the_clock` surfaces automatically. Follow them.
3. **Tests first**, offline. No network, no cached data. See `tests/test_idp.py`.
4. **Implement.**
5. **Print the real artifact and read it.** Not the tests -- the actual board,
   the actual recommendation, the actual JSON. Compare it against a stated
   expectation written down *before* looking.
6. **One PR per task**, with the evidence from step 5 in the description.

Step 5 is the one that matters and the one most likely to be skipped. Every
significant defect found in this project so far passed its tests and its linter
first:

- A board that ranked a defensive back with one 38-point game at number one on
  646 projected points, ahead of the two best linebackers in the league.
- An IDP average draft position that would have priced the consensus IDP1 as a
  top-three overall pick, because IDP consensus is published on a different
  scale from the overall board.
- A test asserting a steeper recency tilt than `RECENCY_WEIGHTS` actually
  applies. The test was wrong; the code was right.

None of those were caught by tooling. All were caught by looking at output and
not believing it. If a task's evidence section says only "tests pass", it is not
done.

## Standing rules

- Never widen `FANTASY_POSITIONS`. IDP follows the K/DST contract: tracked in
  `starters`, never modelled. Widening it fabricates `fpa_LB`/`sos_LB_z` and
  multiplies real QB/RB/WR/TE projections by a category error. See
  `docs/idp-research.md`.
- Filter on `position_group`, never `position` -- the latter splits linebackers
  four ways and drops about a fifth of the pool.
- Do not invent a constant to fill a gap. If the data does not support a number,
  say so and leave it out. Precedent: no IDP age curve, because linebacker
  production is near-flat 27-32 and confounded by survivorship.
- Defenders have no usable per-player ADP. IDP consensus correlated 0.30 with
  actual pick in this league. Use the timing envelope from `idp.draft_timing`.
- Ranking is trustworthy (0.97 rank correlation); point totals are ~3.5%
  approximate. Never present an IDP projection as exact.

## Tasks

### 1. Unify the interface  *(highest value -- this is the user-visible problem)*

Having to call a separate tool for one roster slot is unnatural. The split is an
implementation detail that leaked into the interface. Internals stay separate;
the interface should not be.

Follow the `separation_report` precedent: specialist module underneath, surfaced
automatically by the main flow.

- `draft_status` -- show an unfilled IDP slot. It currently does not appear at
  all, so the roster looks complete when it is not.
- `who_should_i_pick` -- include a defender among the recommendations when the
  IDP slot is unfilled and the defender's VOR is genuinely competitive.
- `on_the_clock` -- surface the IDP pick the same way separation data is
  appended for a WR/TE recommendation.
- `best_available` -- accept `LB`/`DL`/`DB`.
- `idp_report` stays, for detail.

**Prerequisite, now met:** IDP VOR is forward-looking as of the recency-weighting
change, so it is comparable with offensive VOR. Both are denominated in the same
league's points, and a weekly score sums starters regardless of position.

**Evidence required:** a real `who_should_i_pick` at a round 9-10 pick showing a
defender ranked against real offensive alternatives, with the VOR numbers, and a
statement of whether the ordering is defensible. At picks 85-96 the best
available offensive VOR was +38.6 (RJ Harvey) against a mid-tier linebacker at
+36.8 -- if defenders come out dominating that range, something is wrong.

### 2. Single-season confidence  *(done -- [issue #33](https://github.com/bjsteiger/Fantasy-Football-Draft-MCP/issues/33))*

Known unvalidated case, carried from the recency-weighting change. A rookie with
one strong season and a five-year veteran were treated as nearly equally
certain, and one-season players topped the board.

Reproduced 2026-08-30 against the real league board: Carson Schwesinger (1
season, 16 games) ranked pos_rank 3 at 411.1 projected points, ahead of Bobby
Wagner (5 seasons, 83 games) at 408.3.

#### Where the error actually was

The pool-wide numbers say the opposite of the complaint, and that is the useful
part. Across every qualified defender a one-season player is *under*-projected:
bias -0.33 ppg predicting 2024, -0.57 ppg predicting 2025. Shrinking on that
basis alone would have been wrong.

It inverts at the top of the board, which is the only part anyone drafts. Among
the twenty players the board ranked highest, predicting 2025:

| cohort | n | bias (pred - actual) | mean actual rank |
|---|---|---|---|
| one season | 1 | **+9.07 ppg** | **172** |
| two or more | 19 | +1.42 ppg | 27 |

Predicting 2024, over the top 50: +0.90 against +0.29, mean actual rank 146
against 51. A thin-evidence player reaches the top of the board by having had
one good run, and a good run is exactly what does not repeat.

#### What was tested

Three folds, each a real held-out season: 2023 from 2021-22, 2024 from 2021-23,
2025 from 2021-24. The 2024 and 2025 folds use this league's own ESPN scoring
for those seasons (they differ sharply -- the league sextupled its IDP scoring
between them); the 2023 fold reuses the 2025 ruleset, since the league did not
exist then, and is a robustness check rather than a live-league result. Truth is
actual ppg in the held-out season for defenders with >= 8 games.

Candidates: doing nothing; a flat pull on one-season players at 0.15 through
0.65; seasons-tiered pulls; and a games-based empirical-Bayes weight
`g/(g+g0)` applied to everyone. Anchors: upper-half mean, all-player mean,
top-quartile mean.

| | MAE 2023 | MAE 2024 | MAE 2025 | rho 2023 | rho 2024 | rho 2025 |
|---|---|---|---|---|---|---|
| do nothing | 2.5814 | 0.5462 | 2.4444 | 0.7112 | 0.6868 | 0.7393 |
| 0.15 one-season (shipped in #14) | 2.5283 | 0.5348 | 2.4039 | 0.7207 | 0.6904 | 0.7431 |
| **0.20 / 0.10 tiered (adopted)** | **2.5350** | **0.5303** | **2.3873** | **0.7188** | **0.6967** | **0.7466** |

The adopted pair beats doing nothing on both metrics in all three folds, has the
best average error rank of every candidate tried, and each value is at or within
0.001 of its own fold-by-fold optimum. What lost:

- **Stronger pulls.** 0.35 and 0.40 stopped beating doing nothing.
- **Games-based shrinkage** `g/(g+g0)`. Best of all candidates on the top-30
  error, and clearly worst on pool-wide error (0.55 -> 0.69 at g0=32) because it
  drags every player toward the anchor, including the ones with five seasons of
  evidence. Not adopted.
- **A third tier** for three-season players at 0.05. Moved error by 0.0005 and
  split the folds on ranking. That is not evidence for a constant, so it was
  left out -- same rule as the missing age curve.
- **The all-player mean as anchor.** Slightly better on error, worse on ranking
  in all three folds. Ranking is what a board is for, and this is the anchor the
  offence model already got burned by.

#### Live board, before and after

Stated before looking: Schwesinger should fall behind Wagner and Oluokun, Cedric
Gray (also one season) should drop out of the top eight, and no multi-season
projection should move at all.

```
 before                          after
  1 Alex Singleton      415.8     1 Alex Singleton      415.8
  2 Jordyn Brooks       411.7     2 Jordyn Brooks       411.7
  3 Carson Schwesinger  411.1     3 Bobby Wagner        408.3
  4 Bobby Wagner        408.3     4 Foye Oluokun        407.3
  5 Foye Oluokun        407.3     5 Roquan Smith        405.7
  6 Roquan Smith        405.7     6 Carson Schwesinger  399.4
  7 Zaire Franklin      399.3     7 Zaire Franklin      399.3
  8 Cedric Gray         397.3     8 Blake Cashman       390.1
  9 Blake Cashman       390.1     9 Cedric Gray         386.4
 10 Ernest Jones        381.8    10 Ernest Jones        381.8
```

Schwesinger 3 -> 6, Gray 8 -> 9, Edgerrin Cooper (two seasons) 32 -> 37. Every
multi-season projection is unchanged to the tenth of a point; replacement level
(Ernest Jones, 381.8) does not move, so VOR stays comparable with the offence
board.

### 3. CHANGELOG

Explicitly requested. `CHANGELOG.md` follows Keep a Changelog with an
`[Unreleased]` section -- match the existing "Team drive efficiency" and
"Touchdown luck" entries: bolded feature heading, then bullets that explain why
and give measured results.

None of the IDP pull requests touched it, so the entry covers the whole arc, not
just the last change: slot tracking, round arithmetic, the scoring derivation,
the module, the tool, draft timing, forward projection.

### 4. Prewarm the IDP board  *(done -- #18)*

`prewarm` builds every cache before draft day. It does not know about IDP, so the
first `idp_report` of a live draft pays full cost. With 90 seconds per pick that
matters.

Verified 2026-08-30: `prewarm(league_id=...)` builds the defender board in 0.33s
alongside every other step.

### 5. IDP rounds in the simulators  *(done for plan_my_draft; declined for mock_draft)*

Checked before building, as the task said to, and the answer differed by tool.

`plan_my_draft` now reports `idp_pick` -- the defenders worth targeting -- beside
the plan rather than inside it. The plan is built pick by pick from ADP, modelling
who realistically falls to you at each turn, and defenders have no usable draft
position, so there is no honest way to say which round one lands in. The league's
own history supports a window, not a round, so a window is what is given.

`mock_draft` is deliberately left alone. Its opponents are ADP bots, and without
a defensive market there is nothing for them to draft against. Inventing bot
behaviour for a position whose real timing correlated 0.30 with published
consensus would add noise and call it a simulation -- worse than the honest
exclusion it replaces.

### 6. Score IDP rounds in draft_backtest  *(done -- #19)*

`draft_backtest` labels defensive rounds `your_pick_unmodelled_position` and
leaves them out of the totals. Defenders can now be scored, so those rounds
could carry a real value verdict and count toward the comparison.

Note the totals guard exists for a good reason (see the comment in `adp.py`) --
changing it means re-verifying that the algorithm/optimal comparison stays fair.

### 7. Verify the other IDP slot ids  *(investigated -- not resolvable, closed)*

Attempted the same way the scoring was derived: read `eligibleSlots` off real
players and infer which slot id belongs to which position.

It does not resolve. Eligibility spans hybrid and utility slots rather than
mapping cleanly to positions -- Ja'Marr Chase, a receiver, carries slots 14 and
15; a safety carries slot 10; a defensive lineman carries running back slots.
Only `10 = LB` is unambiguous, and that is already encoded.

Deriving a DL/DB/edge table from this would mean guessing, which is what
`_ESPN_KNOWN_IDP_SLOTS` exists to avoid. The generic count in
`starters_from_slot_counts` already keeps round arithmetic correct for those
leagues without naming their slots, which is the part that actually matters.

Reopen only with access to a real DL/DB league's payload, where the slot ids in
`lineupSlotCounts` can be read against a roster that actually uses them.

## Known issues found outside this list

- [Issue #32](https://github.com/bjsteiger/Fantasy-Football-Draft-MCP/issues/32)
  *(fixed)* -- `configure_league` rebuilt `ModelWeights` from scratch on every
  call, so any tuned weight other than `consistency_weight` (schedule, injury,
  oline, td_luck, qb_boost...) silently reset to its default the next time
  `configure_league` ran for an existing league, including just to change
  `idp`/`rounds`/`draft_slot`. Not IDP-specific, but found while auditing this
  work. `model_settings` itself was unaffected. It now loads the league's
  existing weights and overwrites only `consistency_weight`, and only when
  passed; the response echoes `weights` so a reset could not be silent again.

## Reference

- `docs/idp-research.md` -- why IDP is a separate module, cited to file:line
- `docs/idp-scoring-derivation.md` -- how the ESPN statId mapping was derived,
  what did not resolve, and the measured cost
- `docs/tools.md` -- `idp_report` usage and caveats
