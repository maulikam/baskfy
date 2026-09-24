# FO — final report (25 Sep 2026)

The FO run added **overnight, defined-risk F&O** to Baskfy: two paper sleeves, the F&O data layer
and two read-only web pages, with **every money flag off**. It ran FO0 → FO12 in one session on
branch `developer`. It was deployed as `7c1124b` and `verify-fno.sh` reads **FNO OK** on the box.
The pack is `docs/fno/`, the ledger is `docs/fno/STATUS.md`, and every judgement call is in
`docs/fno/DECISIONS-FO.md` (M.1–M.4 are yours; the FO*.n entries are ⚠ UNREVIEWED).

**Nothing in this run is a claim of profitability.** The research found every stock F&O family
negative after costs. F1 is +0.022R a trade on 100 trades with your loss close (t = 1.01), and F2
is +0.017R and negative in four of five years. Paper exists to find out whether F1 was luck.

## What was built

| Module | What it is | Evidence |
|---|---|---|
| FO0 | Baseline: box flags, BANKNIFTY's monthlies from the master (Tuesdays; 23 Nov a Monday; lot 30, step 100), Zerodha's delivery-margin ramp, the box reading the F&O bhavcopy and the ban list | research reproduces: B4 n = 100 +0.033R, loss close +0.022R, F2 n = 2,334 +0.017R |
| FO1 | `baskfy_core.fno`: every number of `04` as a pure function; `covered` property-tested over every prefix and partial fill | 211 tests; the research port reproduces B4 **trade by trade** |
| FO2 | `0052_fno` (the `fo_` schema), the 18:30–23:30 bhavcopy ingest, `fo_ban_list`, the resumable backfill, the NFO master widened with O-sleeve reads proved NIFTY-only, `fo_underlying_daily` anchored to the settle | 216 underlyings derived from the 22 Sep file in 0.1 s |
| FO3 | 15:00 spread sample: one Kite quote, 448 keys, into `fo_spread_sample` | 37 tests |
| FO4 | Nightly scan into `fo_scan` for F1 (per underlying) and F2 (every name in the universe) | the ₹10 lakh sizing gap found here (M.2) |
| FO5 | `GET /fno/overnight`, `/fno/info`, `GET\|PATCH /fno/config`; `/options/overnight` and `/options/fno` | every other verb 405; no route near the gateway |
| FO6 | Gateway: NRML on NFO only with `OPTIONS_ENABLED` + `BASKFY_FNO_CARRY_ENABLED` + a `fo_plan` reference; the covered-overnight guard, fail closed; F2 stock-future GTTs only | orders without `fo_plan` unchanged byte for byte; execution suite green |
| FO7 | Desk `fno_monitor`: F1 09:20 plans, 60 s profit take and loss close, E−1 15:00 exit, `LATE_EXIT`; F2 GTT, never-lower trail, E−1 roll, 40-session exit, `NAKED_FUTURE`; nightly marks | 19 replays on Postgres; the FO gateway is locked to dry run (`LIVE_NOT_BUILT`) |
| FO8 | Desk `/fno` page and `POST /fno/execute` (paper) | spy broker 0 calls even with `DRY_RUN=false` |
| FO9 | The quarterly re-test of all 15 `RESEARCH.md` families from `fo_contract_daily` | **every family reproduces** from the raw day files (golden gate) |
| FO10 | Journal at every close, ledger per sleeve/underlying/book, the `04` §7 pauses, the `04` §9 checklist, paper and live never pooled | fixed FO4's pooling |
| FO11 | The safety proof: scans, 16 flag rows per sleeve, Hypothesis over the execute route and the fill scripts, `tools/fno/drill.py` | `sleeve=F1N confirms=1 fills=8 orders_to_broker=0`; F1B and F2 the same |
| FO12 | `fno-monitor` in compose with every FO money flag pinned false, `verify-fno.sh`, five alerts, deploy | **FNO OK** on the box |

## What was decided

Your decisions (in session): **OP-M.1** the options monitor on; **M.2** F1's capital ₹25 lakh
(₹10 lakh sized zero lots every month: one lot's max loss last year was ₹15.8k–26.2k on NIFTY and
₹17.1k–46.6k on BANKNIFTY); **M.3** the F1 confirm sentence names the loss close; **M.4** the scan
and the monitor on, no backfill yet.

The agent's calls worth reading first (all ⚠ UNREVIEWED in `DECISIONS-FO.md`):

* **FO7.1** the FO gateway is locked to dry run in code, so even with every flag on, a live order
  is refused `LIVE_NOT_BUILT`. Live FO needs code (reading fills back from Kite), not just flags.
