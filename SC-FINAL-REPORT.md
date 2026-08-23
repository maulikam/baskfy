# SC-FINAL-REPORT — Smallcase layer (SC0–SC12)

**Date:** 2026-08-23  
**HEAD at report:** `b8bdbef` (SC11) + this SC12 commit  
**Mode:** unlazy orchestrated · PLAN.md depth 5 / ~24 leaves (tree-11 intent via integrity siblings)  
**DRY_RUN:** true · **BROKER_OAUTH_REVIEW.signed_off:** False · **OpenAPI order-shaped mutating routes:** clean

## Verdict

Track A smallcase-shaped curated baskets are in the monorepo: schema, catalog math/API, version/plan preview (no execute), accounting/drift pure math, discovery + investor UI with plan handoff only, SIP REMINDER math, create form, engage API, Track B dark, and SC11 integrity tests. The product is **one written D3 answer + deliberate flag flips** away from Phase 4 — not one accidental deploy.

## Module ledger (re-checked)

| Module | Commit | State |
|---|---|---|
| SC0 | `2ddc51e` | ✅ |
| SC1 | `8f0f9be` | ✅ |
| SC2 | `5673e10` | ✅ |
| SC3 | `32f839e` | ✅ |
| SC4 | `4f204cf` | ✅ |
| SC5 | `707e63e` | ✅ |
| SC6 | `02150bc` | ✅ |
| SC7 | `8d2f9d5` | ✅ |
| SC8 | `dfeb9a3` | ✅ |
| SC9 | `120e28f` | ✅ |
| SC10 | `e100e4e` | ✅ |
| SC11 | `b8bdbef` | ✅ |
| SC12 | this commit | ✅ report |

## Re-measured suites (parent, not agent self-report)

| Suite | Count |
|---|---|
| Core curated (versions, hours, plans, accounting, drift, sip, metrics, baskets, scan) | **119 collected** |
| API curated (plans, engage, track_b, tenant, explore perf/orders/catalog, versions service) | **39 collected** |
| Web vitest (nav + explore + investments read-only) | **17 passed** |
| Fee straddle (remeasured) | ₹6666 → base 99.99 / total **117.99**; ₹7000 → base 100.00 / total **118.00** |
| Track C | No web `execute` / `place_order` OpenAPI paths; OAuth gate still False |

## Integrity (SC11)

- **Security:** explore/watchlist no-order tests; curated plan router source scan; PlanHandoff only on CTAs.
- **Tenant:** `curated_tenant.scoped_sole_user_id`; isolation tests (5).
- **Performance:** explore list documents N+1 avoidance + p95 &lt; 1s budget.
- **Memory:** `METRICS_BASKET_CHUNK = 50` batching in metrics service.
- **Accuracy:** fee/XIRR fixtures in `test_curated_accounting.py` (21) assert docs/smallcase/04.

## Honest NOT done (deferred, not silently dropped)

These are product polish / ops wires beyond leaf gates, recorded so the next session does not invent green:

1. Celery Beat job that persists SIP fires into `cb_pending_action` (pure math + dict shape exist).
2. Create API persisting PRIVATE `cb_basket` (UI + weight normalize client-side).
3. Dividend derivation job against corporate-actions fixtures (drift pure helpers exist).
4. Full performance chart SIP/benchmark on `/basket/[slug]`.
5. Playwright E2E journey filter → invest handoff.
6. Runbook section in `RUN-AND-TEST.md` for publish-version / Friday apply (call out in follow-up).
7. Live multi-user broker OAuth — **blocked on D3** (`NEEDS-MAULIK` / DECISIONS-MERGE).

## Track C held

- Web does not execute orders.
- Third-party broker OAuth remains behind `BROKER_OAUTH_REVIEW`.
- Fee collection / public signup / subscriptions flags default **false**.

## Unlazy gates

- Leaf gates under `gates/`: **44 checked**; only `leaf-1.9-verify` was open before this report — closed by this file.
- Root `gates/node-1.md`: OpenAPI clean + OAuth False re-verified at report time.

## How to reverse / flip later

- Track B: set `BASKFY_SUBSCRIPTIONS_ENABLED` (and siblings) only after D3/D7 written answers.
- OAuth: flip `BROKER_OAUTH_REVIEW.signed_off` only with counsel + Zerodha posture in `docs/DECISIONS-MERGE.md`.
- Execution-in-web: explicitly out of SC0–SC12 (PACK.2).
