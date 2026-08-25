# Plan: Tree 7 — Product can complete (or deliberately hand off) an investment

Depth: tree 7   Mode: orchestrated
Budget note: months of unfinished product surface; this tree sequences the load-bearing gaps.
Branch: `developer` (tracks `origin/developer`). Remote exists; agent SSH may need Maulik's key for push.

## Contract

Decided BEFORE fan-out. Sequence from Maulik's brief (highest leverage first).

### Out of scope (hard)
- Live order placement from the web (`POST /execute`, OrderGateway). Track C / SC11 stay.
- Phase-4 multi-tenant. D3 counsel answers. D7 pricing amounts.
- Touching `frozen/strangle/`. Weakening no-order-route tests.

### Sequencing (do not reorder without recording)
1. **Populate `cb_metrics`** — catalog is empty without it.
2. **Decide investment-creation contract** — six subsystems blocked on it.
3. **Manager page + costs-and-returns** — data exists; SEBI furniture.
4. **Rebalance notification** — rebalance nobody hears about does not happen.

### Interfaces
- **Metrics**: `baskfy_api.curated_metrics_service.compute_all_metrics` → upsert `cb_metrics`.
  Beat task `baskfy.cb.compute_metrics`. Explore cards join latest metrics; null = no row.
- **Investment creation (decision only in leaf 7.2)**: choose one of
  (a) desk creates `cb_investment`, web reads;
  (b) "mark as invested" reconcile from broker holdings;
  (c) Kite Connect basket-order handoff (later).
  Record in `docs/DECISIONS-MERGE.md` tagged ⚠ UNREVIEWED. PlanHandoffPanel stays terminus until (c).
- **Manager**: `/manager/[slug]` reads `cb_manager` (bio, strategies, disclosures_md, SEBI reg).
- **Costs & returns**: fee math in `baskfy_core` (gst / fee modules); page renders cost-adjusted series.
- **Notifications**: rebalance-published first; no new channel that has never been delivered — prove one path.

### Data ownership (no two leaves share a file)

| Leaf | Owns |
|---|---|
| 7.1 metrics populate | ops script or make target + status docs; may call `compute_all_metrics`; not web UI |
| 7.2 investment contract | `docs/DECISIONS-MERGE.md` Tree7 entry + `NEEDS-MAULIK.md` if hands needed; no code until decided |
| 7.3 manager page | `apps/web/src/app/(app)/manager/[slug]/` + fetch helper |
| 7.4 costs-returns | `apps/web` costs page under investment or basket; uses existing fee core |
| 7.5 rebalance notify | worker/alert path for rebalance-published only |
| 7.6 price-return caveat | shared disclaimer component on catalog + performance surfaces |
| 7.0 root | `gates/tree7-root.md` — integration |

### Naming
- Decision tags: `T7.1`, `T7.2`, …
- Gates under `gates/tree7-*.md`.

## Tree

- 7 Investment completion gap ..................... gates/tree7-root.md
  - 7.1 Populate cb_metrics ....................... gates/tree7-leaf-7.1-metrics.md
  - 7.2 Investment-creation contract .............. gates/tree7-leaf-7.2-contract.md
  - 7.3 Manager profile page ...................... gates/tree7-leaf-7.3-manager.md
  - 7.4 Costs & returns page ...................... gates/tree7-leaf-7.4-costs.md
  - 7.5 Rebalance-published notification .......... gates/tree7-leaf-7.5-notify.md
  - 7.6 Price-return caveat component ............. gates/tree7-leaf-7.6-return-caveat.md

Deferred (named, not gated this run): SIP delivery proof, subscriptions surface, drift-repair UI,
"why did this change" panel, public signup, broker stale-session UX, route-sweep in CI.

## Status log

- 2026-08-24 plan written, contract fixed, sequence locked to Maulik brief
- 2026-08-24 leaf 7.1 verified — cb_metrics COUNT=1; CLI + Celery session fix (T7.1)
- 2026-08-24 leaf 7.2 verified — contract (b) mark-as-invested; NEEDS #15 for UI/API
- 2026-08-24 leaf 7.3 verified — `/manager/[slug]` + basket links; API pre-existed
- 2026-08-24 leaves 7.4–7.6 deferred this pass (named in plan; not abandoned)
- 2026-08-24 explore brief ([Explore Tree7](be5ebb84-d679-4ac2-b9ad-83d710d73feb)) arrived after 7.1–7.3 landed — recommended (a) desk-creates; T7.2 already chose (b). Gate stubs 7.4–7.6 written with ABANDON deferred.
