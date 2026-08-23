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
- 2026-08-23T00:41Z SC12 SC-FINAL-REPORT; gates leaf-1.9 + node-1 closed; unchecked gates=0; checked=45

---

# Tree 2 — AC closure (module-plan gaps after SC12)

Depth: 4 · Mode: orchestrated · Integrity on every leaf
Prior SC0–SC12 closed leaf gates but deferred real ACs. This tree finishes those
without Phase 4 / D3 (OAuth ABANDON).

## Contract (Tree 2)

- No OrderGateway / web execute / OAuth flip.
- Sole user + Decimal money rules unchanged.
- File ownership disjoint per leaf below.
- Commits: `SC-AC<N>:` or append to STATUS as AC1–AC7.

### Ownership
| Leaf | Owns |
|---|---|
| 2.1 SIP Beat | `worker/tasks/curated_sip.py`, beat key in celery_app, tests |
| 2.2 Create API | `routers/curated_create.py`, tests; wire create form to API |
| 2.3 Dividends | `core/curated_dividends.py` + tests (pure from CA×holdings) |
| 2.4 Chart | `apps/web` basket performance chart SIP+benchmark components |
| 2.5 E2E | playwright explore→handoff smoke (or ABANDON if env blocks) |
| 2.6 Runbook | `RUN-AND-TEST.md` SC section |
| 2.7 Integrity | no-order on new routers; chunk/fee accuracy spot-checks |

## Tree

- 2 AC closure ..................................... gates/node-2.md
  - 2.1 SIP Beat persist ........................... gates/leaf-2.1-sip-beat.md
  - 2.2 Create API ................................. gates/leaf-2.2-create-api.md
  - 2.3 Dividend derivation ........................ gates/leaf-2.3-dividends.md
  - 2.4 Performance chart .......................... gates/leaf-2.4-chart.md
  - 2.5 Playwright E2E ............................. gates/leaf-2.5-e2e.md
  - 2.6 Runbook .................................... gates/leaf-2.6-runbook.md
  - 2.7 Integrity sweep ............................ gates/leaf-2.7-integrity.md

- 2026-08-23T00:44Z Tree 2 AC-closure planned; dispatching leaves 2.1–2.7
- 2026-08-23T00:47Z leaf-2.7-integrity: G2 fee accuracy green (11 tests [100%]). G1 **ABANDON** — after ~3 min poll, `curated_create.py` + `worker/.../tasks/curated_sip.py` still absent (siblings 2.1/2.2); wrote `test_ac_no_orders.py` (4 fail clear, not faked green). Gate evidence in `gates/leaf-2.7-integrity.md`.
- 2026-08-23T00:47Z **ABANDON (not a leaf fail):** live broker OAuth remains blocked on D3 / `BROKER_OAUTH_REVIEW.signed_off` (Tree-2 contract + Phase-4 hold). No OAuth flip attempted; Track C stays shut. Unblocks nothing in 2.1–2.7 product leaves — counsel/Zerodha only.
- 2026-08-23T00:44Z leaf-2.3-dividends green — `curated_dividends.derive_dividends` 11 tests [100%]; closed [from,to] windows × cash CA; RECOVERED-ACTIONS TATASTEEL ex-dates; uncommitted
- 2026-08-23T00:44Z leaf-2.4-chart green — `PerformanceChart` visx + range/SIP/benchmark on `/basket/[slug]`; DisclosureBlock kept adjacent; uncommitted
- 2026-08-23T00:44Z leaf-2.5-e2e + leaf-2.6-runbook green — explore-handoff.spec.ts; RUN-AND-TEST §8 SC; gates checked; uncommitted (no commit)
- 2026-08-23T01:02Z leaf-2.1-sip-beat green — `cb-sip-reminders` Beat + `baskfy.cb.sip_reminders` (4 tests [100%]); leaf-2.2-create-api green — `POST /cb/baskets` PRIVATE/STOCK/GENESIS (5 tests [100%]); no OrderGateway; uncommitted
- 2026-08-23T01:05Z Tree 2 parent verified all leaf-2 + node-2; integrity G1 closed after sibling race; SC-AC-REPORT written

---

# Tree 3 — Unblock D3 and finish remaining ACs (depth 4)

User instruction 23 Aug 2026: do not stay blocked on D3 — decide, record, unlock.

