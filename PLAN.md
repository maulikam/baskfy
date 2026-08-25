# Plan: Tree 3 — Investor loop (Own)

Depth: tree 3   Mode: orchestrated (six real leaves; 1.1.1 is sequential; 1.1.2–1.3.2 after it)
Budget note: machinery exists; the object it operates on does not. Mark-as-invested unblocks the rest.

Branch: `developer`. No web execute / OrderGateway. Track B flags stay false. SC11 no-order tests stay green.

## Contract

Decided BEFORE fan-out. T7.2 (b) stands: the user confirms they invested at the broker; Baskfy records the book. It does not place an order.

### Out of scope (hard)
- `POST /execute`, OrderGateway, `place_order`, `confirm=true` from `apps/web` or `baskfy_api` curated routers.
- Flipping `BASKFY_SUBSCRIPTIONS_ENABLED` / `FEE_COLLECTION_ENABLED` / `PUBLIC_SIGNUP_ENABLED`.
- Kite Connect basket-order handoff (T7.2 option c).
- `frozen/strangle/`. Phase 4 multi-tenant. Weakening no-order tests.

### Interfaces

**Mark-as-invested (1.1.1)**

```
POST /api/v1/cb/investments/mark
  { basket_slug, amount (>0), confirmed: true,
    holdings: [{ symbol, qty (>0), avg_price (>=0) }] (≥1),
    desk_plan_id?: string | null }
  → 201 { id, status: "ACTIVE", basket_slug, batch: { id, kind: "BUY", status: "PLANNED" } }

GET  /api/v1/cb/investments
GET  /api/v1/cb/investments/{id}
GET  /api/v1/cb/fees     # accrued ledger; collected stays false
```

- `confirmed` must be `true` or 400.
- One ACTIVE investment per `(user_id, basket_id)` — second mark is 409 (`stale-data-version`).
- Batch status on this POST is **always `PLANNED`**. Never `EXECUTED` from the web.
- Fee row via `platform_fee("BUY", amount)`, `collected=false`.
- Holdings are what the user declared they bought. Do not invent qty from weights (sizing module refuses unit counts on purpose).

**EXECUTED (1.1.2)**

- Worker `baskfy.cb.sync_batches` reads `desk.rebalance_versions` / `rebalance_orders`.
- Match `cb_order_batch.desk_plan_id` to `rebalance_versions.version_id` (string).
- All planned qty filled → `EXECUTED` + `executed_at`. Some filled → `PARTIAL`. Synthetic `cb-sim-*` never matches → stay `PLANNED`.
- Protocol seam `DeskFillReader` so tests do not need a live desk schema.

**Rebalance notify (1.2.1)**

- Worker `baskfy.cb.rebalance_notify` finds undismissed `REBALANCE_AVAILABLE` whose payload lacks `notified_at`.
- Sends one email via existing `baskfy_api.email` transport (ConsoleTransport in tests).
- Writes `payload.notified_at` (ISO) + `delivery = "email"`. Second run must not send.
- Does not edit `curated_versions.py` publish path (already inserts the pending row).

**SIP (1.2.2)**

```
POST /api/v1/cb/investments/{id}/sip
  { amount (>0), day_of_month: 1–28 }
  → 201 { id, mode: "REMINDER", status: "ACTIVE", next_fire_date }
GET  /api/v1/cb/investments/{id}/sip
```

- `mode` is always `REMINDER`. AUTO refused (existing `assert_reminder_mode`).
- Beat `baskfy.cb.sip_reminders` already persists `SIP_DUE`. This leaf adds the plan writer so Beat has rows, plus a proof test.

**Costs (1.3.1)**

```
GET /api/v1/cb/investments/{id}/costs
  → snapshot + accrued_fees_total + returns_after_fees
```

- Page: `apps/web/src/app/(app)/me/investments/[id]/costs/page.tsx` (legacy `/investments/[id]/costs` via existing redirects).
- No collection. Copy says accrued, not charged.

**Drift (1.3.2)**

