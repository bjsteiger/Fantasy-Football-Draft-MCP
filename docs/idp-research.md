# IDP (LB) research

Research pass only — no code was changed to produce this. Every claim below is cited to the
source file and line that owns it. Where the existing docs disagree with the code, the code
wins and the disagreement is called out.

The concrete motivating case: an ESPN league with **one LB starting slot**. The tool models
QB/RB/WR/TE and nothing else (`config.py:25`).

---

## Executive summary

**Recommendation: a standalone `ffdraft/idp.py` module, not an extension of the offense model.
Do not add `"LB"` to `FANTASY_POSITIONS`.**

The seam is not where it first looks. `FANTASY_POSITIONS` reads like the one switch that
gates the player universe, but it is actually used in eight files for five *different*
purposes — universe filter, defensive-strength pivot column set, schedule z-score column set,
VOR loop, and roster-need loop — and widening it changes all five at once with different
consequences in each. Three of those five would produce silently wrong numbers rather than
an error.

The evidence, in order of how decisive it is:

1. **Scoring cannot express an LB at all.** `features.fantasy_points` (`features.py:24-43`)
   sums only passing, rushing, receiving, fumble and 2pt columns. The `Scoring` dataclass
   (`config.py:41-54`) has ten fields, none defensive. An LB row fed through
   `fantasy_points` scores **exactly 0.0** — the `g()` helper at `features.py:26` returns
   `0.0` for any absent column, so this fails silently, not loudly. Nothing downstream can
   be salvaged until new scoring fields exist, and adding them means touching
   `Scoring.preset` (`config.py:56-65`) and `cache_key` (`config.py:127`) too.

2. **Two of the eight projection multipliers would be actively wrong for an LB, in a
   direction the code cannot detect.** In `model.project`:
   - `m_oline` (`model.py:316-318`) gives `run_block_z` to RBs and `pass_block_z` to
     everyone else. An LB would be graded on *his own team's pass protection* — a number
     with no causal relationship to his tackle volume.
   - `m_volume` (`model.py:321-324`) gives `rush_rate` to RBs and `neutral_pass_rate` to
     everyone else. LB production is driven by the *opposing* offense's volume and run
     rate; the sign is arguably inverted, not merely irrelevant.

   The remaining multipliers degrade gracefully rather than wrongly: `m_separation` is gated
   on `position.isin(["WR","TE"])` (`model.py:357`) so LBs sit at neutral 1.0; `m_td_luck`
   keys on red zone touches (`model.py:379-380`) which are zero for an LB, and
   `touchdown_luck_multiplier` pins anything under `min_touches=8` to exactly 1.0
   (`model.py:44,57`). `m_schedule` (`model.py:328-332`) would need an `sos_LB_z` column
   that only exists if `defense_ratings` starts pivoting LB — see point 3.

3. **`defense_ratings` would produce a nonsense column if `FANTASY_POSITIONS` widened.**
   `features._defense_ratings:206` filters weekly stats to `FANTASY_POSITIONS`, then
   `:213-216` pivots fantasy points allowed per position into `fpa_QB`/`fpa_RB`/… keyed on
   `opponent_team`. With LB in the tuple this produces `fpa_LB` = "fantasy points a defense
   allows to opposing linebackers," which is not a thing — the opponent's LBs are on the
   *other* defense. That column then flows into `_strength_of_schedule` via the
   `startswith("fpa_")` sweep at `features.py:386` and becomes `sos_LB_z` at
   `features.py:414-417`, which `model.project:328-332` would then apply as a real
   multiplier. This is the single most dangerous consequence of the naive approach: a
   fabricated signal reaching `draft_score` with no error raised.

4. **The parts genuinely worth reusing are already position-agnostic.** `model.project`'s
   VOR block (`model.py:431-439`) only needs a replacement rank per position;
   `_positional_need` (`model.py:578-625`) is generic apart from hard-coded QB/TE special
   cases (`model.py:594-598`, `model.py:621-624`); `expected_best_at_next_pick`
   (`model.py:539-559`) groups by position with no positional knowledge at all;
   `survival_probability_vec` (`model.py:480-493`) is pure ADP arithmetic. A standalone LB
   module can import these directly. That is the real seam.

5. **The project already has an answer to "a roster slot the model doesn't cover," and it
   works.** K and DST are excluded at the universe level and then explicitly subtracted from
   round-count math, with a note in every affected tool's output. That precedent is
   documented in full below and IDP/LB should follow it — at minimum as the interim state,
   and permanently for any part of the LB problem that doesn't get solved.

**Proposed shape**

- New `ffdraft/idp.py` owning: an `IDP_POSITIONS` constant, an LB-only player table built
  from the defensive columns in `weekly_stats`, LB-specific thresholds, and an LB board.
- New IDP fields on `Scoring` (`config.py:41`), defaulted to `0.0` so every existing league
  is unaffected and `cache_key` (`config.py:127`, which serialises all of `asdict(scoring)`)
  invalidates boards only for leagues that actually set them.
- `FANTASY_POSITIONS` untouched. A separate `IDP_POSITIONS` tuple, so the eight existing
  call sites keep meaning exactly what they mean today.
- Reuse `model._positional_need`, `model.expected_best_at_next_pick`,
  `model.survival_probability_vec`, `board.DraftState` unchanged.
- Until the LB board exists, extend the K/DST exclusion to cover the LB slot — see
  "Interim: extend the K/DST precedent" below.

---

## 1. `model.build_player_table` (`model.py:106`)

The player universe is **not** assembled here. It is assembled one layer down, in
`features.player_season_profiles` (called at `model.py:121`), whose implementation filters at
`features.py:441`:

```
w = w[w["position"].isin(FANTASY_POSITIONS) & (w["season_type"] == "REG")].copy()
```

That is the only universe gate. Everything `build_player_table` does afterwards assumes the
rows it receives are offensive skill players.

What would break if LB rows arrived (i.e. if `FANTASY_POSITIONS` were widened and this filter
let them through):

