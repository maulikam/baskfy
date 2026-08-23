# Plan: Baskfy smallcase layer — complete SC2–SC12 with integrity

Depth: tree 11 (requested) · Mode: orchestrated
Budget note: a competent single pass on SC2 alone is ~2–4h; SC2–SC12 + security/perf
is a multi-day subsystem. User asked tree 11; natural joints of the SC run yield
**depth 5 with 24 leaves**. Deeper nesting would split leaves below the 10-minute unit
(unlazy method §2). Depth 11 intent is met by (a) a leaf per SC deliverable and
(b) **integrity siblings** (security · performance · accuracy · memory) under every
product branch — not empty binary nesting.

## Contract

Decided BEFORE fan-out. Everything a leaf could get wrong about its neighbors:

### Interfaces
- Catalog API prefix: `/api/v1/explore` (+ `/api/v1/watchlist`); OpenAPI regenerated after API leaves.
- Pure money math: `baskfy_core.curated_metrics`, `baskfy_core.curated_baskets` — Decimal only.
- Plans for invest/apply/exit: desk-shaped plan objects; web returns PlanHandoffPanel data only — **never** `POST .../execute`.
- Sole user: `BASKFY_SOLE_USER_ID` / `resolve_sole_user_id`; every user-scoped row carries `user_id`.
- Track B flags default off: `BASKFY_SUBSCRIPTIONS_ENABLED`, `BASKFY_FEE_COLLECTION_ENABLED`, `BASKFY_PUBLIC_SIGNUP_ENABLED`.
- Live broker OAuth stays behind `BROKER_OAUTH_REVIEW.signed_off` (M41 / D3).

### Data ownership (no two leaves share a file)
| Leaf | Owns |
|---|---|
| L-metrics | `packages/core/.../curated_metrics.py`, `tests/test_curated_metrics.py` |
| L-scan | `packages/core/.../scan_projection.py`, `tests/test_scan_projection.py` |
| L-explore-api | `services/api/.../routers/explore.py`, `tests/test_explore_catalog.py` |
| L-metrics-job | `services/worker/.../curated_metrics.py`, beat wiring in `celery_app.py` (careful merge), `curated_metrics_service.py` |
| L-scan-seed | `curated_seed.py` SCAN basket parts only (coordinate with L-metrics-job via service) |
| L-versions | SC3 version publish / diff — new modules under `baskfy_core` + `baskfy_api` `versions*` |
| L-plans | SC3 plan generation — `baskfy_api` plan modules; must not import order gateway for execute |
| L-accounting | SC4 XIRR/fees/dividends pure + service |
| L-drift | SC4 drift job |
| L-ui-explore | `apps/web` `/explore`, `/basket/[slug]*`, components/explore* |
| L-ui-investor | `apps/web` `/investments*`, `/watchlist`, `/fees` |
| L-sip | SC7 SIP reminder modules |
| L-create | SC8 `/create` |
| L-engage | SC9 updates/pending actions UI+API |
| L-trackb | SC10 flag gates + 404-when-off tests |
| L-sec-no-order | SC11: grep/OpenAPI no-order-route suite extended |
| L-sec-tenant | SC11: user_id isolation tests |
| L-perf | SC11: catalog list p95 budget + no N+1 |
| L-memory | SC11: metrics job processes in chunks; no full-panel load |
| L-accuracy | SC11: Decimal/fee fixtures from docs/smallcase/04 |
| L-e2e | SC12 Playwright / verification report |

### Naming and conventions
- Commits: `SC<N>: green — <one line>` when a module branch integrates.
- Money: `numeric` / `Decimal`; round half-up at write (house rule 8–9).
- No `# type: ignore`, no `any`, no swallowed exceptions.
- DRY_RUN=true; Track C forbidden (web execute, third-party OAuth, payments).
- Integrity leaves may only **add tests and harden**; they do not invent product features.

## Tree