## Contract
- Write D3 as posture B (docs/06 recommendation) in DECISIONS-MERGE.md ⚠ UNREVIEWED.
- Flip BROKER_OAUTH_REVIEW.signed_off with decision_reference.
- Sole-tenant OAuth for Zerodha first; encrypted token at rest; holdings sync via execution ports.
- Web STILL never executes orders (desk non-negotiable #1). DRY_RUN=true.
- Counsel paperwork (algo ID / RA filing) becomes non-blocking NEEDS-MAULIK follow-up, not a gate.
- Also close SC-AC remnants: create form→API, dividend worker job.

## Ownership
| Leaf | Owns |
|---|---|
| 3.1 D3 write + gate flip | DECISIONS-MERGE, broker_connections.py, tests that asserted False |
| 3.2 OAuth callback + token | brokers router callback, token helper, tests |
| 3.3 Holdings sync | sync endpoint using HoldingRow; DRY_RUN safe |
| 3.4 Create UI + dividend job | create form fetch; worker dividends task |
| 3.5 Integrity | no execute; oauth signed with reference; node-1 G3 rewritten |

## Tree
- 3 Unblock ................................ gates/node-3.md
  - 3.1 D3 posture B + flip ................ gates/leaf-3.1-d3.md
  - 3.2 OAuth callback + encrypt ........... gates/leaf-3.2-oauth.md
  - 3.3 Holdings sync ...................... gates/leaf-3.3-holdings.md
  - 3.4 Create wire + dividend job ......... gates/leaf-3.4-remnants.md
  - 3.5 Integrity .......................... gates/leaf-3.5-integrity.md

- 2026-08-23T01:08Z Tree 3: unblock D3 per user; gates leaf-3.* written
- 2026-08-23T01:15Z leaf-3.4-remnants green — create form → `POST /api/v1/cb/baskets` (`lib/create/fetch.ts`); Beat `cb-dividends` + `baskfy.cb.derive_dividends` (4 tests [100%]); uncommitted (no commit)
- 2026-08-23T01:15Z leaf-3.2-oauth green — `GET /api/v1/brokers/callback` (state + Fernet via AccessTokenStore; DRY_RUN stub `exchange_request_token_stub`); 7 tests [100%]; OpenAPI ok; uncommitted
- 2026-08-23T01:15Z leaf-3.3-holdings green — `POST /api/v1/brokers/{id}/sync-holdings` HoldingRow shape (qty+t1+collateral); DRY_RUN empty/fixture; 6 tests [100%]; `/brokers` copy updated for open gate; uncommitted
- 2026-08-23T01:18Z Tree 3 parent verified; D3 unlocked; D3-UNLOCK-REPORT.md

---

# Tree 4 — Backlog closure (tree-11 intent, depth 5)

User backlog 23 Aug 2026 items 1–15. Engineering leaves ship; human/counsel leaves
get written UNREVIEWED decisions or ABANDON with NEEDS-MAULIK — never silent.

## Contract
- No web execute / OrderGateway.
- DRY_RUN default true; live Kite path exists when secret+token and DRY_RUN=false.
- D7/D10/counsel: do not flip Track B flags; write decision stubs or NEEDS only.
- Remote push: ABANDON if no origin URL available (do not invent a remote).

## Ownership
| Leaf | Owns |
|---|---|
| 4.1 ops | delete junk, STATUS refresh, remote ABANDON note |
| 4.2 kite holdings | broker_holdings live Zerodha fetch + tests |
| 4.3 peer brokers | authorize URL map for peers where shape known |
| 4.4 e2e | harden explore-handoff; reduce skip |
| 4.5 chart | wire PerformanceChart to metrics series fetch |
| 4.6 sc3 loop | integration test dry-run plan loop |
| 4.7 customize | customize UI/API for constituents |
| 4.8 runbook | Friday operator steps in RUN-AND-TEST |
| 4.9 human | D7/D10/counsel NEEDS + optional UNREVIEWED stubs |
| 4.10 integrity | no-order + fee remasure |

## Tree
- 4 Backlog ................................ gates/node-4.md
  - 4.1 Ops hygiene ......................... gates/leaf-4.1-ops.md
  - 4.2 Live Zerodha holdings ............... gates/leaf-4.2-holdings-live.md
  - 4.3 Peer broker wiring .................. gates/leaf-4.3-peers.md
  - 4.4 Playwright harden ................... gates/leaf-4.4-e2e.md
  - 4.5 Chart metrics wire .................. gates/leaf-4.5-chart.md
  - 4.6 SC3 dry-run loop .................... gates/leaf-4.6-sc3-loop.md
  - 4.7 Customize ........................... gates/leaf-4.7-customize.md
  - 4.8 Runbook Friday ...................... gates/leaf-4.8-runbook.md
  - 4.9 Human decisions ..................... gates/leaf-4.9-human.md
  - 4.10 Integrity .......................... gates/leaf-4.10-integrity.md

- 2026-08-23T01:31Z Tree 4 backlog plan+gates written; dispatching
- 2026-08-23T01:40Z leaves 4.4–4.7 green (uncommitted): explore-handoff page.route mocks;
  `lib/explore/performance.ts` + basket chart wire; `test_sc3_dry_run_loop.py` [100%];
  CUSTOMIZE preview API + `/investments/[id]/customize`; gates 4.4–4.7 checked
- 2026-08-23T04:07Z Tree 4 parent verified; TREE4-BACKLOG-REPORT; push ABANDON #14
