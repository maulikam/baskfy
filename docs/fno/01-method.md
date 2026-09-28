# 01 — Method: what the FO run builds, and why it is so much less than was asked

Maulik asked for the opportunity across the F&O segment: stock options bought and sold, stock
futures, index futures and options, some held overnight. `RESEARCH.md` tested every one of those
families on 2022–2026 end-of-day data, and **all but one failed to beat their costs**. This file
builds what the evidence supports, and builds the machinery that lets the rejected families be
re-tested honestly as data accumulates, rather than building them anyway.

## §0 — The honest limit, first

SEBI's Jan 2023 and Sep 2024 studies of individual F&O traders found ~90 % lost money, with average
losses in lakhs. The research here agrees in its own terms. The two families that *feel*
profitable, selling stock premium (80 % of trades win) and buying options on breakouts (the
underlying often moves), are the two most clearly negative after costs. The one survivor has a
t-statistic of 1.36. **Nothing in this pack is a claim of profitability.** Every page says which
tier its number comes from (`07` §4).

## §1 — F1: the index monthly condor, carried overnight (paper)

**Thesis.** Index options have carried a small variance premium, and the monthly contract's
decay in its last three weeks can be harvested with both sides hedged, at a third of the cost that
stock options charge. This is `docs/condor/`'s thesis, stretched from one expiry day to fifteen
sessions. That is exactly what the intraday O1 cannot do, because O1 must be flat by 15:00.

**Honest limit.** As tested with Maulik's loss close (M.1): **+0.022R** per trade on 100 trades
(t = 1.01), worst trade −0.73R, max drawdown −1.35R, and slightly negative in 2022 and 2023. Without
the loss close it was +0.033R, the best of three entry days (`RESEARCH.md` §B4). The paper period
exists to find out whether it was luck.

