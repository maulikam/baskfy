# Plan: Baskfy ranking engine (explainable presets)

Depth: tree 4   Mode: orchestrated (Phase 1 solo this session; later leaves fan out)
Budget note: full build is a subsystem (registry, screener pipeline, UI, desk parity, research).
This session delivers Phase 1 foundations and leaves 1.2–1.5 gated for follow-on work.

## Contract

Decided BEFORE fan-out. Everything a leaf could get wrong about its neighbors:

- **Interfaces**
  - `baskfy_core.factor_registry.FACTORS` remains the whitelist for `sort_by` SQL keys.
  - `baskfy_core.ranking` owns modes, scopes, preferences, presets, and the desk-score adapter.
  - Desk SCORE/A–F are produced only by `baskfy_core.score.score` (same function the book uses).
    No second formula in the screener.
  - `ScreenDefinition.ranking_mode` ∈ `{single, sequential, composite}`; default `composite`
    preserves today's sum-of-ranks behaviour when factor two/three are on.
  - `ScreenDefinition.ranking_scope` ∈ `{filtered_results, fixed_universe, within_sector}`;
    default `filtered_results` preserves docs/06 today. `within_sector` is schema-reserved until
    sector membership is a first-class column.
  - Stock quality rank and portfolio selection are separate outputs. Selection must not mutate SCORE.
- **Data ownership**
  - Leaf 1.1.1: `factor_registry.py`, `ranking.py`, factor golden / registry tests, marketing families.
  - Leaf 1.1.2: `screen_definition.py` (+ TS mirror), `screener.py` scope/mode ordering.
  - Leaf 1.1.3: desk adapter + explainability schema (A–F breakdown DTO).
  - Leaf 1.2+: research factors (ATR extension, efficiency, …) — new module, not registry churn
    without a decision entry.
- **Naming**
  - New SQL factors use `snake_case` keys ≤ 64 chars, family enums in `FactorFamily`.
  - Preference: `higher` | `lower` | `target_range` | `eligibility`.
  - Presets are named strings (`desk_quality`, `path_quality`, …), versioned later.

## Corrections locked in (Maulik / product brief, 13 Sep 2026)

1. Excess return vs a common index → **filter / column**, not a rank key (same subtraction does not reorder).
2. `% above MA` → **qualification vs extension** separated; extension prefers target ranges.
3. Positive-days % → pair with drawdown / downside vol / jump dependence before promoting alone.
4. Acceleration → non-overlapping log-return rates, not `3m − 12m` on overlapping horizons.
5. High RSI → penalty is tested, not assumed; distinguish persistent strength from a jump.
6. Wasserstein regime → filter / category priority only until a validated strength measure exists.
7. Sortino → different preference; must add information vs Sharpe in a hold-out before promotion.
8. "NSE Momentum" preset → exact NSE methodology + universe, or do not use that label.

## Tree

- 1 Ranking engine
  - 1.1 Phase 1 — expose + semantics .......... `gates/ranking-1.1.md`
    - 1.1.1 Registry: path/trend/participation keys + preference metadata
    - 1.1.2 ScreenDefinition modes/scopes + screener ordering
    - 1.1.3 Desk score adapter (same `score()`) + explainability DTO
  - 1.2 Explainability UI + rank history ...... `gates/ranking-1.2.md`
  - 1.3 Research candidates (ATR, slope, …) .. `gates/ranking-1.3.md`
  - 1.4 Portfolio-aware selection ............. `gates/ranking-1.4.md`
  - 1.5 Presets + validation harness .......... `gates/ranking-1.5.md`

## Status log

- 2026-09-13 plan written; contract fixed; Phase 1 in progress in main session
- 2026-09-13 Phase 1 code landed: registry keys, ranking_mode/scope, desk adapter, tests green
- 2026-09-13 Phase 1.2: `sort_by=desk_score` filters in SQL then re-ranks with `baskfy_core.score.score`;
  A–F explain columns on the result payload; ScreenDefinition rejects desk_score + factor_two
- 2026-09-13 Phase 1.2 UI: explainability UI — peek A–F breakdown; desk columns out of table diet;
  rank history via existing `/instruments/{symbol}/rank-history` when `screenPublicId` is set
- 2026-09-13 Phase 1.3: `baskfy_core.ranking_research` (ATR extension, MA slope, efficiency,
  non-overlapping acceleration, excess-as-filter); keys stay out of FACTORS
- 2026-09-13 Phase 1.4: `baskfy_core.ranking_selection` wraps rank_buffer; SCORE immutable
- 2026-09-13 Phase 1.5: `baskfy_core.ranking_presets` + desk_quality validation vs `score()`
- 2026-09-13 Deploy deferred: gates green locally; AWS SSO valid; uncommitted tree so
  `push-images` would ship HEAD without ranking — see NEEDS-MAULIK §34

