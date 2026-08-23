# TREE4-BACKLOG-REPORT

**Date:** 2026-08-23  
**Mode:** unlazy orchestrated · depth 5 / 10 leaves (+ node)  
**Instruction:** close the listed backlog (items 1–15) without silent ABANDONs.

## Ledger (parent re-run)

| Leaf | Result |
|---|---|
| 4.1 Ops | Junk deleted + gitignored; STATUS refreshed; **ABANDON push** → NEEDS-MAULIK #14 |
| 4.2 Holdings live | `parse_kite_holdings_payload` + `fetch_kite_holdings` (httpx); DRY_RUN→fixture |
| 4.3 Peers | `_WIRED_AUTHORIZE` **5** (zerodha, upstox, angelone, fyers, dhan) |
| 4.4 E2E | `page.route` mocked explore-handoff always-on path |
| 4.5 Chart | `lib/explore/performance.ts` series from metrics |
| 4.6 SC3 loop | `test_sc3_dry_run_loop.py` [100%] |
| 4.7 Customize | `POST .../customize` + page; no OrderGateway |
| 4.8 Runbook | Friday operator checklist in RUN-AND-TEST |
| 4.9 Human | D7 + D10 ⚠ UNREVIEWED written; Track B flags **not** flipped |
| 4.10 Integrity | fee `[100%]`; OAuth `True`; OpenAPI execute **clean** |
| node-4 | 0 pending evidence |

**Gates:** leaf-4.* + node-4 → **all checked**, **0** `EVIDENCE: pending`.

## ABANDON (honest)

- **git push** — this clone has no `origin`. Need URL in NEEDS-MAULIK #14. Did not invent a remote.

## Still needs Maulik hands (unchanged)

- Risk-ceiling deploy (#1), irregular CAs (#9), missing instruments (#11), breadth (#12), credentials housekeeping.
- Counsel C1–C3 on D3 (algo ID / advice characterisation).
- Live Kite login + API secret for non-stub OAuth/holdings against production.

## Not done by design

- Web execute
- Track B flag flips (await real D7 amounts after review)
- PMS / posture C