| Step | Line | Behaviour with LB rows |
|---|---|---|
| `fp_mean` and every weighted aggregate | `model.py:124-135` | All zero — `fantasy_points` has no defensive terms (`features.py:24-43`). Silent. |
| `startable_rate` / `spike_rate` | `features.py:444-446` | `w["position"].map(STARTABLE_THRESHOLD)` → NaN for LB → comparison is False → rate 0.0 for every LB. Silent. |
| Role filter | `model.py:150-156` | Passes on `games_last >= 6` alone, so LBs survive it. `snap_share` comes from `offense_pct` (`features.py:493-495`) and would be ~0 for an LB; `defense_pct` exists in the snap-counts source but is never read. |
| `injury_risk` | `features.py:546-548` | `WORKLOAD_BURDEN` has no LB key → `burden_line` NaN → `burden_ratio` NaN → `recent_burden` NaN → silently `.fillna(0.5)` at `features.py:579`. |
| Injury positional base | `features.py:570-571` | `.fillna(0.20)` — LB silently gets the WR base rate. |
| `age` | `model.py:162`, `features.py:572` | `AGE_CLIFF.get(position, 30)` → LB gets 30/0.05 from `age_adjustment`'s fallbacks (`features.py:591-592`). Silent, and wrong: LB aging is not WR aging. |
| Red zone role | `model.py:187-194`, `features.py:334-354` | Keyed on `rusher_player_id`/`receiver_player_id`, so LBs get `rz_touches=0` via the `fillna(0.0)` at `model.py:189-190`. Harmless but meaningless. |
| Separation merge | `model.py:196-210` | NGS receiving only; LBs get NaN, and `m_separation` gates them out at `model.py:357`. Harmless. |
| Rookie board | `model.py:214-230`, `rookies.py:57,68,80` | Three independent `FANTASY_POSITIONS` filters, so rookie LBs never appear even if the main universe widened. Result: veteran LBs on the board, no rookie LBs — an inconsistency the offense model doesn't have. |

The net: **nothing raises.** Every LB would land on the board with `proj_points` near zero,
sort to the bottom, and be invisible — which is the *benign* failure. The dangerous failure
is `fpa_LB`/`sos_LB_z` reaching `m_schedule` (see §6 below), which changes real players'
scores.

One genuinely reusable property: `build_player_table`'s leak-free season bound
(`model.py:114-118`, `lookback` at `model.py:120`) is position-independent and an LB module
should copy the same discipline verbatim so `mock_draft`/`draft_backtest` stay honest.

---

## 2. `model.project` (`model.py:251`)

Position-**specific**, and would need an LB entry or an explicit exclusion:

| Feature | Line | Why it is position-specific |
|---|---|---|
| `starter_n` | `model.py:263` | Cohort size for the small-sample regression target. `.get(pos, 30)` fallback means LB silently regresses toward a 30-player cohort. |
| `pos_target` | `model.py:264-269` | Derived from `starter_n`; inherits the problem. |
| `m_oline` | `model.py:316-318` | RB gets run block, everyone else pass block. Wrong for LB. |
| `m_volume` | `model.py:321-324` | RB gets rush rate, everyone else neutral pass rate. Wrong for LB. |
| `m_schedule` | `model.py:328-332` | Loops `FANTASY_POSITIONS` looking for `sos_{pos}_z`. LB has none unless §6's fabricated column is created. |
| `m_separation` | `model.py:355-360` | Explicitly WR/TE-gated. Neutral for LB. Correct by construction. |
| `rz_baseline` | `model.py:370-377` | LB cohort has zero red zone touches → `touch_sum == 0` → falls to the hard-coded `0.18` default. Unused downstream because `m_td_luck` pins LBs to 1.0. |
| `pos_rec` (PPR conversion) | `model.py:395` | `{"WR":3.4,"TE":2.8,"RB":2.2,"QB":0.0}`, `.fillna(0.0)` after. LB correctly gets 0. |
| VOR baselines | `model.py:434-437` | Loops `FANTASY_POSITIONS`; `repl.get(pos, 24)`. |
| `qb_boost` | `model.py:447-448` | QB-only by construction. |

Position-**agnostic** and safe to reuse as-is: `bounded()` (`model.py:312-314`), the staleness
discount (`model.py:297-298`), the off-roster discount (`model.py:308-309`), the consistency
blend (`model.py:404-428`) — note `cons_target` at `model.py:418-422` uses a hard-coded
`nlargest(40)` cohort which is a magic number, not a positional constant, and would apply to
LB unchanged — the VOR-to-consistency scaling (`model.py:441-446`), and the NaN guard
(`model.py:451-454`). That last one is the only place in `project` that fails loudly, and it
only catches NaN, not zero.

---

## 3. `model.recommend` (`model.py:496`) and `_positional_need` (`model.py:578`)

`recommend` itself is fully position-agnostic. It maps two dicts onto `avail["position"]`:
`fallback` from `expected_best_at_next_pick` (`model.py:523-524`) and `need` from
`_positional_need` (`model.py:528-529`). Both `.fillna()` — `0.0` and `0.7` respectively —
so an unknown position does not crash; it gets a made-up need multiplier of 0.7.

`_positional_need` is where the roster model actually lives:

- `BACKUP_DECAY` (`model.py:573`) — `{"QB":0.04,"TE":0.28,"RB":0.72,"WR":0.70}`, `.get(pos, 0.5)`
  fallback at `model.py:614`.
- `ROSTER_CAP` (`model.py:575`) — `{"QB":2,"TE":2,"RB":6,"WR":7}`, `.get(pos, 6)` fallback at
  `model.py:604`.
- The loop is `for pos in FANTASY_POSITIONS` (`model.py:601`), so a position outside the tuple
  never gets a `need` entry and falls through to `recommend`'s `0.7`.
- FLEX accounting (`model.py:587-589`) uses `league.flex_eligible` (`config.py:79`), which is
  `("RB","WR","TE")`.
- Superflex QB handling (`model.py:594-598`) and the early-round QB/TE dampeners
  (`model.py:620-624`) are hard-coded position names.