---

# Phase 2 — complete the ranking engine (13 Sep 2026, Maulik: "complete whatever all the things required")

Depth: tree 4 · Mode: orchestrated · Driver: main session · Leaves run as fresh subagents that
do **not** commit; the driver re-runs every gate and commits once per verified wave.

## What Phase 1 left (facts found by the Phase-2 survey, not assumptions)

- `ranking_mode`/`ranking_scope` exist in the schema but not the editor; `within_sector` is refused.
- **Bug:** `sequential` orders by `r1, r2, r3`, but each `rN` is a unique `ROW_NUMBER`, so r2/r3
  never decide anything. Sequential must order by the factor *values*.
- **Desk SCORE is not the book's.** The screener scores filtered survivors (percentiles and 1/99
  clips over a different set), caps rows at 4,000 in `instrument_id` order before scoring, and
  gives rejected rows ranks that can land inside `top_n + buffer`. The book scores the whole
  `nse_cash` scan via `momentum_scan.build` → `score.score`.
- `DeskScoringDefaults` copies `kite-momentum-rebalancer/app/config.py` by hand with no test tying them.
- No sector column anywhere; the only point-in-time sector source is narrowest NSE sectoral-index
  membership (`universes.SECTOR_INDEX_SLUGS`, `worker/tasks/swing.py:load_sector_membership`).
- `ranking_research.py`, `ranking_selection.py`, `ranking_presets.py` are called by nothing.
- `index_member_daily` starts 2021-08-02; the local export `research/volume-breakout/aws/*.csv.gz`
  (bars to 2026-09-09, PIT membership, instruments incl. delisted) is the validation dataset.
- NSE methodology (Method_NIFTY_Equity_Indices.pdf, Sep 2026, §16 Nifty200 Momentum 30), verbatim:
  eligible = Nifty 200 at review, ≥1 year listing, available in F&O; MR12 = 12M price return / σp,
  12M return = P(M−1)/P(M−13) − 1 with prices at the last trading day of those months; MR6 likewise
  with M−7; σp = annualised std of lognormal daily returns for 1 year; Z = (MR − μ)/σ over the
  eligible universe for each horizon; weighted Z = 0.5·Z12 + 0.5·Z6; normalised score = 1+Z if
  Z ≥ 0 else (1−Z)^−1; top 30; top 15 compulsorily in, existing beyond rank 45 out; semi-annual
  June/December; weights = FF mcap × score capped at min(5%, 5× FF weight). No winsorisation is
  stated, so none is applied.

## Phase-2 contract (every leaf obeys this; changing it is a driver decision, not a leaf's)

### C1. New stored factors — `factor_daily` columns (migration `0046_ranking_factors`, down `0045_instrument_watch_and_prefs`)

All computed per instrument on **adjusted** close/high/low, point-in-time (only bars ≤ the row's
date), rounded at write time via `precision.COLUMN_PRECISION`, NULL when the window is not full.
"Window N sessions" = the last N bars ending at and including the row's date. Daily simple return
`r_t = c_t/c_{t−1} − 1`; daily log return `l_t = ln(c_t/c_{t−1})`. Calendar-month windows reuse
`windows.py` exactly as `ret_Nm` does.

