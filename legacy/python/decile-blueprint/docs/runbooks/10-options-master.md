# Runbook 10 — the NIFTY options master (`OPTIONS_MASTER_CHANGED`)

**Verified against:** NOT YET — written from `baskfy_worker/options/master.py` (OP2). The nightly
`baskfy.options.refresh_master` job has not yet run against a real Kite NFO dump; the first run is the
first 19:30 on the box after OP2 is deployed. Every options money flag is **false**; nothing here
can reach a broker.

## What fired

`baskfy.options.refresh_master` (Beat `options-contract-master`, 19:30 IST mon–fri) wrote tonight's
NIFTY CE/PE contracts into `op_contract` and rebuilt `op_expiry`, and one of these changed
(`docs/options/03` §2):

| `what` | Meaning | Why it matters |
|---|---|---|
| `LOT_SIZE` | an expiry's lot size differs from last night's | every plan sizes by it (`04` §1.4) |
| `KIND` | an expiry flipped between `WEEKLY` and `MONTHLY` | decides whether O1-M or O1-W trades it (`04` §1.2) |
| `WITHDRAWN` | an expiry that has not happened yet vanished from the master | the shape of a holiday shift: the Tuesday goes, a Monday appears |

The alert's `detail.changes` lists each one; `op_expiry.detail.history` keeps them.

## What to do (before 09:15)

1. Read the alert, then `SELECT * FROM op_expiry WHERE underlying='NIFTY' AND expiry_date >= current_date ORDER BY 2;`
2. **A withdrawn expiry** — check the NSE circular for the holiday. The calendar already follows the
   master (no weekday rule), so the new date is used automatically; confirm the replacement row
   exists with the right `kind`. If the master looks wrong rather than shifted, set no flag and
   note it in NEEDS-MAULIK § Options — the sleeves will read whatever the master says.
3. **A lot-size change** — check NSE's lot-size revision circular. No action is needed for sizing
   (it reads the master), but a paper period's R figures straddling the change should be read with
   that in mind.
4. Re-running is safe: `uv run python -m baskfy_worker.options_cli refresh-master` (idempotent; it
   does not alert twice for the same change).

## If the job refused or skipped

* `refused: the NFO dump held no NIFTY option` — Kite answered with an empty dump; nothing was
  written and last night's calendar stands. Re-run after a Kite login.
* `skipped: no usable Kite session` — nobody logged in today; the calendar keeps last night's rows.
