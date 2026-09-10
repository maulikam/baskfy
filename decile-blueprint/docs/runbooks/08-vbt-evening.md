# Runbook 8 — the volume-breakout sleeve's evening (VB7)

**Verified against:** NOT YET — written from `baskfy_worker/tasks/vbt.py`,
`baskfy_worker/tasks/vbt_evening.py` and `baskfy_worker/tasks/vbt_ops.py`, like the seven before
it; no VBT evening has run anywhere and the sleeve has never held a share. Fires on the four
`VBT_*` names in `baskfy_worker.alerts.AlertName`, each raised in-process by a
`baskfy.vbt.check_*` task at the moment it is about.

**Read this first.** `BASKFY_VBT_EXECUTION_ENABLED` is **false**, and while it is false nothing
in this runbook can reach a broker: the desk simulates every confirm and writes the fill it would
have got. Two of the four alerts below are therefore about *the paper book being wrong*, not
about money. Treat them as real anyway — the point of twenty DRY_RUN sessions is to find out
whether the machinery is trustworthy before it is trusted, and an alert nobody reads during the
paper run is an alert nobody will read after it (`docs/vbt/02` §3).

Nothing here places a buy. The only order-shaped actions are a cancel and a stop, both confirmed
by hand on the desk's `/vbt` page, both through the VBT gateway.

## VBT_ORDER_PAST_EXPIRY (critical, 21:40 IST)

A working limit is still in a live state (`PROPOSED`, `CONFIRMED`, `SENT`, `PARTIAL`) with an
`expires_after_session` now in the past. **This is the alert VB7 exists for.**

The signal-day close works as a limit for three sessions and no longer (`docs/vbt/04` §7.2). The
window is not a preference: at two sessions the study returns 11.4% a year and at three 18.2%, so
an order that outlives it is neither the strategy's trade nor anybody's — it is a bid resting in a
name whose setup is four days old, which can fill on news nobody planned for.

The evening job sweeps: `sweep_expired_orders` writes a `CANCEL_LIMIT` line into that evening's
plan and a person confirms it. So an order past its expiry means **either the sweep did not run
or its cancel line was never confirmed**.

1. Desk page → `/vbt` → the working-orders panel. An expired row is rendered with its
   `expires_after_session` in the past; confirm its **Cancel** line. That is the whole fix in the
   ordinary case, and it takes ten seconds.
2. No plan on the page at all? The evening job did not run. `/admin/pipeline` → did step 13
   (`compute_vbt`) reach `SUCCEEDED`, and did the `vbt-evening` Beat entry (21:15) fire? Run it
   by hand: `make vbt-plan` on the box, or `celery call baskfy.vbt.evening`.
3. The order is `SENT` or `PARTIAL` and the flag is **true**: the limit is live at the broker and
   the desk's cancel is the only thing that removes it. If the desk cannot reach Kite, cancel it
   in Kite directly, then mark the row `CANCELLED` on the page so the book and the broker agree.
   A partial fill keeps the shares it got: the position is real, and `04` §7.6 sizes the stop on
   what filled.
4. It keeps firing on the same row: the sweep is not seeing it. Nearly always the row's
   `expires_after_session` is null — an order that was written before its session calendar was
   known. Set it from `signal_date` plus three sessions and re-run the evening.

## VBT_POSITION_NAKED (critical, 21:40 IST)

A `vb_position` has `quantity_open > 0` and no `gtt_id`. The method has exactly one stop and it
is a resting GTT 12% below entry, armed in the same confirm as the buy (`04` §6.1).

1. Desk page → `/vbt` → the book → **Arm GTT** on the naked row. It needs a last price above the
   stop; with no Kite session it answers `BLOCKED` and says so.
2. Still naked because the stop is above the market: the name has already fallen through the
   level. Do not lower the stop — `04` §6.2 says a stop never falls. Sell it at the next open by
   hand and record why.
3. In DRY_RUN the arm is simulated and `gtt_id` is a `SIM-` string. A naked row during the paper
   run means the simulated arm was refused too, which is a real finding: the same refusal would
   have happened with money.

## VBT_DETECT_STALE (warning, 21:30 IST)

`vb_breadth_daily` has no row within the last four days. A day with no signals still writes a
breadth row, so its absence is the difference between "nothing qualified" and "the detector did
not run" — a distinction the page cannot make on its own, which is why this check exists.

1. `/admin/pipeline` — did the chain reach `compute_vbt` (step 13)? It cannot fail the night, so
   a failure there is a `SUCCEEDED` chain with a recorded step error. Read the step's payload.
2. The 21:10 `vbt-detect` belt asks before it works: with a `vb_signal_daily` row already present
   for the date it returns `skipped`. That is not staleness.
3. Run it by hand: `make vbt` (or `python -m baskfy_worker.vbt_cli --date <YYYY-MM-DD>`). It needs
   201 sessions of bars per name; a fresh database has none and the honest answer is to wait for
   the backfill rather than to lower the requirement.

## VBT_POSITION_NO_BAR (warning, 21:40 IST)

A held name has not printed a bar in more than seven calendar days. The evening writes a position
off after five blank sessions at its last close (`04` §6.5), and this is the warning that comes
first, because a delisting or a suspension is a fact about a company and the five-session
write-off is only bookkeeping.

1. Find out what happened to the name: NSE corporate announcements, then the exchange's
   suspension list.
2. Suspended and expected back: leave it. The write-off is what happens if it does not return.
3. Delisted, merged or in a scheme: the shares are not worth the last close and the book will say
   they are. Close the position on the page at the price the corporate action gives, and add the
   instrument to `NEEDS-MAULIK.md` — pre-2024 corporate actions are already a known hole (V2).

## What none of these do

No check here writes an order, cancels anything, or changes a stop. Each is one indexed query and
a possible alert; the fix is always a confirm on the desk page or a decision a person makes.
That asymmetry is deliberate — the sleeve's whole safety argument is that the worker plans and a
human executes (`docs/vbt/02` §2, non-negotiable #1).