| key | numeric | definition | preference | weight_family | rankable |
|---|---|---|---|---|---|
| `atr_14` | (18,4) | Wilder ATR(14): TR=max(h−l,|h−c₋₁|,|l−c₋₁|); first = mean of first 14 TR; then (prev·13+TR)/14 | eligibility | risk_execution | no |
| `atr_ext_20` | (10,4) | (close − ma_20) / atr_14 | target_range | trend_structure | yes |
| `ma50_slope_20` | (14,2) | (ma_50_t / ma_50_{t−20} − 1)·100 | higher | trend_structure | yes |
| `eff_ratio_63` | (10,4) | |c_t − c_{t−63}| / Σ_{i=t−62..t} |c_i − c_{i−1}|; NULL if denominator 0 | higher | path_quality | yes |
| `max_dd_6m`, `max_dd_12m` | (14,2) | max over window of (1 − c_i / max_{j≤i in window} c_j)·100, a positive magnitude | lower | path_quality | yes |
| `downside_vol_6m`, `downside_vol_12m` | (18,10) | sqrt(mean over window of min(r_t,0)²)·√252 (same window/returns as `vol_Nm`, divide by N) | lower | risk_execution | yes |
| `sortino_6m`, `sortino_12m` | (14,2) | ret_Nm / (downside_vol_Nm·100); NULL if downside_vol < 0.5e−10 (mirrors sharpe) | higher | risk_execution | yes |
| `underwater_12m` | (7,2) | % of window sessions with c_i < running max of c since window start | lower | path_quality | yes |
| `ret_ex_top3_12m` | (14,2) | (exp(Σ l_t − sum of the 3 largest l_t in the window) − 1)·100 | higher | path_quality | yes |
| `accel_21_105` | (18,10) | mean(l over last 21 sessions) − mean(l over the 105 sessions before those) | higher | momentum | yes |
| `accel_21_105_vs` | (10,4) | accel_21_105 / std(l over the same 126 sessions, ddof=1); NULL if std=0 | higher | momentum | yes |
| `vol_exp_21_126` | (10,4) | mean(vol_day_val, last 21) / mean(vol_day_val, the 126 sessions before those) | higher | participation | yes |
| `vol_persist_20` | smallint | count of last 20 sessions with vol_day_val > mean(vol_day_val over the 126 sessions before the 20) | higher | participation | yes |
| `excess_ret_3m`, `excess_ret_6m`, `excess_ret_12m` | (14,2) | ret_Nm − (NIFTY 500 level_t / level_{window start} − 1)·100 | eligibility | momentum | **no** (filter/column only: a common subtraction cannot reorder) |
| `resid_ret_12m` | (14,2) | ret_12m − beta_12m · NIFTY 50 12M return (%) — beta-adjusted, CAN reorder | higher | momentum | yes |
| `rs_persist_126` | (7,2) | over the last 126 sessions, % of days whose 20-session stock return > NIFTY 500 20-session return | higher | momentum | yes |
| `mom_pctile` | (7,2) | percent rank ×100 of `avg_sharpe_12_6_3_1` among that date's written rows with `universe_mask <> 0` (ties average, NULLs excluded) | eligibility | momentum | no |
| `rank_persist_20` | (7,2) | % of the last 20 dates (incl. today) with `mom_pctile` ≥ 80; NULL if fewer than 20 such dates have a non-NULL `mom_pctile` | higher | momentum | yes |
| `nse_mr6`, `nse_mr12` | (18,10) | NSE momentum ratios for "rebalance month" = the row's month: return P(end M−1)/P(end M−7 or M−13) − 1 over σp = std(l, ddof=1, last 252 sessions ending at end M−1)·√252; month ends are the last trading day in `trading_days` | eligibility | momentum | no |

Benchmarks: `compute_factors(..., benchmark=<NIFTY 50 levels>, market_benchmark=<NIFTY 500 levels>)`
— both frames `(date, level)`; the worker loads `nifty-50` and `nifty-500` from `index_snapshot_daily`.
`mom_pctile` and `rank_persist_20` are cross-sectional/cross-date: pure functions in core
(`factors_ranking.cross_sectional_pctile`, `factors_ranking.rank_persistence`), invoked by the
worker after the day's rows are computed, with prior dates' `mom_pctile` read from the DB.

Registry (`factor_registry.Factor`) gains: `weight_family: WeightFamily` (`momentum | path_quality |
trend_structure | participation | risk_execution`, every factor mapped), `rankable: bool` (default
True), `validation_status: "legacy" | "research" | "validated" | "rejected"` (pre-Phase-2 factors
`legacy`, new ones `research` until leaf F records evidence), `definition: str` (one sentence).
Display `family` stays as it is (docs/08 dropdown grouping). Also new SQL factor
`regime_priority` = CASE regime BULL→2 NEUTRAL→1 BEAR→0 (explicit category priority, higher,
trend_structure, rankable) — the Wasserstein labels get an order, never a distance.

Computed (non-SQL) factors: `desk_score` (read from `desk_score_daily`, C2) and
`nse_momentum_score` (computed by the ranking engine over the NSE-eligible set, C3).

### C2. Desk SCORE, exactly the book's — table `desk_score_daily` (migration `0047_desk_score_daily`, down `0046_ranking_factors`)

