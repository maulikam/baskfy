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

**Sleeve codes** for `fo_sleeve`: `F1N` (NIFTY), `F1B` (BANKNIFTY) and `F2`. Flags are grouped
as `F1` and `F2`.
