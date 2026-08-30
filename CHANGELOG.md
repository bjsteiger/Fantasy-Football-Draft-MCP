# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

**IDP projections are discounted by how much evidence they rest on** ([#33](https://github.com/bjsteiger/Fantasy-Football-Draft-MCP/issues/33))
- A one-season projection is now pulled 0.20 toward the mean of the upper half
  of the board and a two-season projection 0.10, replacing the flat 0.15 for one
  season and nothing for two. Three or more seasons are untouched.
- The reported symptom was a defender with one season and 16 games ranking third
  on the real board, ahead of three five-year veterans including Bobby Wagner.
  Measuring it showed the pool-wide numbers say the opposite: across every
  qualified defender, one-season players are *under*-projected (bias -0.33 and
  -0.57 ppg on two folds). The error is at the top of the board, which is the
  only part anyone drafts — among the twenty highest-ranked players, one-season
  players came in +9.07 ppg over their actual 2025 rate against +1.42 for
  everyone else, and finished 172nd on average against 27th.
- Fitted on three held-out seasons (2023 from 2021-22, 2024 from 2021-23, 2025
  from 2021-24), two of them under this league's own ESPN scoring for that year.
  The adopted pair beats doing nothing on both mean absolute error and rank
  correlation in every fold — MAE 2.581 → 2.535, 0.546 → 0.530, 2.444 → 2.387;
  rank correlation 0.7112 → 0.7188, 0.6868 → 0.6967, 0.7393 → 0.7466 — and each
  value is at or within 0.001 of its own fold-by-fold optimum. About a 2.3%
  error improvement: real, and small.
- What lost, recorded because the alternatives look reasonable: stronger pulls
  (0.35, 0.40) stopped beating doing nothing; a games-based `g/(g+g0)` weight
  won on top-30 error and was worst of everything on pool-wide error, because it
  drags five-season players toward the anchor too; a third tier at 0.05 for
  three-season players moved error by 0.0005 and split the folds, so it was left
  out; and the all-player mean as anchor was better on error but worse on
  ranking in all three folds — the trap the offence model already hit.
- On the live board this moves the one-season defender from 3rd to 6th, behind
  the veterans he was marginally ahead of, and leaves every multi-season
  projection unchanged to the tenth of a point. Replacement level does not move,
  so IDP value over replacement stays comparable with the offence board.

## [1.1.0] — 2026-08-30

### Added

**Individual defensive players (IDP)**
- New `ffdraft/idp.py` and `idp_report`: scoring, projection and ranking for
  leagues with a defensive-player roster slot. Deliberately a separate module
  rather than four new entries in `FANTASY_POSITIONS` — that tuple serves five
  distinct purposes across eight files, and two of them must be *prevented* from
  gaining a defensive position. Widening it would fabricate `fpa_LB`/`sos_LB_z`
  ("points a defence allows to opposing linebackers", a category error) and
  multiply real QB/RB/WR/TE projections by it. See `docs/idp-research.md`.
- Scoring is read from your own ESPN league, via a statId map **derived against
  real player-seasons** rather than taken from a community table — ESPN
  publishes 63 scoring items keyed by number with no names attached, and a wrong
  mapping is silent, producing a plausible total that is simply wrong. Five
  statIds matched every one of 38 cross-matched linebackers. Reconciliation is
  exact: 465.5 computed against ESPN's own 465.5. `docs/idp-scoring-derivation.md`
  records both the method and what did *not* resolve — ESPN and nflverse agree
  on a player's total tackles and disagree on the solo/assisted split by ~10.45
  per season, an unofficial stat neither source can settle.
- The cost of that disagreement is measured, not waved away: reproducing ESPN's
  IDP totals from public data gives 3.5% mean error and 0.97 rank correlation.
  Rankings are reliable; point totals are approximate, and two players within
  ~12 points are not meaningfully separated. The tool says so in its own output.
- Projection is recency-weighted across seasons using the same `RECENCY_WEIGHTS`
  the offence model uses, so a defender and a receiver are projected on
  comparable terms — which matters because value over replacement is the only
  figure comparable across positions, and a weekly score sums starters
  regardless of where they line up. Chosen on evidence: over 536 defenders,
  recency weighting beat last-season-only (MAE 2.54 vs 2.67) and a flat mean
  (2.60), and ranked better (0.712 vs 0.702 and 0.695).
- One-season projections are pulled 0.15 toward the mean of the upper half of
  the board. One season and five are not equal evidence — 2.80 mean absolute
  error and 0.607 rank correlation against 2.30 and 0.854. The 0.15 figure was
  fitted on two independent folds and was the optimum in each. The anchor is
  deliberately the upper-half mean rather than the all-player mean, which is the
  trap the offence model already hit, where a mean dragged down by rotational
  players cut genuine starters by a third.
- **No age curve**, on purpose. Linebacker production is near-flat from 27
  through 32 in this data (12.84 points per game at peak, still 11.45 at 32),
  and what decline is visible is confounded by survivorship — only defenders
  still playing well stay in the league. Inventing a decay constant would
  fabricate precision the data does not support.
- **No per-player IDP draft position**, also on purpose. FantasyPros does
  publish IDP consensus and it is already in the cached parquet, but it sits on
  a different scale from the overall board (the consensus IDP1 is at ECR 2.1,
  meaning "best defender", not "second pick"), and it does not predict this
  market anyway: against 22 real defender picks it correlated 0.30 with actual
  pick. `idp.draft_timing` reports the envelope instead — how many defenders are
  gone by a given pick, from a league's own drafts — because that is the part
  that holds and it is what "can I wait?" actually depends on.

### Fixed

**Board unusable on the declared pandas floor**
- `model.project()` raised `TypeError` on pandas 2.0 and 2.1, both of which
  `pyproject.toml` declares supported (`pandas>=2.0`). `include_groups` only
  exists on `groupby.apply` from pandas 2.2; before that the keyword is handed
  straight to the applied function, which does not accept it. Every
  recommendation path runs through `project()`, so on those versions the tool
  did not degrade — it did not work at all.
- CI could not have caught it, and still cannot from the version matrix alone:
  pandas 3.0 requires Python 3.11, so the 3.10 job resolves pandas 2.3.3 and the
  3.11/3.12 jobs resolve 3.0.5. All three carry `include_groups`, so the matrix
  stays green across every version it actually tests. Only a pinned 2.0.x or
  2.1.x — which the dependency range permits — reaches the broken path.
- Replaced with a plain grouped loop. The lambda never read the grouping column,
  which is the only thing `include_groups=False` suppressed, so the result is
  unchanged on the versions that already worked.
- Verified against the floor rather than assumed: the suite passes on pandas
  2.0.3, 2.1.4 and 3.0.5, and `project()` builds a board on each. The pre-fix
  code raises on both 2.0.3 and 2.1.4.

**Roster slots the model does not cover**
- An ESPN league with a defensive-player slot lost it entirely: the slot matched
  neither the base nor the flex branch and vanished from `starters`, while still
  counting toward `rounds`. Unrecognised *starting* slots are now summed into an
  `IDP` count rather than matched against a guessed table of slot ids — league
  formats vary more than the module can enumerate, and the arithmetic only needs
  to know how many slots the model cannot fill, not what each is called.
- `mock_draft` subtracted kickers and defences by name and so missed IDP
  entirely; `plan_my_draft` subtracted nothing at all and planned sixteen skill
  players for a league with twelve skill slots, returning a roster that cannot
  be fielded. Both now use `LeagueSettings.modellable_rounds()`, so there is one
  place to be right instead of two places disagreeing.
- A drafted defender was invisible in `draft_status`. `my_roster` resolves picks
  against the offence board and silently skips what it cannot find, so the IDP
  slot read as empty however many defenders you had taken. Adds `roster_needs`,
  which shows required against filled per slot.
- Asking the offence board about a defender dead-ended, and misleadingly:
  `player_report` answered "no match for 'Fred Warner'", which says the name is
  wrong rather than that defenders live on another board. `best_available` and
  `separation_report` returned nothing at all. All three now name the problem
  and point to `idp_report`.
- `draft_backtest` returned unscoreable rounds with no points and no
  explanation, which reads as a player who scored nothing all season rather than
  a position the tool does not cover. Rows now carry
  `your_pick_unmodelled_position`, and the note names all three kinds.
- The IDP board ranked players who had stopped playing. C.J. Mosley came out
  first on a 2026 board despite last appearing in 2024 for four games, three
  strong seasons outweighing his absence. Players absent from the most recent
  season are now excluded.
- Two pre-existing `ruff` failures in `adp.py` (`B023`, `I001`). CI installs
  `ruff` unpinned, so these failed on a clean checkout and made a red build
  uninformative.
- CI never ran on pushes to the default branch: the workflow triggered on
  `main` while this repository's default is `master`. Pull requests were always
  checked, so nothing merged unverified, but post-merge verification was silently
  dead — which matters more now that pull requests can merge unattended.


**Team drive efficiency and red zone identity**
- New `features.team_drive_efficiency` (share of a team's drives ending in a
  touchdown/field goal/punt) and `features.redzone_identity_shift` (a team's neutral
  pass rate minus its red zone pass rate), surfaced through `team_context`.
- Both are informational only, like `matchup_z` in `separation_report` — not folded
  into `draft_score`. New `redzone_shift_backtest` tool tested whether blending the
  identity shift into the touchdown-luck signal (`m_td_luck`) improves prediction of
  next-season points; a 2022-2025 run found it makes predictions *worse* for both WR
  (`improvement_corr` -0.006, 300 player-seasons) and TE (-0.053, 117 player-seasons),
  so it stays informational, matching the conclusion `matchup_backtest` already
  reached for schedule difficulty.
- `team_drive_efficiency` needs new play-by-play columns (`drive`,
  `fixed_drive_result`) not present in a `play_by_play` cache built before this
  change — run `refresh_data(force_download=true)` if it comes back empty.

**Touchdown luck**
- New environment multiplier (`m_td_luck`, weight `td_luck` / `td_luck_weight` in
  `model_settings`, default 0.06): a player's red zone touch/touchdown rate, from raw
  play-by-play, regressed toward what his position converts on average — computed
  from the same starter-caliber cohort the baseline projection regresses toward.
  Below 8 red zone touches a rate is treated as noise and sits neutral.
- `player_report` now shows `rz_touches`, `rz_td`, `rz_td_rate`, `rz_baseline_rate`
  alongside the multiplier, and `explain()`'s plain-language summary surfaces it as
  "touchdown regression" whenever it's non-trivial.
- New `features.player_redzone_role` (raw plays → per-player-season red zone
  touches/TDs) and `model.touchdown_luck_multiplier` (the bounded z-score
  adjustment, independently unit-tested).

## [1.0.0] — 2026-08-12

First release. A live fantasy football draft analyst exposed over MCP.

### Added

**Draft recommendations**
- `who_should_i_pick` weighs projected value, week-to-week consistency, open starting
  slots, and the odds each player survives to your next pick.
- Positional opportunity cost rather than raw value: what matters is the marginal gain
  over what a position still offers at your next turn, not who grades highest overall.
- `plan_my_draft` simulates all your picks from your slot under balanced, zero-RB,
  hero-RB or robust-RB strategies.

**Projections**
- Recency-weighted production across five seasons, converted to your exact scoring.
- Offensive line from adjusted line yards and pressure allowed per dropback.
- Neutral-script pace and run/pass split, measured only between 20% and 80% win
  probability so garbage time doesn't distort it.
- Five-year defensive strength by position defended, plus divisional schedule weighting.
- Injury risk from availability history, injury-report frequency and workload burden.
- Positional aging curves.

**Separation and route efficiency**
- NGS tracking separation and cushion, plus estimated YPRR and TPRR, as an open-data
  stand-in for paywalled charting. Man/zone splits are not reproducible.

**Rookies**
- Projected from draft capital, fitted to ten years of first-year outcomes and blended
  with empirical pick bins so the curve can't extrapolate past the data.

**Scoring formats**
- PPR, half PPR, standard, superflex and TE premium. Consensus rankings are converted
  between formats, since only PPR is published upstream.

**Multiple leagues**
- Named leagues with separate boards, replacement levels and in-progress drafts.

**Platform sync**
- Sleeper (public API), ESPN (public leagues, or private with cookies), or paste from
  anywhere.

**Name resolution**
- Aliases, nicknames, suffixes, punctuation, bare surnames, initialisms and typos.
  Ambiguous names name their candidates rather than guessing.

**Analysis**
- `draft_value_history` backtests consensus rank against actual finish across 913
  draftable player-seasons.
- `value_picks`, `separation_report`, `defense_report`, `team_context`, `player_report`,
  `compare_players`, `rookie_report`.

### Notes on accuracy

Several defaults were calibrated against data rather than assumed, and the reasoning is
documented in `docs/methodology.md`:

- Baseline projections regress toward the mean of *starter-caliber* players, not all
  players. Regressing toward a mean that includes third-stringers cut genuine starters
  by roughly a third.
- Consistency is regressed for small samples, so a backup with three good games doesn't
  outrank proven starters on reliability he never demonstrated.
- Rookie curves are capped at each pick bin's observed 75th percentile.
- Format conversion is damped to 0.6, because draft rooms move less than pure points
  arithmetic implies.