```
POST /api/v1/cb/investments/{id}/drift/scan
  { broker_holdings?: [{ symbol, qty }] }  # optional; else GET desk holdings
POST /api/v1/cb/investments/{id}/drift/fix   # rebase ledger; resolve DRIFT action
```

- Uses `detect_drift` / `fix_drift` / `rebase_holdings_after_drift`. No OrderGateway.
- UI on investment detail: Fix now when a DRIFT pending action exists.

### Data ownership (no two leaves share a file)

| Leaf | Owns |
|---|---|
| 1.1.1 mark | `routers/curated_investments.py`, `curated_investments.py` (service), `tests/test_curated_investments.py`, `lib/investments/mark.ts`, `components/cb/mark-invested-form.tsx`, `components/cb/__tests__/mark-invested-form.test.tsx`. May add GET helpers to `lib/investments/fetch.ts`. May pass `basketSlug` through `invest-cta.tsx` + basket page. Mounts router in `app.py`. Extends `DELIBERATE_MUTATING_BASKET_ROUTES` only if a path contains "basket" — **this POST does not**; do not add it there. Extend `test_api_artifacts.py` + exempted-router scan analog in this leaf's tests. |
| 1.1.2 EXECUTED | `worker/tasks/curated_batch_sync.py`, `worker/tests/test_curated_batch_sync.py`, Beat key in `celery_app.py` + task stub in `celery_tasks.py` (append-only). |
| 1.2.1 notify | `email` template fn in `templates.py` (append one function), `worker/tasks/curated_rebalance_notify.py`, `worker/tests/test_curated_rebalance_notify.py`, Beat append. |
| 1.2.2 SIP | `routers/curated_sip.py`, `tests/test_curated_sip_api.py`, `lib/investments/sip.ts`, `components/investments/sip-form.tsx`. Mount in `app.py` (one include line). |
| 1.3.1 costs | `routers/curated_costs.py`, `tests/test_curated_costs.py`, `app/(app)/me/investments/[id]/costs/page.tsx`, `lib/investments/costs.ts`. Mount in `app.py`. |
| 1.3.2 drift | `routers/curated_drift.py`, `tests/test_curated_drift_api.py`, `components/investments/drift-repair.tsx`, `lib/investments/drift.ts`. Mount in `app.py`. |

`app.py` include lines: each leaf appends its own `include_router` after `curated_from_screen`. If two leaves race, driver merges. `openapi.json` / `schema.ts`: driver regenerates after API leaves (`make openapi && make client` or the repo's equivalent). Do not hand-edit `schema.ts`.

### Naming
- Decision tags: `T8.1` … `T8.6` in `docs/DECISIONS-MERGE.md`.
- Gates: `gates/inv-leaf-*.md`, `gates/inv-node-*.md`, root `GATES.md`.
- data-testid: `mark-invested-form`, `mark-invested-submit`, `costs-after-fees`, `drift-repair`, `sip-form`.

### Sequencing
1.1.1 first (creates `cb_investment`). Then 1.1.2, 1.2.*, 1.3.* in parallel (disjoint files).

## Tree

- 1 Investor loop (Own) ............................... GATES.md
  - 1.1 Own the book .................................. gates/inv-node-1.1.md
    - 1.1.1 Mark-as-invested API + UI ................. gates/inv-leaf-1.1.1.md
    - 1.1.2 Desk-fill → EXECUTED ...................... gates/inv-leaf-1.1.2.md
  - 1.2 Hear about it ................................. gates/inv-node-1.2.md
    - 1.2.1 Rebalance notification delivered once ..... gates/inv-leaf-1.2.1.md
    - 1.2.2 SIP reminder persistence .................. gates/inv-leaf-1.2.2.md
  - 1.3 Live with it .................................. gates/inv-node-1.3.md
    - 1.3.1 Costs-and-returns page .................... gates/inv-leaf-1.3.1.md
    - 1.3.2 Drift-repair UI ........................... gates/inv-leaf-1.3.2.md

## Status log

- 2026-08-24 plan written, contract fixed, sequence locked (1.1.1 first)
- 2026-08-24 1.1.1–1.3.2 implemented in-session (mark, EXECUTED sync, notify, SIP, costs, drift)

