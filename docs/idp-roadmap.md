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

### 2. Single-season confidence

Known unvalidated case, carried from the recency-weighting change. A rookie with
one strong season and a five-year veteran are treated as equally certain. They
are not, and one-season players currently top the board.

The 536-defender backtest only covered players *with* 2021-24 history, so this
is genuinely unmeasured rather than measured-and-accepted.

Do not reach for shrinkage toward a cohort mean without testing it. That mean
spans every rotational defender in the league, and the offence model already hit
exactly this trap regressing toward an all-player mean full of third-stringers.

**Evidence required:** a backtest that includes single-season players, comparing
whatever adjustment is proposed against doing nothing. If it does not beat doing
nothing, do nothing and record that.

### 3. CHANGELOG

Explicitly requested. `CHANGELOG.md` follows Keep a Changelog with an
`[Unreleased]` section -- match the existing "Team drive efficiency" and
"Touchdown luck" entries: bolded feature heading, then bullets that explain why
and give measured results.

None of the IDP pull requests touched it, so the entry covers the whole arc, not
just the last change: slot tracking, round arithmetic, the scoring derivation,
the module, the tool, draft timing, forward projection.

### 4. Prewarm the IDP board

`prewarm` builds every cache before draft day. It does not know about IDP, so the
first `idp_report` of a live draft pays full cost. With 90 seconds per pick that
matters.

### 5. IDP rounds in the simulators

`mock_draft` and `plan_my_draft` currently skip IDP rounds entirely, which is
correct as far as it goes -- they cannot recommend a defender. Now that
defenders can be scored and ranked, those rounds could be simulated properly
instead of excluded.

Check first whether this actually improves anything. Excluding them is honest;
simulating them badly is worse than not simulating them.

### 6. Score IDP rounds in draft_backtest

`draft_backtest` labels defensive rounds `your_pick_unmodelled_position` and
leaves them out of the totals. Defenders can now be scored, so those rounds
could carry a real value verdict and count toward the comparison.

Note the totals guard exists for a good reason (see the comment in `adp.py`) --
changing it means re-verifying that the algorithm/optimal comparison stays fair.

### 7. Verify the other IDP slot ids

Only slot `10` (LB) is verified against a real payload. A DL/DB/edge league is
counted correctly but its slots cannot be named. Resolve the same way the
scoring was: against real league data, not a community table.

## Reference

- `docs/idp-research.md` -- why IDP is a separate module, cited to file:line
- `docs/idp-scoring-derivation.md` -- how the ESPN statId mapping was derived,
  what did not resolve, and the measured cost
- `docs/tools.md` -- `idp_report` usage and caveats