Columns: `instrument_id` int FK, `date` date, PK(instrument_id,date); `score` numeric(6,1) NULL
(rejected → NULL); `score_rank` int NULL; `a_trend, b_momentum, c_sharpe, d_consistency,
e_liquidity, f_penalty` numeric(8,4) NULL; `ext_over_20dma` numeric(10,4) NULL; `reject`
varchar(200) NOT NULL default '' ; `score_version` varchar(32) NOT NULL. Model in
`models/ranking.py`. Written nightly by the worker by calling **the book's service**:
`momentum_scan.build(bars, as_of, trading_days, cfg=DESK_CONFIG, carried=…)` → `score.score` over the
whole scan (universe `nse_cash`), exactly as `kite-momentum-rebalancer/app/scan_source.py` does.
`DESK_CONFIG` lives in core (`baskfy_core.desk_config`), and a test in the kite tree asserts every
scoring/scan attribute equals `app/config.py`. `DESK_SCORE_VERSION` constant in core
(`"desk-score-2026.09.13"`) bumps whenever score.py or DESK_CONFIG changes (a test pins a hash of
score.py's source + DESK_CONFIG to the constant). The screener reads this table; it never re-scores.
Rejected rows are never ranked: they appear only when a user explicitly asks to show ineligible rows.

### C3. ScreenDefinition additions (py model is the source; TS zod mirror, JSON schema, corpus regenerate)

```
ranking_terms: list[RankingTerm] = []          # max 8
family_weights: FamilyWeights | None = None    # composite only
missing_data: "penalize" | "neutral" | "exclude" = "penalize"
factor_ranges: list[FactorRange] = []          # max 10, eligibility filters
regime_in: list["BULL","NEUTRAL","BEAR"] | None = None
RankingTerm = {factor: FactorKey (rankable or computed), preference: "higher"|"lower"|"target_range",
               weight: float = 1.0 (0 < w <= 100), target_min: float|None, target_max: float|None}
FactorRange = {enabled: bool = True, factor: FactorKey, min: float|None, max: float|None}
FamilyWeights = {momentum, path_quality, trend_structure, participation, risk_execution: float >= 0 | None}
```
Rules: empty `ranking_terms` ⇒ the legacy path (sort_by/factor_two/three, docs/06 parity, untouched
except the sequential-by-values fix). Non-empty ⇒ `sort_by == ranking_terms[0].factor`, factor_two/three
inactive, `single` ⇒ exactly one term, `target_range` needs ≥1 bound and min ≤ max, weights only
meaningful in composite, `rankable=False` factors refused as terms (e.g. `excess_ret_12m` →
message pointing at factor_ranges). `within_sector` is **allowed**. **Hash stability:** canonical_json
omits each Phase-2 field when it equals its default, so every pre-Phase-2 definition hashes exactly
as before (pinned by a test over the corpus with hashes computed at HEAD 999bf37).
Schema default scope stays `filtered_results` (reference parity + saved screens); the **web editor's
new-screen default is `fixed_universe`** (⚠ UNREVIEWED decision, record in DECISIONS-MERGE).

### C4. Ranking engine (pure, `baskfy_core.ranking_engine`, pandas)

Input: a frame of the selected universe for `as_of` (post-bucket, **pre-filter**) with a boolean
`passes_filters` (all filter + risk + factor_ranges + regime clauses), `sector` (narrowest PIT
sectoral index slug or NULL→"unclassified"), every term's raw value, desk_score_daily columns, NSE
inputs (`nse_mr6`, `nse_mr12`, nifty-200 PIT membership flag, F&O flag). Steps:
1. Computed factors: `desk_score` from the joined table; `nse_momentum_score` per C1/NSE text over
   rows eligible = in Nifty 200 on as_of ∧ F&O ∧ both MR non-NULL (population std ddof=0 — the PDF
   says "std. deviation" without a sample qualifier; recorded), NULL for everyone else.
2. Scope set: `fixed_universe` = all rows; `filtered_results` = rows with passes_filters;
   `within_sector` = all rows grouped by sector.
3. Transform per term within the scope set (per group for within_sector): higher → percentile
   (average-tie rank −1)/(n−1) ascending in value (n=1 ⇒ 1.0); lower → same on −value;
   target_range → distance = 0 inside [min,max] else gap to nearest bound, score = 1 − percentile of
   distance (all-zero distances ⇒ 1.0). Missing raw: penalize ⇒ 0.0, neutral ⇒ 0.5, exclude ⇒ row
   dropped from output.
4. composite: effective weight = family share × (term weight / Σ weights of terms in that family);
   family share = family_weights[f] (None or missing ⇒ equal share across families present),
   normalised to sum 1. `composite_score` = 100·Σ eff_w·score, 2 dp. Order: composite desc, then
   first term's raw value by its preference, then instrument_id.
   sequential: order by each term's raw value by preference (target_range by distance asc),
   NULLs last, then instrument_id. single: the one term, same rule.
5. Output = rows with passes_filters only (and not excluded), ranked 1..n, columns
   `composite_score` (composite only), `term_score__<key>`, `term_contrib__<key>`, plus the frame.
6. `explain(frame_row, context) -> RankExplanation` (dataclass, JSON-able): total, per-term
   {factor,label,weight_family,preference,raw,transformed,effective_weight,contribution,missing},
   positives (transformed ≥ 0.8 → "Top 20% on <label> within <scope>"; desk A–E at ≥ 80% of their max),
   deductions (transformed ≤ 0.2, outside target range, desk F < 0, reject tokens), eligibility
   {passed, failures:[{filter, detail}]} (per-clause booleans come from the frame as
   `fail__<clause>` columns), data_quality {missing_factors, insufficient_history, stale_price
   (last bar date < as_of), recent_corporate_action (adj_factor ≠ 1 within 252 sessions)},
   desk {score, rank, A–F, reject, eligible} | None, provenance {universe, as_of, data_version,
   ranking_engine_version, desk_score_version, nse_momentum_version, scope, mode}.
Versions: `RANKING_ENGINE_VERSION = "ranking-2.0.0"`, `NSE_MOMENTUM_VERSION =
"nifty200-momentum30-2026.09"`.

### C5. Portfolio selection (pure, `baskfy_core.ranking_selection`)

`select_portfolio(candidates, holdings, constraints, returns=None) -> SelectionResult`.
Candidate {instrument_id, symbol, quality_rank, score, sector, adv_value_inr (median_vol_12m), close_raw}.
Holding {instrument_id, symbol, quantity, sector}. Constraints {max_names=15, entry_rank=15,
retention_rank=60 (was 30; C8 grid, `docs/ranking/VALIDATION.md` §6), max_per_sector=None, capital_inr=None, max_adv_participation_pct=1.0,
turnover_budget_names=None, max_correlation=None, correlation_window=126}. Rules in order: holdings
ranked ≤ retention_rank are kept ("hold"), others "exit" (reason RANK_OUTSIDE_RETENTION /
NOT_IN_RESULTS); then candidates in quality_rank order with rank ≤ entry_rank are "enter" unless
a cap refuses them ("skip" with reason SECTOR_CAP | CAPACITY | CORRELATION | TURNOVER_BUDGET |
FULL); proposed value = capital/max_names (equal weight), qty = floor(value/close_raw),
adv participation = value/adv·100. Scores and ranks are never modified (asserted). Informational
only: no order, no plan_id, no route to execution.

### C6. API (services/api)

- Every screen payload gains `provenance` {universe, universe_label, as_of, data_version,
  ranking_engine_version, desk_score_version, scope, mode}; contract tests updated deliberately.
- `POST /api/v1/screens/explain` {definition, symbol, as_of?, data_version?} → RankExplanation.
- `POST /api/v1/screens/selection` {definition, as_of?, portfolio_id? | holdings?, constraints} → SelectionResult (owner-checked portfolio).
- `GET /api/v1/meta/ranking-presets` → [{key,label,description,status,patch}].
- `FactorOut` gains `weight_family, rankable, validation_status, definition`.
- `make client` regenerates openapi + TS; `generate:check` clean.

### C7. Presets (`ranking_presets`), all expressed as ScreenDefinition patches
`desk_quality` (single desk_score) · `path_quality` (composite: pos_days_6m, max_dd_12m, downside_vol_12m,
ret_ex_top3_12m) · `trend_structure` (composite: ma_stack_score, ma50_slope_20, atr_ext_20 target
[0,3]) · `participation` (composite: vol_exp_21_126, vol_persist_20) · `leadership` (composite:
rank_persist_20, rs_persist_126, avg_sharpe_12_6_3_1) · `nse_momentum` (single nse_momentum_score,
index nifty-200, label "NIFTY200 Momentum 30 score (NSE methodology)" — score and eligibility only;
FF-mcap weights, caps and the 15/45 buffer are index construction, stated in the description) ·
`desk_sequential` (sequential: desk_score → atr_ext_20 lower → median_vol_12m higher). Status per
preset comes from leaf F's evidence.

### C8. Validation (leaf F)
Core `baskfy_core.ranking_validation` (pure) + runner `research/ranking-validation/` over the
local AWS export. Base model vs base + one factor, each candidate; monthly rebalance, signal at
close, fill at next session open, 25 bps a side, top 20 with entry/retention buffer, PIT universe
= all EQ names with bars whose trailing 63-session median traded value ≥ ₹5 cr (PIT from bars;
survivorship-free because delisted instruments are in the export), 2013-01 → 2026-08, halves
IS < 2020-01-01 ≤ OOS. Metrics: CAGR net, max DD, annual turnover, mean max-sector weight
(sector = narrowest sectoral index where PIT membership exists, else unclassified), per-year
returns, IS/OOS. Extra studies: Sortino vs Sharpe, RSI-penalty, regime filter, entry/retention grid.
Output `docs/ranking/VALIDATION.md` + `validation_status` updates + preset statuses + the selection
defaults for entry/retention (from the grid's best OOS-stable cell).

## Phase-2 tree and waves

- 2 Ranking engine, complete ................................ `gates/ranking-2.md`
  - 2.A Stored factors (core factors, registry, model, 0046) . `gates/ranking-2.A-factors.md` — wave 1
  - 2.B Desk SCORE service + table (0047) ..................... `gates/ranking-2.B-desk-score.md` — wave 1
  - 2.E Portfolio selection core ............................. `gates/ranking-2.E-selection.md` — wave 1
  - 2.C Worker: nightly wiring, rank persistence, backfill .... `gates/ranking-2.C-worker.md` — wave 2
  - 2.D Ranking engine + definition + screener SQL + presets . `gates/ranking-2.D-engine.md` — wave 2
  - 2.F Validation harness + evidence ....................... `gates/ranking-2.F-validation.md` — wave 3
  - 2.G API ................................................. `gates/ranking-2.G-api.md` — wave 3
  - 2.H Web ................................................. `gates/ranking-2.H-web.md` — wave 4
  - 2.I Integration, docs, deploy, box backfill ............. `gates/ranking-2.md` (root) — wave 5

File ownership: A owns `factors.py`, new `factors_ranking.py`, `precision.py`, `models/facts.py`,
`factor_registry.py`, `ranking_research.py`, migration 0046, factor tests, docs/04. B owns
`desk_config.py`, `desk_score_service.py`, `models/ranking.py`, migration 0047, kite-tree config test.
E owns `ranking_selection.py` + its tests. C owns `services/worker/**`. D owns `screen_definition*.py`,
`ranking.py`, `ranking_engine.py`, `nse_momentum.py`, `ranking_presets.py`, `screener.py`, api-client
screen-definition files + corpus. F owns `ranking_validation.py`, `research/ranking-validation/`,
`docs/ranking/VALIDATION.md` and may edit only `validation_status` values in the registry and preset
statuses. G owns `services/api/**` + `packages/api-client/openapi.json` + `generated/`. H owns
`apps/web/**`. Shared doc `docs/DECISIONS-MERGE.md`: leaves append under their own heading only.

## Phase 2 — brief coverage

Root gate G4 (`gates/ranking-2.md`), written 2026-09-14 against the working tree on `developer`.
Path keys: `core/` = `decile-blueprint/packages/core/src/baskfy_core/`, `core-t/` =
`decile-blueprint/packages/core/tests/`, `api/` = `decile-blueprint/services/api/src/baskfy_api/`,
`api-t/` = `decile-blueprint/services/api/tests/`, `web/` = `decile-blueprint/apps/web/src/`,
`rv/` = `research/ranking-validation/`, `kite-t/` = `kite-momentum-rebalancer/tests/`. Every path
and symbol below was grepped before it was written. Evidence is the gate EVIDENCE line, a test, or
a VALIDATION.md section. "Partial" says what is missing.

| # | Brief item | Where | Evidence | Status |
|---|---|---|---|---|
| 1 | Correction 1: excess return is a filter/column, not a rank key | `core/factor_registry.py` `excess_ret_*` `rankable=False`; `core/screen_definition.py` `_rankable_factor`, `NON_RANKABLE_FACTORS`, `FactorRange`; `api-client/src/screen-definition.ts` `NON_RANKABLE_FACTORS` (`RankingTermSchema`); `web/components/screens/ranking-section.tsx` `RankingSection` (combobox uses `rankableFactors`) | 2.A G5; `core-t/test_factor_registry_c1.py::test_the_filter_only_keys_are_not_rankable`; `core-t/test_screen_definition_parity.py::test_every_non_rankable_factor_is_refused_as_a_ranking_term`, `test_the_zod_mirror_refuses_the_same_non_rankable_factors`; corpus cases `ranking-terms-non-rankable-*`; 2.H G1, G2 | **partial**. A `rankable=False` ranking term is refused server-side (Pydantic, plain message pointing to `factor_ranges`) and in the Zod mirror, in any term position. Still open: a definition without terms can put a non-rankable key in `sort_by` (or `factor_two`/`factor_three`) on the legacy SQL path |
| 2 | Correction 2: qualification (MA filter) kept apart from extension (target range) | `core/factors_ranking.py` `_with_atr` (`atr_ext_20`, preference `target_range`); `core/ranking_engine.py` `target_distance`, `target_score`; MA qualification stays the `moving_average` filter | 2.A G1; `core-t/test_ranking_engine.py::test_target_range_inside_and_outside`, `test_target_range_one_bound`; 2.D G3 | met |
| 3 | Correction 3: positive-days % paired with drawdown, downside vol and jump dependence | Pairing factors in `core/factors_ranking.py` `_with_path_statistics` (`max_dd_*`, `underwater_12m`), `_with_downside_volatility`, `_return_ex_top_days` (`ret_ex_top3_12m`); `core/ranking_presets.py` `PRESET_SPECS["path_quality"]` (C7 composite: `pos_days_6m`, `max_dd_12m`, `downside_vol_12m`, `ret_ex_top3_12m`) | 2.A G1; 2.D G7; `core-t/test_ranking_presets.py::test_every_preset_validates_as_a_patch_over_the_default_definition`; VALIDATION.md §7 (`path_quality` `research`: three of its terms rejected) | met. The pairing is the C7 composite. `pos_days_6m` is not promoted on its own |
| 4 | Correction 4: acceleration from non-overlapping log-return rates | `core/factors_ranking.py` `_with_acceleration` (21 sessions vs the 105 sessions before them) | `core-t/test_factors_ranking.py::TestHandComputed::test_acceleration_of_two_constant_growth_rates`; 2.A G1 | met |
| 5 | Correction 5: RSI penalty tested, not assumed; persistent strength told apart from a jump | `rv/run_validation.py` `rsi_penalised`; `core/factors_ranking.py` `rank_persistence`, `_return_ex_top_days` | VALIDATION.md §5 studies (`base+rsi_penalty` +5.23 pts, rule pass); 2.F G2; `rank_persist_20` validated | met |
| 6 | Correction 6: Wasserstein regime used only as a filter or category priority | `core/factor_registry.py` `regime_priority` (CASE order, no distance); `core/screen_definition.py` `ScreenDefinition.regime_in` | 2.A G5; 2.H G2; VALIDATION.md §5 (`regime_priority` rejected; both `regime_in` studies fail) | met |
| 7 | Correction 7: Sortino must beat Sharpe in a hold-out before promotion | `core/factors_ranking.py` `_with_sortino`; `rv/run_validation.py` model `sortino_for_sharpe` | VALIDATION.md §5 (`sortino_for_sharpe` −2.54 pts, fail; `sortino_6m`/`_12m` rejected); 2.F G4, G5 | met |
| 8 | NSE Momentum: exact methodology and universe, or no label | `core/nse_momentum.py` `anchors_for`, `momentum_ratios`, `eligibility`, `scores`; `core/ranking_engine.py` `add_nse_momentum_score`; `core/ranking_presets.py` `PRESET_SPECS["nse_momentum"]` (single `nse_momentum_score`, index `nifty-200`, label `NSE_MOMENTUM_LABEL`); `api/routers/meta.py` `get_ranking_presets` (`label`) | 2.D G4, G7; `core-t/test_nse_momentum.py`; `core-t/test_ranking_presets.py::test_nse_momentum_is_offered_with_c7s_universe_and_label`; `api-t/test_api_meta_ranking.py::TestRankingPresets::test_nse_momentum_carries_c7s_label_and_universe` | met. The preset is served with C7's label; its description states that FF-mcap weights, caps and the 15/45 buffer are not applied. Status `research`: VALIDATION.md §3.3 marks the score not testable because Nifty 200 / F&O membership starts only at 2021-08 |
| 9 | 8 new factors (C1: ATR extension, MA50 slope, efficiency, drawdown/underwater, downside vol/Sortino, return ex-top-3, residual return, RS/rank persistence) | `core/factors_ranking.py` `with_ranking_series`, `cross_sectional_pctile`, `rank_persistence`; `core/factor_registry.py` `Factor`; migration `0046_ranking_factors` | 2.A G1 (150 passed), G3, G4, G5; 2.C G1, G2 | met. Stored and registered. C8 verdicts are 1 validated, 17 rejected, and `rs_persist_126` not testable (row 20) |
| 10 | Volume baseline (recent window vs a non-overlapping 126-session baseline) | `core/factors_ranking.py` `_with_participation` (`vol_exp_21_126`, `vol_persist_20`) | `core-t/test_factors_ranking.py::TestEveryColumnAgainstTheDefinition`; 2.A G1 | met |
| 11 | Acceleration, raw and vol-scaled | `core/factors_ranking.py` `_with_acceleration` (`accel_21_105`, `accel_21_105_vs`) | `core-t/test_factors_ranking.py::TestHandComputed::test_acceleration_scaled_is_null_when_the_log_returns_do_not_vary`; 2.A G1 | met |
| 12 | 3 modes (single, sequential, composite) | `core/ranking.py` `RankingMode`; `core/ranking_engine.py` `rank_frame`; `core/screener.py` legacy sequential ordered by values | `core-t/test_ranking_engine.py::test_sequential_differs_from_composite`; `core-t/test_ranking_semantics.py::test_sequential_orders_by_factor_values_then_instrument_id`; 2.D G3, G6; 2.G G1 | met |
| 13 | 4 preferences (higher, lower, target_range, eligibility) | `core/ranking.py` `FactorPreference`; `core/ranking_engine.py` `transform`; eligibility goes through `FactorRange` | `core-t/test_factor_registry_c1.py::test_every_c1_key_carries_the_c1_preference_family_and_rankable_flag`; 2.D G3 | met. Eligibility-only factors are refused as ranking terms server-side and in the Zod mirror (row 1) |
| 14 | 3 scopes + default + provenance | `core/ranking.py` `RankingScope`; `core/ranking_engine.py` `_groups`, `Provenance`; `core/screen_definition.py` default `filtered_results`; `web/lib/screens/ranking.ts` `NEW_SCREEN_RANKING_SCOPE` = `fixed_universe`; `api/schemas.py` `ScreenProvenanceOut`; `web/components/screens/provenance-header.tsx` `ProvenanceHeader` | `core-t/test_ranking_engine.py::test_filtered_results_ranks_among_the_survivors`, `test_within_sector_groups_and_the_unclassified_bucket`; `api-t/test_api_run.py::test_it_carries_provenance`; 2.G G2; 2.H G3, G7 | met |
| 15 | Stock quality kept apart from portfolio selection | `core/ranking_selection.py` `select_portfolio`; `api/routers/screens.py` `select_over_screen`; `web/components/screens/portfolio-fit.tsx` `PortfolioFit` | `core-t/test_ranking_selection.py::test_scores_and_ranks_are_unchanged_by_selection_immutable`; 2.E G2, G3; 2.G G3, G4; 2.H G5 | met |
| 16 | Entry and retention taken from validation | `core/ranking_selection.py` `SelectionConstraints` (`entry_rank=15`, `retention_rank=60`) | VALIDATION.md §6 (best OOS-stable cell 15/60); 2.F G5 | met. Caveat from VALIDATION.md §3.8: the grid used a 20-name book |
| 17 | Explainability (7 fields: terms, positives, deductions, eligibility, data quality, desk, provenance) | `core/ranking_engine.py` `explain`, `RankExplanation`, `DeskComponent`; `api/routers/screens.py` `explain_screen_row`; `web/components/screens/rank-explanation.tsx` `RankExplanation` | 2.D G5; `core-t/test_ranking_engine.py::test_explain_a_desk_rejected_row`, `test_explain_a_missing_data_row`; `api-t/test_api_screens_explain_selection.py::test_explain_desk_block_carries_the_stored_inputs_behind_each_grade`; 2.H G4 | **partial**. All 7 fields are present. For desk A–F raw inputs, only `ext_over_20dma` is stored, so grades B–E show no stored inputs |
| 18 | Same desk service (the book's `score()`, no second formula) | `core/desk_score_service.py` `score_day`, `DESK_SCORE_VERSION`; `core/desk_config.py` `DeskConfig`; `decile-blueprint/services/worker/src/baskfy_worker/orchestrator.py` `run_compute_desk_score_step` | `core-t/test_desk_score_service.py::test_score_day_equals_the_books_path_row_for_row`; `kite-t/test_desk_score_parity.py::test_score_day_is_the_desks_score_row_for_row`; `kite-t/test_desk_config_parity.py`; 2.B G1–G3; 2.C G3 | met |
| 19 | Families + family weights | `core/factor_registry.py` `WeightFamily`; `core/screen_definition.py` `FamilyWeights`; `core/ranking_engine.py` `family_shares`, `effective_weights`; `web/components/screens/ranking-section.tsx` (`family_weights`) | `core-t/test_ranking_engine.py::test_three_correlated_momentum_terms_split_momentums_share`, `test_explicit_family_weights_are_scale_free`; 2.A G5; 2.D G3; 2.H G1 | met |
| 20 | Ablation protocol (base vs base + one factor, costs, buffer, IS/OOS, promotion rule) | `core/ranking_validation.py` `simulate_monthly_rebalance`, `summary_row`; `rv/run_validation.py` `main`; `rv/render_validation.py` | 2.F G1–G5; `rv/test_runner_uses_core.py`; VALIDATION.md §1–§5 | **partial**. The window starts 2018-03, not C8's 2013-01. `rs_persist_126` is not testable because the export has no NIFTY 500 levels, and `nse_momentum_score` is not testable because membership starts at 2021-08. Mean max-sector weight is empty because the export has no sector membership (VALIDATION.md §3.2–§3.4) |
| 21 | Implementation order 1–5 | This file: "Tree" (Phase 1, 1.1–1.5) and "Phase-2 tree and waves" (waves 1–5) | Waves 1–4 committed (9fd9f08, 4033355, ef78783, 403e6fa, f52df2c). `gates/ranking-2.md` G1–G3, G5, G6 open | **partial**. Wave 5 (root) is not done. 2.C G5 has one red that already failed on HEAD: the `test_swing_scan_now` login race, awaiting Maulik. 2.G G5 is pending, and the staging deploy and box backfill (G6) are not done |

Totals: 21 rows, 17 met, 4 partial, 0 not done (rows 3 and 8 moved to met, and row 1 narrowed, on 2026-09-14).

## Phase-2 status log
- 2026-09-13 20:40 survey done (3 explorers + NSE PDF); contract C1–C8 written; wave 1 dispatching
- 2026-09-13 20:55 wave 1 dispatched (2.A factors, 2.B desk score, 2.E selection); 2.D started early on its independent parts (definition, engine, NSE) — screener/ranking.py/presets held until wave 1 lands
- 2026-09-13 2.E returned: 59 passed, own-file mypy clean (re-run by driver); G4 whole-repo mypy pending wave-1 WIP. Note: correlation cap is on signed corr (hedges allowed) — contract C5 amended by this line
