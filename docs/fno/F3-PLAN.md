# F3 — the directional index credit spread (Maulik, 28 Sep 2026)

**Asked, in his words (28 Sep 2026, in session):** *"here instead of banknifty choose nifty for weekly
expiry and choose bank nifty for monthly expiry and code it"*, followed by the method: sell weekly
options in the direction of the market; direction from daily support/resistance, confirmed on the
75-minute chart, aligned with the intraday trend; sell roughly 1 % beyond the weekly candle's high
or low; target ~80 % premium decay; out at once when the key level breaks; start with 20–30 % of
capital and add the next day only if the trade is working; ~1 % a week; "sell 20–30 rupee options
and cut at 50".

**His three answers to the questions the F&O charter forced (DECISIONS-FO M.5):**

1. **Structure → a credit spread with a far wing.** The 23 Sep charter (`02` §2.1: no naked short
   option, at any instant, in any sleeve) stands; the wing caps the disaster on a gap.
2. **The non-negotiable exit → auto-exit under a new flag**, `BASKFY_FNO_F3_AUTO_EXIT` (default
   false, his hand). Entries and adds stay clicks.
3. **Instruments → NIFTY on the weekly expiry, BANKNIFTY on the monthly.**

## Contract (fixed before any leaf)

* **Sleeves:** `F3N` (NIFTY, weekly) and `F3B` (BANKNIFTY, monthly); config group `F3`
  (`fo_sleeve_config.sleeve = 'F3'`), flags `BASKFY_FNO_F3_EXECUTION_ENABLED` (default false) and
  `BASKFY_FNO_F3_AUTO_EXIT` (default false). Paper only until `02` §3's real-money gate.
* **Structure:** `CREDIT_SPREAD` — one short option 1 % beyond the weekly range in the trend's
  direction, one long wing `wing_pct` further out, same expiry. Roles reuse `SHORT_PUT`/`LONG_PUT`
  (up) and `SHORT_CALL`/`LONG_CALL` (down). Wing before short on entry, short before wing on exit
  (the condor's rules, `02` §2.2).
* **Plan kinds:** `ENTRY`, `ADD` (the pyramid, new), `EXIT`. `ADD` opens no new position: it
  grows the open one's lots at the same strikes and expiry.
* **Exit reasons (`fo_position.closed_reason`):** `LEVEL_BREAK`, `DECAY_TARGET`, `LOSS_CUT`,
  `HARD_EXIT`, plus the book's `MANUAL`.
* **Data:** `fo_index_daily` (NIFTY, BANKNIFTY daily OHLC from Kite history, two years back,
  extended each evening); `op_index_minute` widened to `NIFTY BANK`; 75-minute bars are a pure
  aggregation of the minute bars (09:15, 10:30, 11:45, 13:00, 14:15).
* **Rules** live in `baskfy_core.fno.directional` and `04` §11; **numbers** in `F3Config`,
  every one listed in `04` §11 and `QUESTIONS.md` with its default.
* **Where it shows:** the desk's `/fno` (F3 block: today's direction, levels, the spread, the
  open position, the next rule), `GET /fno` JSON; the web `/options/overnight` gains an F3 card
  in a later leaf if time allows.

## Tree

- **F3-0** this plan, the docs, the decision record, the gates (`gates/f3-0-design.md`)
- **F3-1** the pure core: levels, direction, the 75-minute confirm, the intraday check, strike and
  expiry choice, sizing, the exit and add decisions, `F3Config` (`gates/f3-1-core.md`)
- **F3-2** schema and data: migration 0059 (sleeves `F3N`/`F3B`, group `F3`, structure
  `CREDIT_SPREAD`, kind `ADD`, `fo_index_daily`), the index history backfill and its evening
  extension, `NIFTY BANK` minutes (`gates/f3-2-data.md`)
- **F3-3** the EOD re-test of the daily rules on the option bhavcopy — what can be claimed and
  what cannot (`gates/f3-3-retest.md`)
- **F3-4** the worker's evening scan → `fo_scan` rows for F3 (`gates/f3-4-scan.md`)
- **F3-5** the desk: the monitor raises F3 entries in the window, watches the level, the decay
  and the cut every minute, raises `ADD`s the next session, sends exits under the flag or raises
  them for the click; `fno_execute` enters and exits a `CREDIT_SPREAD`; gates and the safety
  proof; the `/fno` page (`gates/f3-5-desk.md`)
- **F3-6** integration: suites, lint, docs, status, deploy paper-only, report
  (`gates/f3-6-integration.md`)

## Status log

Append-only.

- 2026-09-28 13:40 IST asked; three answers taken; the box measured: `op_index_minute` holds NIFTY 50 and INDIA VIX from 23 Sep only (no NIFTY BANK); `op_expiry` knows NIFTY weekly (Tuesdays) and BANKNIFTY monthly; `fo_underlying_daily` has no index rows; the option bhavcopy on the box is one day (25 Sep) — the re-test must fetch its own history.
- 2026-09-28 14:09 IST F3-0 met (2/2): the docs (01 §1c, 02 Track B, 03 §9, 04 §11, 06, DECISIONS-FO M.5, QUESTIONS Q10–Q12), root CLAUDE.md's third exception, the safety proof admitting the one name. F3-1 met (5/5): `baskfy_core.fno.directional` (49 tests) and `F3Config`; the F3 sleeves, group, structure and ADD kind in the core config; `BASKFY_FNO_F3_EXECUTION_ENABLED` in the core gating, the desk's gates and config. F3-2 met (3/3): migration 0059 (enum labels in an autocommit block; the labels cannot be dropped and the downgrade says so), `fo_index_daily` + `FoIndexDaily`, the worker's `fno.index_daily` (backfill/extend, Beat 18:15 behind `BASKFY_FNO_SCAN_ENABLED`, CLI `index-daily --from`), the collector widened to `NIFTY BANK`, the F3 seed row at ₹0, the API's F3 open-positions bound. F3-1 and F3-2 are one commit: the enum widening is both leaves' and neither is green without the other. Found on the way: `pyproject.toml` still listed `infra/laya` for mypy after LV10.3 removed it, so `make lint` was red on the committed tree; fixed here.
- 2026-09-28 14:25 IST F3-3 met (2/2): `baskfy_core.fno.directional_retest` (the daily half as a pure EOD proxy; 11 tests) and `tools/fno/f3_retest.py` over the 1,165 archived bhavcopies (offline through the provider, index rows cached beside the archive). Found on the way: NSE prints the index's final settlement *level* in an expired option's settle column, so expiry-day legs are valued at intrinsic and a settle above a quarter of the index is refused. Result, after costs: NIFTY n=206 −0.021R, BANKNIFTY n=166 −0.020R; gross near zero; the four intraday rules untested and named. F3 is not registered in the quarterly re-test's families (the worker's option loader drops the NIFTY weeklies); a raw-options loader is the follow-up.
