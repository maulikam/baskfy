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
