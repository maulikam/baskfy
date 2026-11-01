# 05 — UI spec: Stock F&O on the Options tab, and the desk's `/fno`

The division is the same as every sleeve's. The **desk** (Jinja, `kite-momentum-rebalancer/app`)
is the only surface with a Confirm. The **web app** is read-only. Disclaimers are components.
There is no new visual language: the tokens and components are `DESIGN.md` and
`docs/PORTFOLIO-DESIGN-SYSTEM.md`. This extends `docs/options/05`, which stays authoritative for
the O1–O3 panels.

## 1. Navigation

**Web.** No new top-level tab. The **Options** tab (`/options`) gains a sub-nav:
`Intraday (NIFTY)` · `Overnight` · `Stock F&O`. The first is today's page, unchanged. The new
routes are `/options/overnight` and `/options/fno`. `nav.test.ts` is extended, not rewritten.

**Desk.** An `F&O Overnight` tab beside `NIFTY Options`, route **`/fno`**, with a badge for open
structures and their nearest hard-exit date, and `PAPER`/`LIVE` per sleeve from `fno_gates()`.

## 2. `/options/overnight` — F1 and F2 (web, read-only)

**Header.** Per underlying: the next F1 entry date, and today's state chip (`04` §8). A red
**"Hard exit tomorrow 15:00"** line appears when any open structure's `hard_exit_date` is the next
session.

**The clock, stated on the page** (CLAUDE.md's two-clock rule; FO5 adds this row to the root
table in the same commit):

| What | Clock | Label |
|---|---|---|
| Scan state, the proposed condor, IV/RV | The last completed session's bhavcopy (`fo_scan.trade_date`) | `As of close, Tue 22 Sep` |
| Open structures' marks | The last session's **settle** (`fo_mark`), plus the shared `useLiveMarks` overlay on the **underlying's level only** during market hours | `Marked at settle, 22 Sep` · `NIFTY live 13:14` |
| Journal, re-tests | closed records | dates only |

The page never prices an option live. **Live leg prices exist only on the desk**, where they
decide the plan. The page says so.

**Cards.** One per underlying. *Proposed condor* (on an entry day): four strikes with IV, credit,
widths, max loss per lot, cost share, and IV ÷ RV20 with the note "recorded, not used". *Open
structure*: the legs, entry credit, today's mark, P&L in ₹ and R, the profit-take level, the hard
exit date, and the days held. *Journal*: closed structures, in R, paper and live never pooled.

**Evidence card.** The Tier 2E line from `RESEARCH.md` §B4 with its caveat verbatim (`07` §4) and
n = 100; the latest quarterly re-test; the paper tally against `04` §9's checklist.

**F2 section.** A **permanent amber banner**, not dismissible: "Built by choice against the
research: +0.017R a trade after rolls, negative in 2022, 2024, 2025 and 2026 (`RESEARCH.md`)." Below
it: tomorrow's F2 candidates from `fo_scan` (symbol, breakout level, stop, R per lot, what one lot
would risk in ₹, and `REJECTED_SIZE` where the ceiling refuses it), open positions (entry, current
trailing stop, the GTT's state, next roll date, sessions held, P&L in ₹ and R), and closed trades
with their rolls.

**F3 section** (F3-7). One card per underlying — NIFTY on the weekly expiry (F3N), BANKNIFTY on
the monthly (F3B): the session the desk may plan it at 09:20, the index level (the shared overlay,
as F1), the night's state chip `As of close, <date>`, the reasons, the daily direction and the
75-minute confirm's sentence, the levels (close, support, resistance, 20-day average, weekly
range), and on a candidate the proposed spread (wing and short with settles, expiry, credit, max
loss per lot, the decay-target and loss-cut prices, the key level, the sizing sentence). Below:
open spreads `Marked at settle, <date>` (lots, legs, credit taken, price, P&L in ₹ and R, key level,
target, cut, days held), closed spreads in R, and the evidence card — the EOD re-test's line, the
four rules it cannot test, the latest `F3N`/`F3B` quarterly re-test with its caveat. The exits are
said in words, with the auto-exit switch's rule; the page states no flag value it does not read,
and binds no action.

## 3. `/options/fno` — Stock F&O information (web, read-only)

The banner from `04` §5 ("None of these numbers predicted a profitable trade after costs in
2022–2026", linking the research). Then the sortable table of `04` §5 over every F&O underlying,
with a filter for the ban list. The clock label is `As of close, <date>`. The price column may
take the shared live overlay during market hours (the four-screen-pages rule, 21 Sep 2026); every
other column is end of day. Below the table: **"Families tested and rejected"**, which is
`RESEARCH.md`'s verdict table with the latest re-test beside each row, and the date and number of
sessions of measured spreads (FO3).

Phone: the table collapses to symbol · IV ÷ RV · OI chg · ban, and a tap opens the row.

## 4. The desk `/fno`

* **Morning plan card** (09:20 on an entry day): the four legs re-priced from live quotes, with
  bid/ask, spread %, OI, credit vs the scan's credit (a > 20 % drift is shown in amber), max loss in
  ₹ and R, lots, the broker's basket margin vs free margin, the costs, **the entry sequence
  (longs first)** and the hard exit date. Then **Confirm (paper)** with the sentence: "This
  confirm also authorises this structure's 50 % profit take, its loss close at 1.5× the credit
  (shorts first), and its E−1 15:00 exit. It places nothing else." (Maulik, M.3: the loss close
  of M.1 fires under the same confirm, so the sentence names it.) It has a 30-minute countdown to `expires_at`.
* **Open structures**: a live mark, the distance of the underlying to each short strike in σ, the
  distance to the loss close (1.5 × credit) and to the profit take, and the next rule to fire.
* **F2**: the morning candidates with Confirm (paper), each stating "This confirm also authorises
  this position's trailing GTT stop, its E−1 rolls and its 40-session time exit"; open futures with
  the GTT id and trigger, and a red flag on any position whose GTT is not resting.
* **Exits** fire under the confirm and are shown as they happen. Any refusal (guard, margin,
  stale quote) is shown by name, never as a spinner.
* No field on this page moves money except Confirm. Capital, risk % and pauses are the settings
  form, audited.

## §6 — F3 on the desk's `/fno` (F3-5; M.5)

* **Plan card** (group F3): the direction, the level and the index at 09:20, the credit on mids,
  the width, the target and cut marks, the rule sentence; the confirm sentence says the confirm
  sends the wing then the short and nothing else, that the exit is the monitor's only under
  `BASKFY_FNO_F3_AUTO_EXIT`, and that an add is always a click.
* **Open spreads**: one card per F3 position — the direction, the level against the live index,
  the credit, the live mark and P&L, the target and the cut, the expiry, the legs with their fills
  and mids, whether the add is spent, the next rule; the section header states the flag's state.
* **Exits and adds**: an F3 exit the monitor raised with the flag off shows `WAITS FOR YOUR CLICK`
  with a **Send the exit (paper)** button and its sentence (short first, then the wing); an add
  shows **Add (paper)** with its sentence. Both post to the one execute route and lapse with the
  plan's expiry. The web app's `/options/overnight` carries F3 read-only (§2, F3-7); the buttons are
  here only.
