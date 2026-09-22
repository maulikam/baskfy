# Runbook 11 — an O1 plan or verdict (`OPTIONS_PLAN`)

**Verified against:** NOT YET — written from `baskfy_worker/options/plan.py` and
`baskfy_core/options/plan.py` (OP6). The builder has run only against fixtures on `baskfy_test`; it
has never built a plan from a live chain. Every options money flag is **false**, and nothing in this
alert, the builder or its task can send an order.

## What fired

`baskfy.options.plan_o1` (Beat `options-plan-o1`, every minute 10:00–10:16 IST mon–fri, **dark**
unless `BASKFY_OPTIONS_MONITOR_ENABLED` and `BASKFY_OPTIONS_COLLECT_ENABLED` are both true) decided an
O1 sleeve's day — O1-M on the month's last expiry, O1-W on a weekly expiry (`docs/options/04` §1.2).
It fires once per sleeve per day, when the session is first written:

| Summary begins | Meaning |
|---|---|
| `O1-M 2026-10-27: no trade — GAP_TOO_BIG, ER_TOO_HIGH.` | the gate or the plan refused; every reason is listed (`04` §3, condor §2, OP4.6 for the `REJECTED_*` codes) |
| `O1-M 2026-10-27 PAPER plan O1M-20261027-…: iron condor …` | one four-leg plan, `ISSUED`, with credit, lots, max loss, round-trip costs, margin and its expiry (10:15) |

The plan's rows: `SELECT * FROM op_plan WHERE plan_id = '<id>';` and its legs
`SELECT seq, role, tradingsymbol, side, quantity, limit_price FROM op_leg l JOIN op_plan p ON p.id = l.plan_id WHERE p.plan_id = '<id>' ORDER BY seq;`
— seq 1–2 are the wings, 3–4 the shorts: the send order (never naked, `02` Track C §2).

## What to do

1. **Nothing, to leave it.** A plan nobody confirms lapses at `expires_at` (the task moves it and
   its session to `LAPSED` at the next minute). Nothing is ever sent by the plan itself.
2. **To act on it (paper)**: the desk's `/nifty-options` Confirm arrives in OP10; until then a plan
   is a proposal to read, not to execute. Never place its legs by hand from this alert as a live
   trade — the sleeve is in its paper period (`02` §3.2).
3. **Warnings on the plan** — `MARGIN_POOL_UNSET` (set `op_book_config.margin_pool_inr` before any
   live sleeve; paper is unaffected), `MARGIN_UNKNOWN` (no Kite session at 10:00, so the calculator
   was not asked — log in; a live plan would have been refused), `RESERVE_EXCEEDED` (costs per lot
   above the ₹1,000 reserve — read the cost share).
4. **A verdict you did not expect** — the gate's numbers are in `op_session.numbers` for the day and
   on `/options`; the same computation produced the scan's candidate (`04` §10).

## If no alert came on an O1 day

* The task is dark by default: check `BASKFY_OPTIONS_MONITOR_ENABLED` and
  `BASKFY_OPTIONS_COLLECT_ENABLED` in the worker's environment, and `BASKFY_SOLE_USER_ID`.
* No `WINDOW_CLOSED`/`NOT_READY` day writes a session: if the 09:59 bar or the 10:00 chain never
  arrived before 10:15, there is no session — see the collector (`options-collect-chain`) and index
  bars (`options-index-bars`) in the worker log.
* Re-running is safe: `uv run python -m baskfy_worker.options_cli plan --at 2026-10-27T10:01:00+05:30`
  returns today's decided session unchanged (idempotent per date; no second alert).