**How a one-slot LB would have to be represented.** The structure is already right for it:
`required = 1`, `have = roster.get("LB", 0)`, `cap = 1` (in a 1-LB league a second LB can
never start, exactly the argument `BACKUP_DECAY["QB"] = 0.04` encodes at `model.py:564-572`),
and a decay at or below the QB value. The comment block at `model.py:562-572` is the
strongest existing evidence for what the LB value should be — it documents a `mock_draft`
finding that an 80% discount was *not* enough to stop the model rostering a second QB, because
QB `draft_score` is structurally larger than other positions'. LB carries the same hazard in
reverse: if LB scoring produces large raw point totals (tackle-heavy scoring often does), an
un-tuned LB would dominate the board. **Any LB integration must be validated with `mock_draft`
the same way `BACKUP_DECAY["QB"]` was**, and that validation is far easier against a separate
LB board than against a mixed one.

The other half is `LeagueSettings.replacement_ranks` (`config.py:95-119`), which builds
`base` from `FANTASY_POSITIONS` (`config.py:103`), distributes FLEX by a hard-coded share dict
(`config.py:106`) and adds `bench_pad` (`config.py:117`). An LB slot needs a `bench_pad`
entry; with a 1-LB league and a cap of 1, the correct pad is arguably 0 — meaning replacement
level is simply `teams * 1`.

---

## 4. `config.py` — every position-keyed constant

`cache_key` (`config.py:124-130`) is the safety net for all of this: it hashes
`asdict(self.scoring)` and the full `starters` dict, so adding `"LB": 1` to `starters` or any
new `Scoring` field automatically invalidates a cached board. `tests/test_config.py:88-97`
already asserts that a changed `starters` dict forces a new board. Note the omissions:
`flex_eligible`, `rounds`, `draft_slot` and `snake` are *not* in the key.

| # | Constant / site | Line | Needs an LB entry? |
|---|---|---|---|
| 1 | `FANTASY_POSITIONS` | `config.py:25` | **No** under the recommended approach — add a separate `IDP_POSITIONS`. |
| 2 | `STARTABLE_THRESHOLD` | `config.py:29` | Yes (LB weekly startable bar). Consumed at `features.py:444` and duplicated at `adp.py:84`. |
| 3 | `SPIKE_THRESHOLD` | `config.py:31` | Yes. Consumed at `features.py:445`. |
| 4 | `AGE_CLIFF` | `config.py:34` | Yes. `.get(position, 30)` fallback at `features.py:591` and `.fillna(30)` at `features.py:572`. |
| 5 | `AGE_DECAY` | `config.py:35` | Yes. `.get(position, 0.05)` at `features.py:592`. |
| 6 | `WORKLOAD_BURDEN` | `config.py:38` | Yes, or an explicit LB opt-out — "touches" is meaningless for an LB and the NaN silently becomes 0.5 at `features.py:579`. |
| 7 | `Scoring` dataclass | `config.py:41-54` | **Yes — blocking.** No defensive fields exist. |
| 8 | `Scoring.preset` | `config.py:56-65` | Only if IDP presets are wanted; defaults of 0.0 make this optional. |
| 9 | `LeagueSettings.starters` default | `config.py:76-78` | Decision point: adding `"LB": 0` keeps every existing league identical while making the slot expressible. |
| 10 | `flex_eligible` | `config.py:79` | No — LB is not flex-eligible in the motivating league. |
| 11 | `replacement_ranks` base loop | `config.py:103` | Yes if LB gets VOR. |
| 12 | `replacement_ranks` FLEX share | `config.py:106` | No. |
| 13 | `replacement_ranks` `bench_pad` | `config.py:117` | Yes (likely 0 for a 1-LB league). |
| 14 | `roster_slots()` | `config.py:121-122` | Already sums all `starters` values — an LB entry is picked up automatically. |
| 15 | `features.fantasy_points` | `features.py:24-43` | **Yes — blocking.** |
| 16 | `injury_risk` positional base | `features.py:570` | Yes; `.fillna(0.20)` otherwise. |
| 17 | `_defense_ratings` universe + pivot | `features.py:206,213-219` | **Must NOT gain LB** — see §6. |
| 18 | `_strength_of_schedule` z-score loop | `features.py:414-417` | **Must NOT gain LB.** |
| 19 | `_player_season_profiles` universe | `features.py:441` | The single real universe gate. |
| 20 | `model.project` `starter_n` | `model.py:263` | Yes if LB is projected in the shared pipeline. |
| 21 | `model.project` `pos_rec` | `model.py:395` | No (`.fillna(0.0)` is correct). |
| 22 | `BACKUP_DECAY` | `model.py:573` | Yes. |
| 23 | `ROSTER_CAP` | `model.py:575` | Yes (1 for a 1-LB league). |
| 24 | `_positional_need` loop | `model.py:601` | Yes if LB participates in need. |
| 25 | `board.SYNTHETIC_ADP_CURVE` | `board.py:149-154` | Yes — otherwise `.get(position,(3.0,1.05))` at `board.py:173` prices an LB like a WR. |
| 26 | `board._ESPN_BASE_SLOTS` | `board.py:450` | Yes — see §5. |
| 27 | `board.espn_league_context` starters seed | `board.py:483` | Yes. |
| 28 | `board.parse_pasted_board` position-strip regex | `board.py:528` | Yes — it strips `(QB\|RB\|WR\|TE\|K\|D/?ST\|DEF)` but not `LB`, so a pasted `"Fred Warner - LB"` keeps the suffix. |
| 29 | `adp._ecr_raw` position filter | `adp.py:29` | See §"Risks" — this filter is *inside the cached builder*. |
| 30 | `adp.season_finish` universe + thresholds | `adp.py:82,84` | Duplicates `FANTASY_POSITIONS` and `STARTABLE_THRESHOLD` inline. |
| 31 | `adp._OPTIMAL_POSITION_CAPS` | `adp.py:529` | Yes if LB enters `draft_backtest`. |
| 32 | `adp._MOCK_BOT_CAPS` | `adp.py:782` | Yes if LB enters `mock_draft`; `.get(p, 99)` at `adp.py:899`. |
| 33 | `adp.mock_draft` `sim_rounds` | `adp.py:856-857` | **Yes — see the precedent section.** |
| 34 | `rookies` universe filters ×3 | `rookies.py:57,68,80` | Yes if rookie LBs are wanted. |
| 35 | `rookies.rookie_consistency_prior` base | `rookies.py:246` | Yes; `.get(position, 0.40)`. |
| 36 | `rookies` `ppg_sd` map | `rookies.py:203` | Yes. |
| 37 | `separation._damp` map | `separation.py:66` | No — receiving-only by construction. |
| 38 | `server.configure_league` starters | `server.py:149` | Yes — needs an `lb:` argument to be settable at all. |
| 39 | `server.plan_my_draft` strategy tilts | `server.py:1038-1041` | Optional; `pool.loc[...] *= mult` at `server.py:1061` only touches listed positions. |

