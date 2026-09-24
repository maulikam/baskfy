# Runbook 13 — the FO book (`FNO_WEEKLY`)

**Verified against:** NOT YET — written from `baskfy_worker/fno/weekly.py`,
`baskfy_worker/fno/ledger.py` and the desk's `app/fno_ledger.py` (FO10). None has run against a
real FO book; all have run only against fixtures on `baskfy_test`. Every FO money flag is
**false**, and nothing in this alert, its task or the readers behind it can send an order.

FO12 adds the FO book's other alerts (plan issued, exit done, hard exit tomorrow, `LATE_EXIT`, a
missing bhavcopy) to this file.

## What fired

`baskfy.fno.weekly` (Beat `fno-weekly`, Friday 16:35 IST, **dark** unless
`BASKFY_FNO_MONITOR_ENABLED`) summarised each FO tenant's week. It is information, not a failure:

| Part of the summary | Where it comes from |
|---|---|
| `F1N paper: 1 closed, ₹-1234.50, -0.05R (worst -0.05R)` | the week's `fo_journal` rows, one line per (sleeve, simulated) — never pooled (`docs/fno/03` §6) |
| `paper book month to date: …` | `baskfy_worker.fno.ledger.read_ledger`: realised this month, open positions at their latest `fo_mark` |
| `paper pause F1N: …` | `docs/fno/04` §7 over that mode's journal (`read_pauses`) |

## What to do

* **A pause is listed.** It stops new entries only; open structures run to their own exits.
  F2's and the book's end with the month (`fo_sleeve_config`/`fo_book_config.paused_until`, the
  reason prefixed `PAPER:` or `LIVE:`). **F1's stands until lifted** (DECISIONS-FO FO10.2): after
  reviewing the three losing closes, Maulik lifts it through the FO settings form
  (`baskfy_worker.fno.ledger.lift_f1_pause`, one `fo_config_audit` row `lift:F1N`/`lift:F1B`).
  Nobody else lifts it.
* **A close is missing from the week.** `SELECT * FROM fo_position WHERE closed_at IS NOT NULL
  AND id NOT IN (SELECT position_id FROM fo_journal);` — a closed position without its row is a
  journal gap on the paper checklist (`04` §9). The desk alerts when it cannot write one
  (`FO position <id>: the journal or the pauses failed after the close`).
* **The paper checklist.** `baskfy_worker.fno.ledger.read_checklist(session, user_id, as_of)`
  gives the tally: F1 cycles and structures opened per underlying, F2 sessions, closes and rolls,
  and every violation by kind. A violation restarts that sleeve's count (FO10.6).