* **FO2.8** `fo_underlying_daily`'s levels are rescaled each night so the close equals the settle
  (otherwise ATR rounded to zero at `numeric(18,2)`).
* **FO9.3** the re-test pages option rows a batch of symbols at a time: B1 over the whole panel
  peaked at 2.8 GB, which the box (7.8 GB, no swap) cannot spare. A batch gives exactly the same
  trades.
* **FO10.5** a book loss limit of ₹0 means the ₹75,000 ceiling, not "off".
* **FO5.1** capital and risk % are editable on `PATCH /fno/config`, audited and under the ceilings.

## What is NOT done

* **No backfill** (M.4). History starts with the 25 Sep bhavcopy tonight. F2's 50-session signal
  cannot fire for about 50 sessions, and the re-test and the 1-year IV percentile stay thin until
  `fno_cli backfill` runs (~90 minutes at the NSE limiter, outside market hours).
* **No live FO path.** FO7.1 pins the gateway to dry run, and nothing reads fills back from Kite.
* **No browser or phone check** of `/options/overnight`, `/options/fno` or the desk's `/fno`.
* **No web settings form**; the PATCH exists in the API only.
* The results calendar (Q8) has no table (FO3.6); F2 has no results-day skip (FO4.6).
* The `ROLL_INCOMPLETE` journal path is wired and untested (FO10).
* Nothing of the FO book has seen a live Kite session: every plan, fill and exit has run on
  fixtures and the test database.

## What needs you

1. **The real-money gate (`02` §3) is not met by either sleeve.** Tier 2E as tested: F1 with the
   loss close is −0.010R in 2022; F2 is negative in four of five years. Any flag flip needs your
   written waiver or forward evidence, plus the paper period, plus the live path (above).
2. **F2's capital** is ₹0. Paper runs one lot; live would refuse `NO_SLEEVE_CAPITAL`.
3. **The backfill**, when you want the history.
4. **A Kite login every trading day** while an F1 structure is open: a cycle missed for want of a
   login counts as a paper-period violation (`04` §9).

## The first paper entry day, step by step

F1 enters 15 exchange sessions before each monthly expiry. The next cycles expire on **27 Oct
2026** (NIFTY and BANKNIFTY), so the entry session is in early October; the scan names the exact
date on the Overnight page the night before (`NOT_ENTRY_DAY` carries `next_entry_date`).

1. **The evening before**, after the 18:30 ingest: `https://staging.baskfy.com/options/overnight`
   shows the underlying as `CANDIDATE` with the proposed condor from that night's bhavcopy
   (strikes, credit, max loss per lot, lots, cost share, IV ÷ RV recorded and not used), labelled
   `As of close, <date>`.
2. **Before 09:14**, log in to Kite through the web app. The desk's `/status` must say
   `"authed": true`. It says `"dry_run": false` on the box, and that is correct: the FO book is
   paper because the FO gateway is dry-run-locked and `OPTIONS_ENABLED` is false.
3. **09:14** `fno-monitor` starts. **09:20** it re-prices the condor on live quotes, checks OI and
   spreads, sizes against ₹25,000 with the broker's basket margin as a ceiling, and writes the plan
   (`FNO_PLAN` email). The plan expires at the earlier of 30 minutes and 10:30.
4. **On `https://desk.staging.baskfy.com/fno`**, read the plan card: four legs, bid/ask, credit
   against the scan's, max loss in ₹ and R, costs, longs first, the hard-exit date. Click
   **Confirm (paper)**: "This confirm also authorises this structure's 50 % profit take, its loss
   close at 1.5× the credit (shorts first), and its E−1 15:00 exit. It places nothing else."
5. The legs go through the real gateway's dry-run branch, **longs first**, filled from live depth,
   and the position opens `simulated=true`.
6. **Every 60 s** in session the monitor checks the profit take (cost to close ≤ 50 % of credit)
   and the loss close (≥ 2.5 × credit). **Each night** it marks at the settle. You get
   `FNO_HARD_EXIT_TOMORROW` the evening before E−1.
7. **E−1 15:00** (26 Oct for the October cycle) the structure is closed shorts first, unless a
   rule closed it earlier. The journal row lands in ₹ and R, and the paper tally on the Overnight
   page counts the cycle.

Rollback of the deploy: the image tag lines in `/opt/baskfy/.env.staging.compose` back to
`f96b6ef`, then `up -d`. Turning the FO book off: delete the `BASKFY_FNO_*` lines from both env
files and `up -d worker ingest-worker beat desk fno-monitor` (M.4's reversal).