| | |
|---|---|
| Underlyings | **NIFTY and BANKNIFTY monthly expiries** (cash-settled). Each is its own position; at most one open per underlying |
| When | Entry at the session **15 exchange sessions before** that monthly expiry (`fo_f1_entry_sessions_before`), plan raised at 09:20 and confirmable 09:20–10:30 |
| Gate | **No IV filter**: F1 trades the rule that was tested, unconditionally. IV ÷ RV20 is recorded on every plan so the forward sample can test the in-sample observation (> 1.3: +0.117R on 24 trades) without having selected on it. No event day inside the hold unless Maulik accepts it on the plan (the options pack's `op_event_day`). The book is not paused |
| Structure | Iron condor on the monthly: shorts at the listed strikes nearest F·(1 ± 1.0·IV·√T), wings a further 0.5σ out, equal quantity. **Longs enter first**; the covered-overnight guard re-proves every prefix (`02` §2.1) |
| Manage | Checked by the monitor during the session and at each close. **Take profit** when the structure can be bought back for ≤ 50 % of the credit. **Loss close** (Maulik, M.1): when the cost to close reaches 2.5 × the credit (a loss of 1.5 × the credit), close the whole structure, shorts first. **No defensive exit on a breached short strike**: it was tested and cut expectancy to +0.018R while barely moving the tail (`RESEARCH.md` §B4). The wings bound the loss if the loss close cannot fill (a gap overnight) |
| Exit | Otherwise at **E−1, 15:00**, never into expiry day (`02` §2.2) |
| Size | Capital **₹25 lakh** for F1 (Maulik, M.2, raising M.1's ₹10 lakh) × 1.0 % = ₹25,000 risk per structure, ÷ the structure's max loss per lot (credit subtracted), floored, max `fo_max_lots`. Margin from `basket_order_margins` is a ceiling only. Paper sizes exactly as live would, journalled `simulated=true` |

## §1b — F2: stock-futures breakout, long only, carried overnight (paper, by Maulik's choice)

**Why it is built although the research rejected it.** Maulik chose it with the numbers in front
of him (M.1, QUESTIONS Q4). The pack builds it faithfully, and says on every surface what the
research found.

**Honest limit, re-costed with its rolls** (M.1): +0.017R per trade (t = 0.71, n = 2,334), win
39 %, and **negative in 2022, 2024, 2025 and 2026**. The whole result is 2023's mid/small-cap
rally. Expect the paper period to lose unless the tape looks like 2023.

| | |
|---|---|
| Universe | F&O stocks in the top 60 % by 20-session median futures turnover; not in the ban list; no `ca_flag` in the last 5 sessions |
| Signal (at the close) | Continuous futures close > the highest of the prior 20 closes **and** > its 50-session average, **and** the NIFTY future > its own 50-session average |
| Entry | Plan at 09:20 the next session, confirmable 09:20–10:30; buy the **near-month** future (the next month if the near one expires within 3 sessions), NRML |
| Stop | 3 × ATR14 below entry, trailing on each close to (the highest close since entry − 3 × ATR14 at entry), never loosened. It rests broker-side as a **GTT on NFO/NRML**, modified each evening (non-negotiable 4, M.1). If `stop_from_vol()` gives a tighter stop, the GTT uses that |
| Roll | At 15:00 on E−1 of the held contract: sell it and buy the next month, as **one plan under the original confirm** (a mechanical calendar roll, not a new entry; `02` Track C §5). The GTT moves with it |
| Exit | The stop; or 40 sessions after entry at 15:00; or a pause |
| Size | Capital ₹0 (Q1 was asked for F1 only), so paper runs **one lot** and live refuses `NO_SLEEVE_CAPITAL`. Note: one lot's 3-ATR risk is often ₹30,000–₹80,000, above the ₹25,000 per-trade ceiling, so live sizing would refuse most names at that ceiling (`04` §10) |
| Concurrency | At most `f2_max_open` (5) positions, one per stock, and at most 2 per sector (the NSE industry of the cash instrument) |

## §1c — F3: the directional index credit spread (paper; Maulik, M.5, 28 Sep 2026)

**Why it is built.** Maulik brought the method and asked for it to be coded (M.5): sell index
options in the direction of the market, direction read from daily support and resistance,
confirmed on the 75-minute chart, aligned with the intraday trend; strikes about 1 % beyond the
weekly candle's high or low; target ~80 % premium decay; out at once when the level breaks; a
first entry of 20-30 % of capital and an add the next session only if the trade is working;
about 1 % a week. His three answers to the questions the charter forced: **a credit spread with
a far wing** (never naked, `02` §2.1 stands), **auto-exit under a new flag** (the level-break
rule is his non-negotiable, so the monitor may send the exit itself when he turns the flag on;
entries and adds stay clicks), and **NIFTY on the weekly expiry, BANKNIFTY on the monthly**.

**Honest limit, first.** The method's edge, as he describes it, is intraday discipline: the
75-minute confirm, the intraday alignment and the immediate cut on a level break. None of these
is in a bhavcopy, so the EOD re-test (F3-3) tests only the daily half - levels, direction and the
next-session entry at settle - and says so. Paper first, like F1 (`02` §3).

**What the EOD proxy found (F3-3, 28 Sep 2026; `evidence/f3-retest.md`).** Over 1,165 sessions
2022-01-03 → 2026-09-22 the daily half alone loses after costs on both underlyings: NIFTY
n = 206, −0.021R (t = −2.26, 67 % win); BANKNIFTY n = 166, −0.020R (t = −1.60, 57 % win); gross of
costs near zero. The decay-target wins are small and the level breaks and loss cuts, exited at
the settle, give back more. Whatever edge the method has must come from the intraday rules the
closing file cannot test. Nothing in it is a reason to turn F3 on, and nothing disproves the
intraday half; the paper period is the test.

| | |
|---|---|
| Underlyings | NIFTY (`F3N`, weekly expiry) and BANKNIFTY (`F3B`, monthly expiry) |
| Levels (at the close) | Pivot highs and lows over the last `f3_pivot_lookback` sessions, a pivot being an extreme with `f3_pivot_width` lower highs (higher lows) either side. **Support** is the highest pivot low below the close; **resistance** the lowest pivot high above it. The **weekly range** is the last `f3_weekly_sessions` sessions' high and low |
| Direction (at the close) | **UP** when the close is above its `f3_trend_sessions` average and above support; **DOWN** when below the average and below resistance; otherwise **NONE**. The 75-minute confirm: the last completed 75-minute bar's close above (UP) or below (DOWN) the average of the last `f3_confirm_bars` 75-minute closes. Disagreement is NONE |
| The key level | Support when UP, resistance when DOWN. The trade is over the moment the index trades beyond it by `f3_level_buffer_pct` |
| Entry | Plan at 09:20 the next session, confirmable 09:20-10:30 (`04` §1's window). The intraday check at plan time: the index above the session's open (UP) or below it (DOWN), and the level intact. UP sells a **put** at the weekly low × (1 − `f3_distance_pct`), DOWN a **call** at the weekly high × (1 + `f3_distance_pct`), rounded to the strike step *away* from the index; the wing sits `f3_wing_pct` further out. Wing first, short second (`02` §2.2) |
| Expiry | NIFTY: the nearest weekly with at least `f3_min_sessions_weekly` sessions left, else the next. BANKNIFTY: the current monthly with at least `f3_min_sessions_monthly` sessions left, else the next month |
| Size | The first entry spends `f3_entry_share_pct` of the sleeve's capital as **max loss** (the spread's width × lot − credit, which is also its margin under the exchange's spread benefit), bounded by `04` §3's risk budget and `fo_max_lots`; zero lots is `REJECTED_SIZE`. Capital is ₹0 until Q10 is answered, so paper runs one lot |
| Add | The next session and later, in the entry window, only if the mark has decayed at least `f3_add_working_pct` of the credit, the direction still reads the same and the level is intact: the same lots at the same strikes and expiry, until the position's max loss reaches `f3_full_share_pct` of capital. Never an add to a loser (Track C §5 is kept, not widened) |
| Exit, in this order | **LEVEL_BREAK** - the index beyond the level by the buffer, checked every minute, sent at once; **LOSS_CUT** - the spread's mark at or above `f3_loss_cut_mult` × the credit ("sell 20-30 and cut at 50"); **DECAY_TARGET** - the mark at or below (1 − `f3_decay_target_pct`) × the credit; **HARD_EXIT** - `fo_hard_exit_time` on the expiry day, because an index option settles in cash and the last hour is gamma, not theta. Short first, wing second |
| Concurrency | One open F3 position per underlying |

## §2 — FX: the F&O data layer and the re-test engine (build)

**Why it is built although it trades nothing.** Every rejected family in `RESEARCH.md` was
rejected on 4¾ years with an *assumed* slippage. Two things can reverse a verdict: more years, and
measured costs. The data layer supplies both.

* **Nightly ingest**: `fo_contract_daily` from the F&O bhavcopy (one NSE request a night), and
  `fo_underlying_daily` derived from it (continuous futures levels, ATR, OI, IV, RV, basis, the
  ban list, the corporate-action flag).
* **Quarterly re-test** (FO9): the `RESEARCH.md` families, re-run as pure functions over the
  growing table, written to `fo_backtest_run` with tier and caveat, shown on the page. A family
  positive in three consecutive re-runs becomes a `DECISIONS-FO` entry *for Maulik*. Nothing
  switches itself on.
* **Measured spreads** (FO3): at 15:00 each session, one Kite `quote()` over the ATM ± 3 strikes
  of the top 30 stock-option underlyings' near monthly (≤ 500 keys, one call). The per-leg
  half-spread it measures replaces the assumed 3 % in the next re-test.

## §3 — The Stock F&O page: information, never signals (build)

A read-only section on the Options tab (`05`). Per underlying: IV vs RV20, IV percentile over a
year, futures OI change, basis, days to expiry, F&O ban, and lot size. It is sortable, and on
purpose **it has no "candidate", "buy" or "sell" column**. `RESEARCH.md` found that none of these
numbers predicts a profitable trade at retail costs, and a page that ranks them as if they did
would be the misleading surface CLAUDE.md warns about. The page says that in one line at the top
and links the research.

## §4 — What is not built, and what would bring each back

| Family | Status | Comes back when |
|---|---|---|
| Stock futures trend, long only | **built as F2, paper** (Maulik, M.1) | — |
| Stock futures trend, short side and OI-filtered | not built | a quarterly re-test positive 3× running, **and** Maulik's decision |
| Stock condors / credit spreads | not built | measured spreads bring cost below gross edge in the re-test |
| Directional long options / debit spreads | not built | a signal first shows a positive *underlying* move after costs |
| Cash-futures carry | not built | median net basis above the liquid-fund rate for a quarter |
| Hedging the equity books with index futures | not built | QUESTIONS Q5 |
| Covered calls on the holdings | impossible | a holding enters the F&O list (none of the 20 has) |

**Sleeve codes** for `fo_sleeve`: `F1N` (NIFTY), `F1B` (BANKNIFTY), `F2`, and since M.5 `F3N`
(NIFTY weekly) and `F3B` (BANKNIFTY monthly). Flags are grouped
as `F1`, `F2` and `F3`.
