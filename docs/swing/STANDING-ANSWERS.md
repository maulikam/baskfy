# STANDING-ANSWERS — Maulik's decisions for the rest of the SW run (2 Sep 2026)

**Read this before raising any AskUserQuestion.** If a question is answered here, apply the
answer, record it in `DECISIONS-SW.md` as "Maulik, 2 Sep 2026 (STANDING-ANSWERS §n)", and
continue without asking. If it is not answered here, use the pack's default (`04`, `07`) and
decide-record-continue. **Ask Maulik only for:** a Kite credential; a schema change that loses
data; anything touching the weekly momentum book or the R1–R4 overlay; a Track C boundary
(`02`). Everything else is yours.

## A. Already decided in conversation (apply as stated)

| # | Question the run asked | Decision |
|---|---|---|
| A1 | Risk per trade to start | **0.5%** (`sw_config.risk_per_trade_pct`); ceiling stays 1.0% |
| A2 | Push channel for the 09:31 signal | **Email via the existing mailer now**, one-way, carrying the whole line (symbol, entry, stop, qty, ₹ risk, plan expiry). Scaffold a Telegram sender **dark** behind `BASKFY_SWING_TELEGRAM_BOT_TOKEN` / `_CHAT_ID`. **Never a confirm path** — a reply or tap must not place an order |
| A3 | D10 catalyst / news | **Free NSE corporate-announcement feed**, watchlist + EP-candidate symbols only, via the existing NSE provider and its limiter; store headline + timestamp + filing URL; auto-fill `sw_watch.catalyst`; link out, do not reproduce text; add an earnings-date flag from the results calendar; **fail soft** (empty catalyst allowed, a crashed pre-open scan not); record the Track C §7 amendment |
| A4 | S2 Kite timing questions | Build `tools/swing/kite_timing_probe.py` as a one-shot Beat task (09:04 IST weekdays, gated by `BASKFY_SWING_TIMING_PROBE=true`, self-disables after one good run, writes `docs/swing/status/S2-kite-timing.md`). Meanwhile: **gap % at 09:09 from `ohlc.open`** (the pre-open LTP updates once at 09:07–09:08; `volume` then is one auction print), **volume pace from 09:16 off the ticker**, and the **5-minute opening range is built from `TickBus` ticks** (Zerodha: the historical API "was never built for polling during market hours"); the historical minute candle is fetched a minute later only to reconcile |
| A5 | Two SIGNAL confirms overshoot the exposure ceiling | Fix **at confirm** in `/swing/execute` under a row lock on the day's `sw_session`: re-derive open exposure at cost + today's CONFIRMED/SENT/FILLED lines + cash; re-size or refuse (`EXPOSURE_FULL` / `TIER_FULL` / `SESSION_CAP`). Also re-read context per trigger so the page shows what will be sent. Property test: no confirm sequence exceeds ceiling, count or cap; the drill shows 25%, not 34% |
| A6 | SW9.5 ordering | Apply `07-primary-source-corrections.md` (patch in `patches/`) **before finishing SW10**, one commit |
| A7 | Live gap at 09:09 with no stop | **Keep no-stop.** Show it in the MORNING plan as a `PENDING_RANGE` line: no qty, no stop, not confirmable, reserves one of the session's new-entry slots, information-only preview at a 1-ADR stop. Becomes a real line only via the SIGNAL plan with `stop = min(range low, LOD)`; wider than 1 ADR → `STOP_TOO_WIDE`, slot released; unreleased slots freed at 10:45. Test: a `PENDING_RANGE` line can never reach `/swing/execute` |
| A8 | Unfilled LIVE buy | **Keep "GTT for the filled quantity only".** Buy as a **marketable LIMIT** `min(trigger × 1.005, range high + 0.25 ADR)`, never MARKET. Poll the order ≤ 10 s (≤ 2 req/s): COMPLETE → GTT + `sw_position` in the same request; partial/open → return SENT with filled qty, GTT for that qty if > 0. Later fills via `on_order_update` raise the GTT quantity (modify, never a second GTT). **Cancel any open remainder at 10:45.** 15:15 sweep: no filled qty without a GTT (`SWING_POSITION_NAKED`), re-arm if so |
| A9 | First five live sessions | **Half RISK at plan time** (`risk_multiplier=0.5` before `size_position`), never quantity at send time; SELL / RAISE_GTT lines are always full size; `first_live_sessions_left` persisted in `sw_config`, decremented once when a LIVE `sw_session` closes; plan header shows "first live sessions: N left · risk 0.25%"; journal tags those trades |
| A10 | Ladder settlement | **Real closes from day one** (no paper book; PACK.6 paper clause void; rung starts at 0). One idempotent, date-bound settlement at 21:05 after `manage` and fill reconciliation; the 09:09 job runs it **only as a catch-up** when no record exists for the previous session |
| A11 | Going live | **We go LIVE after the code work is complete.** `02` §3.2's 20-session paper gate is replaced by: one DRY_RUN morning through SW10's full drill on a real session, the backtest on the page, and Maulik's written risk decision (0.5%/trade, half risk for five sessions, 15%/10% drawdown lock-out, rung 0). Rewrite `02` §3 accordingly. The flag is still flipped by Maulik's hand, never by the run. **Do not ask about paper-vs-live again** |
| A12 | Backtest frame | **Constant ₹10 lakh sleeve + the index rule.** NIFTY 500 from `index_snapshot_daily` (NIFTY 50 fallback), 10/20 SMAs in-frame, no look-ahead. Drawdown lock-out on the constant-sleeve equity curve (realised + open marked at close). Report **gate-on vs gate-off** per year and per setup, with breadth's and the index rule's contributions labelled separately |
| A13 | Backtest fills | **Keep both:** stop-out at the stop, or at the open when the day gapped through it, except the entry day (always the stop). EP entered the day **after** its gap day: next open if ≥ trigger, else at the trigger if the high reaches it |
| A14 | Watchlist funnel and expiry | **Auto-watch the top 20 `SETTING_UP` flags by score + every EP** (the weekly focus list). The monitor watches **all** of them on the `TickBus`. Attention is what is funnelled: **daily focus = top 5 by score + every EP** → push (email) + top of the desk page; the other 15 → signals still raised and logged to `sw_signal`, shown below the fold, never pushed (they feed the journal's "missed setups" analysis). The 3-entries-per-session cap and the confirm step limit what is acted on. **Expiry:** DETECTOR flags after 10 sessions or on trigger; **MANUAL rows also expire after 10 sessions unless re-confirmed** on the watchlist page (a two-week-old typed pivot is stale; MANUAL levels are not refreshed premarket) |

## B. Standing defaults for the rest of SW9–SW12 (do not ask; apply)

**Backtest (SW9)**
- B1 Costs: 0.13% per side (`04` §11), read from params. Circuits: use `ohlcv_daily.upper_circuit` where present; where absent (pre-2020) assume no lock and **say so** on the page.
- B2 Calendar: only sessions the trading calendar names; a bar on an unnamed day is never traded. Delisted names: sold at the last close, flagged `DELISTED` in the funnel.
- B3 Partial: ⅓ at the next open after the day-3–5 signal; trail exits at the next open after the close below the MA. Same `stops.manage`, same precedence — never a backtest-only exit rule.
- B4 Report: R-distribution histogram, win rate, expectancy, profit factor, max drawdown of the constant-sleeve curve, by setup and by year; funnel counts (universe → liquid → candidates → lined → skipped by reason). Caveats verbatim from `04` §11 plus B1's circuit caveat. Runtime target < 30 min; if it needs sampling to fit, sample **dates**, not names, and say so.
- B5 Params stored on `sw_backtest_run`; the page card names them; two runs with the same params and data version produce identical stats (test).

**Safety proof (SW10)**
- B6 Every claim in `02` Track B/C is a test, including: `PARABOLIC_SHORT` never a line; no web route reaches execution; flag-off never reaches a non-dry-run adapter; a SELL never exceeds `quantity_open`; a stop never falls; the monitor strategy has no `place` call; A5's confirm-time property; A7's `PENDING_RANGE` property; A8's partial-fill sequence.
- B7 The drill (`tools/swing/drill.py`) covers: premarket → replayed morning (one flag break, one EP, one locked circuit, one late partial fill) → two confirms → 10:45 sweep → 15:15 sweep → EOD → next morning's plan. It must print the `sw_session` counters and end with **0 orders reaching a broker**.

**Hardening (SW11)**
- B8 Alerts: `SWING_POSITION_NAKED`, `SWING_MONITOR_DID_NOT_START`, `SWING_DETECT_STALE`, plus `SWING_ORDER_OPEN_AFTER_CUTOFF` (an open remainder past 10:45) and `SWING_GTT_MISSING_AT_1515`. Runbook 6 entries for each. Spans optional and unable to raise into the order path.
- B9 Budgets: `/swing/setups` p95 < 300 ms; tick→verdict < 5 ms; detect < 3 min for 2,500 names; the confirm path (lock + re-size + place + GTT) < 2 s in DRY_RUN. Record in `benchmarks/AS-MEASURED.md`.
- B10 Kite limits are hard constraints: quote 1 req/s and ≤ 500 instruments/call; historical 3 req/s; orders 10/s, 400/min. The monitor polls quotes at most once per 5 s; anything faster is a bug.

**Verification and hand-off (SW12)**
- B11 Goldens for `detect_setups`, `size_position`, `manage`, `exposure_tier`, `build_entries`, `evaluate_trigger` into `go/testdata/golden/L1/swing/`; byte-stable across two runs; a line in `docs/go-rewrite/REQUESTS.md`.
- B12 `SW-FINAL-REPORT.md` includes, verbatim, the rewritten `02` §3 checklist with evidence per item, the exact steps for the **one DRY_RUN drill morning**, and then the exact steps for the **first live morning** (login before 09:00, sleeve capital set, flag flip, what the 09:09 / 09:16 / 09:20 / 10:45 / 15:15 / 21:05 jobs will do, what to watch on the desk page). `NEEDS-MAULIK.md` gets a Swing heading listing only what needs his hands: sleeve capital, the flag flip, the Telegram token, the S2 probe morning.

**General**
- B13 Every threshold is a `baskfy_core.swing.config` field or an `sw_config` column; a literal in a task, router or page is a defect.
- B14 Money is `Decimal`; prices round at write time; levels leave core adjusted and are converted by `adj_factor` in the task.
- B15 Nothing in this run reads or writes the weekly book, its plans, its GTTs or R1–R4. A shared helper that would need to is a stop condition, not a shortcut.
- B16 If two answers here conflict, the later letter wins; if this file conflicts with `07`, `07` wins on numbers and this file wins on process.

## C. If you still need to ask

Write the question, the options and your recommendation to `docs/swing/QUESTIONS.md` (append, dated), pick your recommendation, record it as ⚠ UNREVIEWED in `DECISIONS-SW.md`, and continue. Maulik reviews the file, not the terminal.