**Count: 22 sites need an LB entry under the "widen `FANTASY_POSITIONS`" approach; two of them
(#17, #18) must be actively *prevented* from gaining one, which means the widening cannot be a
single-constant change even in principle.** Under the standalone-module approach the count
drops to roughly 8 (#7, #9, #14, #25, #26, #27, #33, #38) plus whatever the new module defines
internally — and none of the 8 changes the meaning of an existing offense computation.

---

## 5. `board.espn_league_context` (`board.py:453`)

**Roster slots.** `_ESPN_FLEX_SLOTS` (`board.py:448-449`) and `_ESPN_BASE_SLOTS`
(`board.py:450`) map ESPN's `lineupSlotCounts` ids:

```
_ESPN_FLEX_SLOTS = {"3": ("RB","WR"), "5": ("WR","TE"), "23": ("RB","WR","TE"),
                    "7": ("QB","RB","WR","TE")}
_ESPN_BASE_SLOTS = {"0": "QB", "2": "RB", "4": "WR", "6": "TE", "16": "DST", "17": "K"}
```

The translation loop is `board.py:484-488`. Any slot id in neither dict is **silently
ignored** — no warning, no error. So an ESPN league with an LB slot today produces a
`starters` dict with no LB entry at all.

But note the asymmetry at `board.py:489`:

```
roster_slots = sum(int(v) for v in slot_counts.values())
```

This sums **every** slot count including ones the loop just ignored — and including bench and
IR slots, which are also not in either dict. `roster_slots` becomes `rounds` at
`board.py:509`. So the LB slot *does* inflate the round count while contributing nothing to
`starters`. That is exactly the mismatch that `mock_draft`'s `sim_rounds` subtraction
(`adp.py:856-857`) exists to correct for K and DST, and an LB slot currently escapes it.

**Scoring detection.** `board.py:477-480`:

```
rec_item = next((i for i in settings.get("scoringSettings",{}).get("scoringItems",[])
                 if i.get("statId") == 53), None)
rec_pts = float(rec_item["points"]) if rec_item else 0.0
scoring = "ppr" if rec_pts >= 0.9 else "half_ppr" if rec_pts >= 0.35 else "standard"
```

The entire ESPN scoring translation is **one stat**: receptions (`statId == 53`), bucketed into
three named presets. Nothing else in the payload is read. `grep` for `statId` across `src/`
returns exactly this one line.

**Could the same mechanism read LB scoring?** Structurally yes — `scoringItems` is a flat list
of `{statId, points}` objects, so the pattern generalises to any stat with a known id, and
reading N stats instead of 1 is a small change. But it cannot stay a *preset* lookup: IDP
scoring is not reducible to three named formats the way PPR/half/standard is, so
`Scoring.preset` (`config.py:56-65`) would have to be bypassed in favour of building a
`Scoring` from the raw items.

**What this repo cannot tell us — flagged as unknown, not guessed:**
- The ESPN `statId` values for `def_tackles_solo`, `def_sacks`, `def_interceptions`,
  `def_fumbles_forced`, `def_pass_defended`, `def_tds`, etc. Only `53` appears anywhere in the
  codebase. These require external confirmation — the cheapest reliable route is fetching the
  user's own league with `view=mSettings` and inspecting `scoringSettings.scoringItems`
  directly, since that payload is already being requested at `board.py:469`.
- The ESPN `lineupSlotId` for an LB slot. Not present in `_ESPN_BASE_SLOTS`, not inferable
  from anything in this repo. Same fetch answers it: dump `rosterSettings.lineupSlotCounts`
  keys for the real league and see which unmapped id has count 1.
- Whether ESPN's LB slot admits DE/edge players. Many nominal 3-4 OLBs are labelled `DE` or
  `OLB` in nflverse (see §"Risks" #3). Unknown from code.

---

## 6. `features.py` — offense-only vs. position-neutral

**Inherently offense-only** (would need a defensive analogue built from scratch, not a
parameter change):

| Feature | Line | Why |
|---|---|---|
| `fantasy_points` | `features.py:24-43` | Passing/rushing/receiving columns only. |
| `oline_ratings` / `_oline_ratings` | `features.py:115-167` | Adjusted line yards + pressure allowed, grouped on `posteam`. An LB analogue would group on `defteam`, which is a different function, not a flag. |
| `team_pace_and_split` | `features.py:77-112` | Grouped on `posteam`. For an LB the relevant pace is the *opponent's*, which this frame can supply but the merge at `model.py:178` (`on="team"`) cannot — it joins the player's own team. |
| `player_redzone_role` | `features.py:312-354` | `rusher_player_id` / `receiver_player_id`. No defensive equivalent in `PBP_COLS` (`sources.py:23-31`). |
| `redzone_identity_shift` | `features.py:271-309` | Offensive play-calling. Explicitly informational-only per its docstring (`features.py:285-287`). |
| `team_drive_efficiency` | `features.py:228-268` | `posteam` drives. Could be inverted for a defence, but that is a new function. |
| `separation.py` (whole module) | `separation.py:1-200` | NGS receiving: separation, cushion, YPRR, TPRR. |
| `_player_season_profiles` role columns | `features.py:448,460-463` | `carries`, `targets`, `receptions`, `target_share`. |
| `snap_share` | `features.py:489-495` | Reads `offense_pct` only. **The snap-counts source also carries `defense_pct` and `st_pct` (verified in the cached parquet), and neither is ever read anywhere in `src/`.** This is the cleanest available LB role signal and it is already downloaded. |

**Potentially position-neutral** (reusable with an LB-appropriate input):

| Feature | Line | Note |
|---|---|---|
| `_zscore` | `features.py:62-64` | Pure. |
| `_season_weights` | `features.py:67-72` | Pure; recency weighting is position-blind. |
| `injury_risk` | `features.py:503-584` | Structurally neutral — availability, injury-report frequency, workload. Needs LB entries in `WORKLOAD_BURDEN` (`config.py:38`) and the `base` dict (`features.py:570`), and "touches" would have to be redefined or the burden term disabled for LB. |
| `age_adjustment` | `features.py:587-596` | Neutral given LB entries in `AGE_CLIFF`/`AGE_DECAY`. |
| Floor/ceiling tails | `features.py:466-486` | Pure rank arithmetic on weekly points. |
| `fp_cv` | `features.py:499` | Pure. |
| `defense_ratings` EPA half | `features.py:198-203` | `def_epa_play/pass/rush` per `defteam` is genuinely a defensive-team quality measure and is the *only* existing defensive feature in the codebase. Note it is team-level, not player-level — it cannot distinguish two LBs on the same team. |
| `_defense_ratings` fpa half | `features.py:205-223` | **Must not gain LB** — `fpa_LB` would mean "points allowed to the opponent's linebackers," a category error, and it propagates to `sos_LB_z` (`features.py:414-417`) and then into `m_schedule` (`model.py:328-332`). |
| `strength_of_schedule` | `features.py:357-418` | The *machinery* (recency-weighted opponent profile, divisional double-count) is neutral; only the `fpa_*` inputs are offense-specific. An LB SoS would need a genuinely different input — opponent rushing volume and offensive-line quality faced, not points allowed. |

---

## 7. The K/DST exclusion precedent — the pattern IDP should follow

This is the existing, working answer to "the league has a roster slot the model doesn't
cover." It is implemented at eleven sites in four layers, and it is deliberately *not* a
single flag.

**Layer 1 — the slot exists in settings, so round arithmetic stays correct.**

- `config.py:76-78` — the default `starters` dict includes `"K": 1, "DST": 1` alongside the
  four modelled positions and `FLEX`.
- `server.py:149` — `configure_league` hard-codes `"K": 1, "DST": 1`; there is no argument to
  change them. `docs/GUIDE.md:151` documents this: *"K and DST are fixed at 1 each and not
  modelled."* Code and doc agree.
- `board.py:483` — `espn_league_context` seeds the same dict, and `_ESPN_BASE_SLOTS`
  (`board.py:450`) maps ESPN's `"16"`→DST and `"17"`→K so real ESPN leagues populate them.
- `config.py:121-122` — `roster_slots()` sums all of `starters`, K/DST included.
- `config.py:127-129` — `cache_key` serialises the whole `starters` dict, so K/DST counts
  participate in board identity even though they never affect a projection.

**Layer 2 — the positions are excluded from every player universe.**

- `config.py:25` — `FANTASY_POSITIONS = ("QB","RB","WR","TE")`. K and DST are simply absent.
- `features.py:441` — the profile-layer filter. This is the gate that actually removes them.
- `features.py:206`, `adp.py:29`, `adp.py:82`, `rookies.py:57,68,80` — five more independent
  `.isin(FANTASY_POSITIONS)` filters, each guarding a different derived dataset.
- `config.py:103,118-119` — `replacement_ranks` iterates `FANTASY_POSITIONS`, so K/DST get no
  replacement level and cannot have a VOR.
- `model.py:434-437`, `model.py:601` — same for the VOR baselines and the roster-need loop.

**Layer 3 — round-count math explicitly subtracts the unmodelled slots.**

This is the load-bearing part and the piece most likely to be missed:

- `adp.py:856-857`:
  ```
  sim_rounds = max(1, league.rounds - league.starters.get("K", 0)
                   - league.starters.get("DST", 0))
  ```
  `mock_draft` simulates fewer rounds than the league has, because the bots would otherwise
  spend a K round drafting a real skill player and inflate everyone's totals.
- `adp.py:733-739` — `draft_backtest` only accumulates a round's points when *your* pick
  resolved to a modelled player:
  ```
  # Only count rounds where your own pick was a modelled position --
  # otherwise a K/DST round would compare your None against algo/optimal's
  # real skill-position alternative, inflating their totals unfairly.
  if yp is not None:
  ```
  Note this is an *implicit* test — `actual_points` returns `None` because the kicker isn't in
  `season_finish`, not because anything checked the position.

**Layer 4 — every affected tool says so in its output.**

- `adp.py:764-765` — `draft_backtest`'s `note`: *"K/DST aren't modelled -- those rounds show
  your actual pick only."*
- `adp.py:947-949` — `mock_draft`'s `note`: *"K/DST aren't modelled, so only skill-position
  rounds are simulated."*
- `adp.py:556-558`, `adp.py:811-812`, `server.py:710-712`, `server.py:745-746` — the same
  statement repeated in four docstrings, so it surfaces through MCP tool descriptions.
- `docs/tools.md:188`, `docs/tools.md:207-209`, `docs/GUIDE.md:151` — documented for users.

**Layer 5 — live-draft handling degrades gracefully.**

- `board.py:432-435` — `sync_espn` recognises ESPN's negative `playerId` encoding for team
  defenses (`-(16000 + proTeamId)`) and synthesises a `"<Team> D/ST"` name rather than
  dropping the pick, so the overall pick count stays right.
- `board.py:329-337` — `DraftState.my_roster` builds counts by looking each pick's normalised
  name up in the board's position map. A K/DST pick isn't on the board, so it contributes
  nothing to `roster_counts` — it is *counted* as a pick (`board.py:283-286`) but not as a
  position. `_positional_need` therefore never sees it.
- `adp.py:1070-1071` — `champion_strategies` special-cases `"D/ST" in name → "DST"` before
  falling back to a roster position lookup.
- `board.py:528` — `parse_pasted_board` strips a trailing `(QB|RB|WR|TE|K|D/?ST|DEF)` suffix.

**What LB inherits from this precedent, and where the precedent has a hole.**

An LB slot today gets Layers 2 and 5 for free (it is not in `FANTASY_POSITIONS`; a recorded LB
pick is counted as a pick but not as a position). It gets **none of Layers 1, 3 or 4**:

- `configure_league` has no `lb:` argument (`server.py:149`), so the slot cannot be expressed.
- `espn_league_context` silently drops the LB slot from `starters` (`board.py:484-488`) while
  still counting it in `rounds` (`board.py:489,509`).
- `sim_rounds` (`adp.py:856-857`) subtracts K and DST by name, not "unmodelled slots" in
  general, so `mock_draft` over-simulates by one round for a 1-LB league.
- No note anywhere tells the user LB is unmodelled.

**Interim: extend the K/DST precedent.** Before any LB projection work, the cheapest correct
change is to make the LB slot expressible and subtracted — an `lb:` argument at
`server.py:149`, an `_ESPN_BASE_SLOTS` entry once the slot id is confirmed, and a generalised
`sim_rounds` that subtracts every starter position not in `FANTASY_POSITIONS` rather than
naming K and DST. That is a small, testable change that makes the tool *correct* about the
league it is advising on, independent of whether LB projections ever ship.

---

## Verification: defensive columns in `weekly_stats`

**Confirmed.** Verified by reading the local cached parquet
(`~/.ffdraft/cache/weekly_stats_2021_2025.parquet`, 94,848 rows, 155 columns) directly — no
network call.

**Loading path.** `sources.weekly_stats` (`sources.py:94-123`) tries
`stats_player/stats_player_week_{season}.parquet` then falls back to
`player_stats/player_stats_{season}.parquet` (`sources.py:108-109`), passes each frame through
`_normalise_weekly`, and concatenates. Critically, `pd.read_parquet` at `sources.py:111` is
called **with no `columns=` argument** — unlike `play_by_play`, which restricts to `PBP_COLS`
(`sources.py:23-31,250`). So `weekly_stats` loads every published column.

**`_normalise_weekly` (`sources.py:126-142`) preserves them.** It only ever *adds* columns:
`recent_team` (`:129-130`), `interceptions` from a renamed source (`:132-138`), and
`sacks`/`sack_yards`/`dakota` as NaN if absent (`:139-141`). There is **no column selection,
no drop, and no filter** in the function. Every defensive column survives to the cached
parquet.

**Confirmed present** (all 20 `def_*` columns, all non-null for all 12,455 `position == "LB"`
rows):

```
def_tackles_solo, def_tackles_with_assist, def_tackle_assists, def_tackles_for_loss,
def_tackles_for_loss_yards, def_fumbles_forced, def_sacks, def_sack_yards, def_qb_hits,
def_interceptions, def_interception_yards, def_pass_defended, def_tds, def_fumbles,
def_safeties, def_punt_blocks, def_pat_blocks, def_fg_blocks, def_2pt_atts, def_2pt_made
```

That is enough to score essentially any real IDP format.

**Where the data is lost, and it is not in `sources.py`.** The defensive rows are dropped at
the *feature* layer, by `features.py:441`. `sources.weekly_stats` is entirely position-blind.
The implication for the seam: **an LB module can call `sources.weekly_stats()` directly and
get everything it needs, with zero changes to `sources.py`.** That is the strongest single
argument for the standalone approach — the data seam already exists and is already cached.

**Position labels (verified from the same parquet).** The `position` column carries 25
distinct values, and the LB group is split four ways: `LB` (12,455), `OLB` (1,726), `MLB`
(876), `ILB` (763). A separate `position_group` column groups exactly
`{OLB, LB, MLB, ILB} → "LB"`. Filtering on `position == "LB"` alone silently drops ~3,365 of
~15,820 LB-group player-weeks (21%). **Any LB filter must use `position_group`, not
`position`.** Note the existing code never touches `position_group` — `grep` for it in `src/`
returns nothing.

---

## Risks and unknowns

Flagged as unknown where they are unknown. Nothing below is inferred from a guess.

**Confirmed risks**

1. **Silent zero-scoring.** `features.fantasy_points`'s `g()` helper (`features.py:26`)
   returns `0.0` for any missing column. If defensive columns reach a scoring call that
   doesn't know about them, every LB scores exactly zero and sorts harmlessly to the bottom —
   no exception, no warning. The only loud failure in the pipeline is the NaN guard at
   `model.py:451-454`, and zero is not NaN. Any IDP work needs its own assertion that LB
   points are non-zero.

2. **`fpa_LB` / `sos_LB_z` fabrication.** Covered in §6. If `FANTASY_POSITIONS` is widened,
   `features.py:206→216→386→414` manufactures a "points a defense allows to linebackers"
   column that then multiplies real players' projections at `model.py:328-332`. This is the
   one change that would corrupt *existing* QB/RB/WR/TE output, not just produce bad LB
   output.

3. **Position-label fragmentation.** Verified above: `position` splits LB four ways;
   `position_group` is the correct key; snap counts use a *third* vocabulary (`LB`, `DE`,
   `DT`, `T`, `SS`, … with no `ILB`/`MLB`, verified in the cached snaps parquet), so any
   snap-share join across the two sources needs its own mapping.

4. **The ECR cache is poisoned by a filter inside its own builder.** `adp._ecr_raw`
   (`adp.py:26-33`) applies both a `page_type` filter (`adp.py:28`) and
   `.isin(FANTASY_POSITIONS)` (`adp.py:29`) **inside** the `build()` closure passed to
   `_cached` (`adp.py:35`). The cache key is the bare string `"fp_ecr_redraft"` — it encodes
   nothing about the position set. Verified: the local
   `~/.ffdraft/cache/fp_ecr_redraft.parquet` contains only `{WR, RB, TE, QB}`. **Widening
   `FANTASY_POSITIONS` would not repopulate it; the parquet must be deleted or the cache key
   changed.** This same shape (filter inside a builder, key that doesn't encode the filter) is
   worth auditing wherever else `_cached` is used.

5. **`mock_draft` over-simulates a 1-LB league by one round** (`adp.py:856-857`), and the
   `rounds` value from ESPN already includes the LB slot (`board.py:489,509`). So today the
   tool believes the league has one more skill-position round than it does.

6. **Live-draft UX gap.** `sync_espn` resolves an LB pick to a real player name via the
   `_id_crosswalk` (`board.py:420-421`), which is built from `weekly_rosters` and *does*
   include defensive players. That name then fails `bd.match_player` against the LB-free board
   (`server.py:365-368`) and lands in `unmatched_names` — strictly worse than the D/ST case,
   which at least gets a recognisable synthesised label (`board.py:432-435`).

7. **Magnitude hazard on `draft_score`.** The `BACKUP_DECAY` comment block
   (`model.py:562-572`) documents that QB raw scores are structurally larger than other
   positions' and that an 80% discount was insufficient to stop a bad behaviour. Tackle-heavy
   IDP scoring can produce large raw totals too. Whether an LB would dominate or vanish on a
   shared board is **unknown until measured** — and measuring it requires `mock_draft`, which
   currently cannot represent the LB slot at all (risk #5). The measurement dependency runs
   backwards from the change.

**Genuine unknowns — require confirmation outside this repo**

8. **ESPN defensive `statId` values.** Only `53` appears in `src/` (`board.py:478`). The ids
   for solo tackles, assists, sacks, interceptions, forced fumbles, passes defended and
   defensive TDs are **not knowable from this codebase**. Resolve by fetching the user's league
   with `view=mSettings` (the request already made at `board.py:469`) and reading
   `scoringSettings.scoringItems`.

9. **ESPN LB `lineupSlotId`.** Not in `_ESPN_BASE_SLOTS` (`board.py:450`). Same fetch resolves
   it via the unmapped keys of `rosterSettings.lineupSlotCounts`.

10. **Which nflverse positions ESPN's LB slot accepts.** Whether a nominal `DE` (common for
    3-4 edge rushers) is LB-eligible in this league is unknown. This materially changes the
    candidate pool size.

11. **Whether any IDP consensus ranking exists in the ECR source.** `adp._ecr_raw` filters
    `page_type` to `{redraft-overall, redraft-op}` (`adp.py:28`) before the position filter, so
    the local cache cannot answer whether `db_fpecr.parquet` carries IDP page types upstream.
    **Unknown — requires fetching the unfiltered parquet.** If it does not, LB pricing falls
    back to `board.synthetic_adp` (`board.py:157-175`), whose `SYNTHETIC_ADP_CURVE`
    (`board.py:149-154`) has no LB entry and would use the generic `(3.0, 1.05)` fallback
    (`board.py:173`) — a curve fitted to RB/WR shape, badly wrong for a position typically
    drafted in the final round or two. Without real ADP the whole opportunity-cost layer
    (`model.py:461-536`) is running on a fabricated market.

12. **Whether LB weekly scoring is predictable enough to be worth modelling.** The project's
    own standard for admitting a signal into `draft_score` is a backtest — `matchup_backtest`
    exists precisely because schedule difficulty made WR predictions *worse*
    (`features.py:285-287`), and `redzone_identity_shift` was deliberately left
    informational-only for the same reason. There is currently **no LB analogue of
    `matchup_backtest` and no evidence either way**. The honest position is that LB projections
    should ship informational-only first, exactly as `redzone_identity_shift` did.

---

## Test conventions, and a template for `tests/test_idp.py`

**Observed conventions** (from all seven files in `tests/`):

- No `conftest.py`. No fixtures directory. No `pytest.ini` beyond `pyproject.toml`'s dev
  extra. Tests are collected by name.
- **Strictly offline.** Every test either operates on a hand-built `pd.DataFrame` or
  `monkeypatch`es a `sources.*` loader. `tests/test_board.py:1` states it in the module
  docstring: *"tested offline with synthetic weekly_rosters data."* `tests/test_adp.py:1` and
  `tests/test_redzone_shift_backtest.py:1-3` do the same. No test reads the parquet cache.
- **Module docstring names the subject and the offline strategy** — one or two lines,
  lowercase, colon-separated.
- **Pure functions are tested via their private underscore form**, bypassing the memoised
  public wrapper: `test_features.py:4` imports `_redzone_identity_shift` and
  `_team_drive_efficiency`, not `redzone_identity_shift`. This is deliberate — the public
  forms call `_memo` (`features.py:56-59`) and would leak state between tests.
- **`monkeypatch.setattr(sources, "<loader>", lambda: frame)`** is the mocking idiom
  (`test_board.py:20`). Note it patches the *module attribute*, so the code under test must
  call `sources.foo()` rather than having imported `foo` directly.
- **Class-per-concern, `Test<Concept>`**, methods named as full sentences describing the
  behaviour: `test_backup_quarterback_is_nearly_worthless_in_one_qb`
  (`test_model.py:52`), `test_counts_drive_outcomes_once_per_drive_not_per_play`
  (`test_features.py:18`).
- **Small module-level row builders** rather than fixtures: `_play` (`test_features.py:6-13`),
  `_rz_play` / `_neutral_play` / `_weekly_row` (`test_redzone_shift_backtest.py:8-30`),
  `_hist` (`test_adp.py:8-9`).
- **Regression tests carry the story in a comment**, naming the real players and the real
  failure: `test_board.py:11-14` names Bijan Robinson, Jahmyr Gibbs and De'Von Achane and the
  ~23% figure. `test_model.py:137-141` explains why a three-player board can't exercise format
  conversion.
- **Class docstrings explain non-obvious test design**, e.g. `TestTouchdownLuck`
  (`test_model.py:135-139`) explains why single-player cases are tested inside a small board.
- Assertions are behavioural inequalities (`>`, `<`, ordering) far more often than exact
  values; exact values use `pytest.approx`. `test_features.py` defines a local
  `pytest_approx(x)` helper with `abs=0.5` at its foot (`test_features.py:63-65`) rather than
  importing pytest at module top — an idiosyncrasy, not a rule.
- Ruff config: `line-length = 100`, `E501` ignored, `tests/*` exempt from `E402` only
  (`ruff.toml`).

**Template for `tests/test_idp.py`**

```python
"""IDP/linebacker scoring and roster need: tested offline with synthetic weekly rows.

No network and no cached parquet — every frame here is hand-built, so these tests
run identically on a cold machine. The LB fantasy-point arithmetic is exercised
directly; the shared VOR/need machinery it reuses is already covered in
test_model.py and is not re-tested here.
"""
import pandas as pd
import pytest

from ffdraft.config import LeagueSettings, Scoring
from ffdraft.idp import IDP_POSITIONS, idp_fantasy_points, lb_universe


def _lb_week(player_id, name, season=2024, week=1, **stats):
    """One weekly_stats row shaped like the real nflverse frame.

    Defaults mirror what the source actually publishes: every def_* column is
    present and zero-filled rather than absent, which is why a missing-column
    bug would not surface without building rows this way.
    """
    row = {
        "player_id": player_id, "player_display_name": name,
        "position": "LB", "position_group": "LB", "season": season,
        "week": week, "season_type": "REG", "recent_team": "SF",
        "def_tackles_solo": 0, "def_tackle_assists": 0, "def_sacks": 0.0,
        "def_interceptions": 0, "def_fumbles_forced": 0,
        "def_pass_defended": 0, "def_tds": 0,
    }
    row.update(stats)
    return row


class TestIdpScoring:
    def test_a_tackle_line_scores_the_configured_points(self):
        rows = pd.DataFrame([_lb_week("p1", "Solo Guy", def_tackles_solo=8,
                                      def_tackle_assists=4)])
        sc = Scoring(tackle_solo=1.5, tackle_assist=0.75)
        assert float(idp_fantasy_points(rows, sc).iloc[0]) == pytest.approx(15.0)

    def test_default_scoring_is_zero_so_existing_leagues_are_untouched(self):
        """Every IDP field defaults to 0.0. A league that never configured IDP must
        see byte-identical behaviour, which is what keeps cache_key stable."""
        rows = pd.DataFrame([_lb_week("p1", "X", def_tackles_solo=10, def_sacks=2.0)])
        assert float(idp_fantasy_points(rows, Scoring()).iloc[0]) == 0.0

    def test_a_scored_linebacker_is_never_silently_zero(self):
        """The failure mode this guards: features.fantasy_points' g() helper returns
        0.0 for any absent column, so a renamed source column would score every LB
        at zero with no error. Assert non-zero explicitly."""
        rows = pd.DataFrame([_lb_week("p1", "X", def_tackles_solo=9)])
        assert float(idp_fantasy_points(rows, Scoring(tackle_solo=1.0)).iloc[0]) > 0


class TestLbUniverse:
    def test_position_group_catches_ilb_olb_and_mlb(self):
        """position splits the LB group four ways (LB/OLB/MLB/ILB); filtering on
        position == 'LB' alone drops about 21% of real LB player-weeks."""
        w = pd.DataFrame([
            _lb_week("p1", "Plain LB"),
            {**_lb_week("p2", "Inside"), "position": "ILB"},
            {**_lb_week("p3", "Outside"), "position": "OLB"},
            {**_lb_week("p4", "Middle"), "position": "MLB"},
        ])
        assert len(lb_universe(w)) == 4

    def test_offensive_rows_are_excluded(self):
        w = pd.DataFrame([
            _lb_week("p1", "Real LB"),
            {**_lb_week("p2", "Receiver"), "position": "WR", "position_group": "WR"},
        ])
        assert lb_universe(w)["player_id"].tolist() == ["p1"]

    def test_postseason_is_excluded_like_the_offense_universe(self):
        w = pd.DataFrame([
            _lb_week("p1", "Reg"),
            {**_lb_week("p2", "Post"), "season_type": "POST"},
        ])
        assert lb_universe(w)["player_id"].tolist() == ["p1"]


class TestOneSlotLinebackerNeed:
    """A 1-LB league is structurally the 1-QB argument from model.BACKUP_DECAY:
    the second linebacker can never start, so his bench value must fall below real
    RB/WR depth. The comment at model.py:562-572 records that an 80% discount was
    not steep enough for QB -- LB needs the same mock_draft validation, and these
    tests only pin the invariant, not the tuned constant.
    """

    def test_an_empty_linebacker_slot_is_a_premium(self):
        league = LeagueSettings(starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1,
                                          "FLEX": 1, "LB": 1, "K": 1, "DST": 1})
        assert idp_need(league, {})["LB"] > 1.0

    def test_a_second_linebacker_is_nearly_worthless(self):
        league = LeagueSettings(starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1,
                                          "FLEX": 1, "LB": 1, "K": 1, "DST": 1})
        assert idp_need(league, {"LB": 1})["LB"] < 0.1
```

Two conventions worth carrying deliberately: build rows with **all** `def_*` keys present and
zero-filled (mirroring the real source, so a column-rename bug is catchable), and write at
least one test that asserts a *non-zero* score — the silent-zero failure mode at
`features.py:26` is the one this module is most exposed to.

---

## Where docs and code disagree

Checked; only one discrepancy, and it is minor:

- `docs/GUIDE.md:151` — *"K and DST are fixed at 1 each and not modelled."* Accurate for
  `configure_league` (`server.py:149`, no argument exists) but **not** for
  `espn_league_context` (`board.py:484-488`), which reads real counts from ESPN's
  `lineupSlotCounts` and can therefore produce a league with `K: 0` or `DST: 2`. Both paths
  write into the same `LeagueSettings.starters`. Worth noting because the LB slot would be
  configured through exactly this split path.

Everything else checked — `docs/tools.md:188`, `docs/tools.md:207-209`, and the four K/DST
docstrings at `adp.py:556-558`, `adp.py:811-812`, `server.py:710-712`, `server.py:745-746` —
matches the code.
