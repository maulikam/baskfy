# Runbook 6 — the swing book's morning (SW11)

**Verified against:** NOT YET — written from `app/swing_clock.py`, `baskfy_worker/tasks/swing_ops.py`
and `baskfy_api/swing_health.py`, like the other five; no swing morning has run anywhere. Fires on the
five `SWING_*` rules in `infra/prometheus/alerts.yml` (group `baskfy-swing`); four of them are
also raised in-process by `baskfy.swing.check_*` (`baskfy_worker/tasks/swing_ops.py`) at the
moment each is about. Every fact both read comes from `baskfy_api/swing_health.py`.

The desk page (`/swing` on the desk) shows the same facts as buttons: **Reconcile fills**,
**Re-arm GTT**, the cutoff. Nothing in this runbook places a buy; the only order-shaped actions
are a cancel and a stop, and both go through the swing gateway.

## SWING_POSITION_NAKED (warning, any time, 10 m)

A position has shares open and no resting GTT for ten minutes. The confirm arms the stop in the
same request, so this means the GTT was refused (the outcome's `reason` on the desk page says
why — usually the stop is not below the last price, or the band refused it) and nobody re-armed.

1. Desk page → the book panel → **Re-arm GTT** on the naked row. It needs a last price above
   the stop; with no Kite session it answers `BLOCKED` and says so.
2. If it stays naked, `sw_position.stop` is above the market: raise nothing, decide the exit by
   hand on the page (a `SELL_AT_OPEN` line in tomorrow's plan, or Kite directly), and note it in
   `sw_session.notes`.

## SWING_MONITOR_DID_NOT_START (critical, 09:20–10:45 IST)

`BASKFY_SWING_MONITOR_ENABLED` is true in the API's settings and no `sw_session` row for today
has `monitor_ran` (the monitor writes it on the **first tick it handles**, `app/swing_monitor.py`
— a launched process whose Kite token had died writes nothing, which is the point).

1. On the box: is `python -m app.swing_monitor` running? (`docker compose ps desk-monitor`,
   or the process list.) It exits 0 at once with the flag off, and with a Kite token that has
   expired it logs `no watchlist`/`not authed` — Kite tokens die daily; log in before 09:00.
2. MD20: one env file feeds every service. If the desk's `.env` has the flag on and the API's
   copy is off (or the reverse), the alert reads the wrong thing — keep them together.
3. Started late? It still watches until 10:45 and runs the cutoff and the 15:15 sweep; the
   signals before it started are lost, and today's plan is the evening's `MORNING` plan only.

## SWING_DETECT_STALE (critical, 21:30–23:30 IST)

`sw_market_daily` has no row for the most recently published trade date. The chain's twelfth
step (`compute_swing`) and the 21:00 belt (`swing-eod` Beat entry) both failed or did not run,
and the 21:05 evening job planned against yesterday's gate.

1. `/admin/pipeline` — did the chain reach `compute_swing`? Its step payload carries the
   detectors' error if it raised (the step is written so it cannot fail the night).
2. Run it by hand: `make swing DATE=YYYY-MM-DD` (then `make swing-eod DATE=...` to re-plan).
3. Idempotent: re-running rewrites the same rows; a row the evening already settled keeps its
   rung (`write_market_row`, SW11).

## SWING_ORDER_OPEN_AFTER_CUTOFF (critical, 10:50–15:30 IST)

A `BUY_ON_TRIGGER` line of today is still `SENT` after the 10:45 cutoff should have cancelled
its remainder (`app/swing_clock.py` → `cutoff_open_orders`).

1. Desk page → the cutoff button (`POST /swing/cutoff`), or read `sw_plan_line.note` for
   "10:45 cancel refused: …" — a refused cancel is the gateway's words (untouchable, kill
   switch, a dead order the broker already closed).
2. If the order is already complete or cancelled at Kite, **Reconcile fills** applies the final
   state and closes the line (`FILLED` with shares, `EXPIRED` without).
3. A cancel that Kite refuses is cancelled from Kite's own order book; then reconcile.

## SWING_GTT_MISSING_AT_1515 (critical, 15:20–21:00 IST)

Shares open with no GTT after the 15:15 sweep (`app/swing_clock.py` → `run_gtt_sweep`, which
re-arms at the last price the desk can read). The book is going into the close unprotected.

1. `sw_session.notes` for today says what the sweep did: `gtt-sweep 15:15: N naked, M armed,
   K still naked`. No line → the monitor process was not alive at 15:15 (see
   SWING_MONITOR_DID_NOT_START; the sweep runs in that process).
2. **Re-arm GTT** on the desk page per position (a GTT can be placed after hours).
3. Still naked → `stop` is above the market: this is a decision, not a re-arm; see the first
   entry.