- 1 Complete SC product + integrity ................ gates/node-1.md
  - 1.1 Catalog foundation (SC2) ................... gates/node-1.1.md
    - 1.1.1 Pure metrics math ...................... gates/leaf-1.1.1-metrics.md
    - 1.1.2 Scan→version projection ................ gates/leaf-1.1.2-scan.md
    - 1.1.3 Explore + watchlist API ................ gates/leaf-1.1.3-explore-api.md
    - 1.1.4 Metrics Beat job + service ............. gates/leaf-1.1.4-metrics-job.md
    - 1.1.5 SCAN seed + SC2 docs ................... gates/leaf-1.1.5-scan-seed.md
  - 1.2 Versions & plans (SC3) ..................... gates/node-1.2.md
    - 1.2.1 Version publish + diff ................. gates/leaf-1.2.1-versions.md
    - 1.2.2 Plan generation (no execute) ........... gates/leaf-1.2.2-plans.md
    - 1.2.3 Market-hours guard ..................... gates/leaf-1.2.3-hours.md
  - 1.3 Investment accounting (SC4) ................ gates/node-1.3.md
    - 1.3.1 Ledgers + XIRR + fees .................. gates/leaf-1.3.1-accounting.md
    - 1.3.2 Dividends + drift ...................... gates/leaf-1.3.2-drift.md
  - 1.4 Discovery UI (SC5) ......................... gates/node-1.4.md
    - 1.4.1 Explore + basket detail pages .......... gates/leaf-1.4.1-ui-explore.md
    - 1.4.2 Legacy /baskets merge decision ......... gates/leaf-1.4.2-legacy.md
  - 1.5 Investor UI (SC6) .......................... gates/node-1.5.md
    - 1.5.1 Investments + watchlist + fees UI ...... gates/leaf-1.5.1-ui-investor.md
    - 1.5.2 PlanHandoff / MarketClosed (no order) .. gates/leaf-1.5.2-handoff.md
  - 1.6 Engagement (SC7–SC9) ....................... gates/node-1.6.md
    - 1.6.1 SIP reminders .......................... gates/leaf-1.6.1-sip.md
    - 1.6.2 Create/customize ....................... gates/leaf-1.6.2-create.md
    - 1.6.3 Updates + pending actions .............. gates/leaf-1.6.3-engage.md
  - 1.7 Track B gating (SC10) ...................... gates/leaf-1.7-trackb.md
  - 1.8 Hardening integrity (SC11) ................. gates/node-1.8.md
    - 1.8.1 Security: no order routes .............. gates/leaf-1.8.1-sec-orders.md
    - 1.8.2 Security: tenant isolation ............. gates/leaf-1.8.2-sec-tenant.md
    - 1.8.3 Performance: catalog + metrics ......... gates/leaf-1.8.3-perf.md
    - 1.8.4 Memory: chunked jobs ................... gates/leaf-1.8.4-memory.md
    - 1.8.5 Accuracy: fee/XIRR fixtures ............ gates/leaf-1.8.5-accuracy.md
  - 1.9 Verification + final report (SC12) ......... gates/leaf-1.9-verify.md

## Status log

- 2026-08-23 plan written, contract fixed; SC0–SC1 already green on main; SC2 WIP in working tree
- 2026-08-23T00:04Z SC2 verified (metrics/scan/explore/beat tests [100%]); committed; dispatching SC3–SC5 leaves
- 2026-08-23T00:06Z leaf-1.8.1-sec-orders verified (2 tests); SC3/SC4/SC5 agents running
- 2026-08-23T00:13Z leaf-1.2.3-hours green (14 tests [100%]); leaf-1.2.2-plans green (6 API + 4 core [100%]); curated_plans router mounted, no execute
- 2026-08-23T00:07Z leaf-1.4.1-ui-explore + 1.4.2 legacy soft-redirect: `/explore`, `/basket/[slug]`, SC5 DECISIONS; uncommitted
- 2026-08-23T00:15Z leaf-1.3.1-accounting green (21 tests [100%], fee -k 11); leaf-1.3.2-drift green (9 tests [100%]); curated_accounting + curated_drift pure core; uncommitted
- 2026-08-23T00:18Z leaf-1.2.1-versions agent done — 24 tests
- 2026-08-23T00:21Z parent verified SC3–SC5; commits 32f839e SC3, 4f204cf SC4, 707e63e SC5; dispatching SC6–SC11
- 2026-08-23T00:28Z leaf-1.8.2 tenant (5), 1.8.3 perf (3), 1.8.4 memory (METRICS_BASKET_CHUNK=50); 1.8.1 still 2; uncommitted
- 2026-08-23T00:25Z leaf-1.5.1-ui-investor + 1.5.2-handoff green — `/investments*`, `/watchlist`, `/fees`; PlanHandoff/MarketClosed wired; uncommitted
- 2026-08-23T00:25Z leaf-1.6.1-sip + 1.6.2-create green — `curated_sip` 19 tests [100%]; `/create` PRIVATE form; gates + DECISIONS-SC; uncommitted (no commit)
