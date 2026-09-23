# Runbook 11 — an options plan or verdict (`OPTIONS_PLAN`)

**Verified against:** NOT YET — written from `baskfy_worker/options/plan.py`,
`baskfy_worker/options/plan_o2.py` and their pure cores (OP6, OP7). Neither builder has run
against a live chain; both have run only against fixtures on `baskfy_test`. Every options money
flag is **false**, and nothing in this alert, either builder or their tasks can send an order.

One alert name covers every sleeve; `labels.sleeve` says which one (`O1M`, `O1W`, `O2`).

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

### O2 (the directional sleeve, OP7)

`baskfy.options.plan_o2` (Beat `options-plan-o2`, every minute 09:30–13:34 IST mon–fri, behind the
same two flags) decides **every** non-event trading day: the day filters at 09:30, then the first
with-trend 5-minute break of the opening range (`04` §4.1–§4.2). It makes **no Kite call at all** —
a long option costs its premium, so there is no margin to ask about (`DECISIONS-OP` OP7.3).

| Summary begins | Meaning |
|---|---|
| `O2 2026-10-19: no trade — GAP_TOO_BIG.` | a day filter refused (gap, opening range, VIX, trend, event day) |
| `O2 2026-10-19: no trade — NO_TRIGGER.` | the 13:30 window closed with no with-trend break; counter-trend breaks are in `op_session.numbers.counter_trend_breaks` and were never traded |
| `O2 2026-10-19: no trade — REJECTED_DELTA.` | the tape broke, but the one-step-ITM contract was outside 0.50–0.75 delta, illiquid, unaffordable or cost-heavy |
| `O2 2026-10-19 PAPER plan O2-20261019-…: BUY 65 …` | one long, `ISSUED`, with the stop, target, time stop, hard exit and the gap-through worst case; it expires 30 minutes after issue, never later than 13:30 |

An O2 plan's leg is always a single `LONG_CALL` or `LONG_PUT` — there is no short leg in this
sleeve, so `margin_required_inr` is `NULL` by design and `detail.premium_inr` is the money at risk
in the worst case.

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
  returns today's decided session unchanged (idempotent per date; no second alert). For O2:
  `uv run python -m baskfy_worker.options_cli plan-o2 --at 2026-10-19T10:06:00+05:30`.
* **O2 specifically:** a day with no with-trend break writes nothing until 13:33, when the window
  is final and the session is written `SKIPPED / NO_TRIGGER`. A trigger whose bar closes at or
  after 13:30 gets no plan at all (`WINDOW_CLOSED`): a plan issued then could not outlive its own
  issue (`DECISIONS-OP` OP7.5).
