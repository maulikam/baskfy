# SC-AC-REPORT — Tree 2 AC closure (after SC12)

**Date:** 2026-08-23  
**Parent of tree:** `d2910fd` (SC12)  
**Why this tree exists:** SC0–SC12 closed unlazy leaf gates, but `docs/smallcase/06-module-plan.md` ACs still deferred SIP Beat, create persist, dividends, chart, E2E, runbook. Unlazy forbids calling that finished.

## Re-measured parent evidence

| Leaf | Result |
|---|---|
| 2.1 SIP Beat `cb-sip-reminders` | `ok` + worker tests `.... [100%]` |
| 2.2 Create API | `..... [100%]` |
| 2.3 Dividends | `........... [100%]` (11) |
| 2.4 Chart | `performance-chart.tsx` + wired on basket page |
| 2.5 E2E | `e2e/explore-handoff.spec.ts` exists (skips without auth) |
| 2.6 Runbook | `RUN-AND-TEST.md` §8 curated baskets |
| 2.7 Integrity | no OrderGateway on create/sip (`rg` exit 1); `test_ac_no_orders` 4 passed; fee `-k fee` 11 `[100%]` |
| node-2 | OpenAPI execute routes **clean**; OAuth still **False** |

## ABANDON (honest)

- **Live broker OAuth / Phase 4** — blocked on written D3 in `docs/DECISIONS-MERGE.md` (`NEEDS-MAULIK`). Not flipped.

## Still not claimed

- Full Playwright green against a live stack (spec is skip-honest without baseURL/auth).
- Web create form → API wire (API exists; form may still be client-only).
- Dividend **job** writing `cb_dividend` rows (pure derivation exists).

## Gates

Tree-2 leaf + node gates: **0 pending evidence** after parent verify.
