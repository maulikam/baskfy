# The kickoff prompt — the TW run

Paste the block below into a fresh CLI session at the repo root (`~/Documents/projects/baskfy`).
There is no docs pack yet — TW0 writes it, in the shape of `docs/swing/`, from
`research/tight-close/STRATEGY.md`. The run is resumable: a fresh session reads
`docs/twt/STATUS.md` and continues from the first module not marked ✅.

Maulik's decision, 11 Sep 2026, recorded here so the run does not re-litigate it: **no paper
phase — the sleeve goes live, ₹25 lakh, as soon as the tree is green and he has flipped the
flag himself.** The run builds everything with the flag off and writes the first-live-morning
runbook; it never flips the flag, never sets the capital, never places an order (CLAUDE.md
safety rails). The first ten live entries run at half size (the swing pack's §3 discipline, kept
because it costs nothing on a book that enters ~18 times a year).

---

```
This is the TW run: build TWT-1 — the three-weeks-tight position strategy
researched in research/tight-close/STRATEGY.md — into Baskfy as a third sleeve
beside the weekly momentum book and the swing book. Signals from the nightly
chain, plans from the desk, every order through /analyze → /execute confirm →
OrderGateway → GTT, a ratcheting trailing GTT, and a runbook for the first live
morning. The sleeve will trade real money at ₹25 lakh the day Maulik flips the
flag; you build it as if that is tomorrow, and you never flip it.

Read, in this order, before writing any code:
1. CLAUDE.md (the root working agreement — it governs this run unchanged)
2. docs/README.md
3. research/tight-close/STRATEGY.md   ← the strategy: scan, entry, exits, gate,
   sizing, the numbers, the caveats. It is the spec. §1's finding about
   Chartink's weekly look-ahead is why the signal is computed point-in-time.
4. research/volume-breakout/STRATEGY.md §1 and §6 (data handling, sleeve shape)
5. docs/swing/README.md, 02-scope-and-gating.md, 03-data-model.md,
   04-business-rules.md, 06-module-plan.md  ← the sibling sleeve; copy its
   shape, its gating and its GTT discipline (RAISE_GTT_STOP, the 15:15 sweep,
   SELL_AT_OPEN), not its rules
6. research/tight-close/tscan.py and research/volume-breakout/vbt/sim.py  ←
   the reference implementation of the signal and the backtest. Float,
   pandas, ignorant of law 1: re-implement, do not import.

TW0 — the pack, before any code. Write docs/twt/ mirroring docs/swing/: README,
01-method (from STRATEGY §2–3), 02-scope-and-gating (Track A/B/C as the swing
pack draws them; the real-money gate rewritten to Maulik's 11 Sep decision:
no DRY_RUN session count, a green tree + a written runbook + his flag flip,
and half size for the first ten live entries), 03-data-model (tw_* tables,
next free migration number), 04-business-rules (every threshold as a named
config field with its value — Close > 30, SMA50(volume) ≥ 10,000, three weekly
closes within 3.01 % with the current week's close = the latest daily close,
close ≥ 1.3 × the low of the calendar month three months back, entry = first
tight session after ≥ 5 sessions out, 20-day avg turnover ≥ ₹5 crore [note:
STRATEGY uses ₹2 cr; at ₹25 lakh and 10 slots a line is ₹2.5 lakh and the
1 %-of-turnover cap needs ₹2.5 cr, so ₹5 cr is the coherent floor and scored
22.5 % vs 20.9 % in the sensitivity table], breadth gate > 40 % of the
universe above its 200-DMA, next-open market entry, 20 % hard stop, 20 %
trailing stop off the highest high ratcheted after every close, 10 slots,
3 new entries a session, 1 % of turnover, 25 bps/side in the backtest; tests
pin these literally), 05-ui-spec, 06-module-plan TW1–TW10 with acceptance
criteria that are tests, STATUS.md, DECISIONS-TW.md, QUESTIONS.md (defaults
for what only Maulik can answer — capital ₹25 lakh, equal weight, 10 slots,
first-ten-entries-at-half-size). Commit as TW0 and continue.

The modules, in outline — 06 fixes the detail:
- TW1  pure core: packages/core/src/baskfy_core/twt/ — config; weekly closes
  (current partial week + two completed ISO weeks) and the month-3 low from
  daily bars; the tight state; the entry event (state today, false the five
  sessions before); the breadth series (share it with VBT-1 if that module
  exists, else build it here); sizing; the trailing-stop level. DataFrames
  in, DataFrames out; Decimal money; test_twt_purity.py green. Thin special
  sessions and the 10 % missing-bar tolerance from STRATEGY §1 as tests.
- TW2  goldens: reproduce research/tight-close/out/final_trades.csv (164
  trades) and final_metrics.json (CAGR 20.9 %, max DD −24.7 %) from the same
  bars to the tick, or explain every difference in DECISIONS-TW.md. Reproduce
  tscan_verify.py's result: point-in-time reading 64.9 % recall against
  chartink_backtest.csv, look-ahead reading 83.1 % — the test that proves the
  live signal is the point-in-time one.
- TW3  data model + migration: tw_state_daily, tw_signal_daily,
  tw_breadth_daily (or the shared one), tw_position (with high_since and the
  current GTT trigger and gtt_id), tw_order, tw_backtest_run (append-only).
- TW4  nightly job compute_twt after compute_swing, plus the 21:00 IST retry;
  neither can fail the night. Published-session clock only: the signal and
  the breadth reading come from the last completed session. The job also
  computes, for every open position, tomorrow's trailing trigger
  (0.8 × highest high since entry, on the exchange tick).
- TW5  the sleeve: its own cash (₹25 lakh is a setting Maulik enters, default
  0 → nothing plans), never sized against the whole account, never sells a
  holding it did not buy; 10 slots, 3 new a session, the turnover cap, the
  half-size rule for the first ten live entries (a counter in the sleeve, not
  a flag).
- TW6  desk plan: /analyze produces the sleeve's morning plan — next-open
  market buys for yesterday's signals (ranked by turnover, gate permitting),
  a GTT stop for every fill the same session (20 % under the fill),
  RAISE_GTT_STOP lines for every position whose trailing trigger rose, and
  SELL_AT_OPEN for nothing (there is no EOD sell rule in TWT-1; the GTT is
  the exit). /execute with confirm=true and the plan_id; plans expire in
  30 minutes; DRY_RUN=true simulates end to end. Everything through
  OrderGateway; client_id = plan_id:symbol. The GTT ratchet uses the swing
  book's delete-and-replace path and the 15:15 sweep re-arms anything naked.
- TW7  the fill-day rule: a fill whose low breaches the stop the same session
  is out (the backtest's stop_day0); a GTT placed after the fill covers it;
  the sweep asserts every open line has a resting GTT before 15:30.
- TW8  desk page + web page (read-only): today's tight names and which are
  entries, the breadth gauge and gate state, open lines with entry, highest
  high, current trigger, distance to trigger, the half-size counter; the
  backtest numbers with STRATEGY §5's caveats as a component.
- TW9  the backtest on the page from the plant's bars, appended to
  tw_backtest_run, drift > 1 CAGR point flagged.
- TW10 safety: with BASKFY_TWT_EXECUTION_ENABLED=false no TWT line reaches
  OrderGateway.place; the web app has no order route; no auto-execute flag
  exists for this sleeve and none is added; the weekly book, R1–R4 and the
  swing book are untouched; both still run. Then the runbook:
  docs/twt/FIRST-LIVE-MORNING.md — the exact sequence for the first real
  session (Kite login before 09:00, the token sync, the flag, the capital
  setting, /analyze, what the plan must show, /execute, the GTT check at
  09:20 and 15:15, what to do if a GTT is missing, how to stop the sleeve
  in one command) and the daily routine after it (login, ratchet plan,
  confirm, sweep).

Execute TW0 through TW10 in order under the Autonomy charter in CLAUDE.md:
run long, decide-record-continue, questions to Maulik are the exception.
- One commit per module: "TW<N>: green — <one line>". No module ends with
  either tree's test suite broken.
- Never place a live order. DRY_RUN=true in every environment you create.
  BASKFY_TWT_EXECUTION_ENABLED stays false; the sleeve capital stays 0; you
  flip neither. Maulik's "go live without paper" is his decision to execute
  by hand on the first morning, not an instruction to you.
- Track C is forbidden: no shorting, no MIS/F&O, no margin, no auto-execution,
  no web-app orders, no changes to the swing rules or the weekly book.
- Every threshold is a field of baskfy_core.twt.config, never a literal.
  Tests assert docs/twt/04, never current behaviour; never weaken a test.
- When the code and a doc disagree, the decision wins (CLAUDE.md, 9 Sep):
  git log -S first, fix the stale half, pin to the commit.
- Judgement calls → docs/twt/DECISIONS-TW.md, numbered, ⚠ UNREVIEWED, with
  alternatives and the reversal path.
- Anything only Maulik's hands can supply → NEEDS-MAULIK.md under a "TWT"
  heading. Known already, list them in TW0: the daily Kite login (tokens die
  ~06:00 IST, RUN-AND-TEST.md), the flag flip and the capital setting, and
  the plant's missing instrument-days (STRATEGY §1) which will make the live
  scan show fewer names than Chartink on some days.
- Update docs/twt/STATUS.md at the end of every module — loud about what is
  NOT done. If context runs long, write enough state there to resume
  losslessly, then continue.
- Deploy nothing. The box and tools/deploy/ are Maulik's; the run ends at a
  green tree, the runbook, and a report.

Stop only for the charter's stop conditions. When TW10 is green — or a hard
blocker ends the run — write TW-FINAL-REPORT.md at the repo root in the style
of SW-FINAL-REPORT.md: what was built, what was decided, what is NOT done,
what needs Maulik, and the first-live-morning runbook verbatim. Then ask
Maulik to review.
```

---

## If the run is resumed

Same prompt. The session reads `docs/twt/STATUS.md` and continues from the first non-green
module. Modules are finished, not restarted.
