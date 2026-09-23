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

**Honest limit.** +0.033R per trade on 100 trades (t = 1.36). It was the best of three entry days,
and ~0 in 2025 and 2026 (`RESEARCH.md` §B4). The paper period exists to find out whether it was
luck.

| | |
|---|---|
| Underlyings | **NIFTY and BANKNIFTY monthly expiries** (cash-settled). Each is its own position; at most one open per underlying |
| When | Entry at the session **15 exchange sessions before** that monthly expiry (`fo_f1_entry_sessions_before`), plan raised at 09:20 and confirmable 09:20–10:30 |
| Gate | **No IV filter**: F1 trades the rule that was tested, unconditionally. IV ÷ RV20 is recorded on every plan so the forward sample can test the in-sample observation (> 1.3: +0.117R on 24 trades) without having selected on it. No event day inside the hold unless Maulik accepts it on the plan (the options pack's `op_event_day`). The book is not paused |
| Structure | Iron condor on the monthly: shorts at the listed strikes nearest F·(1 ± 1.0·IV·√T), wings a further 0.5σ out, equal quantity. **Longs enter first**; the covered-overnight guard re-proves every prefix (`02` §2.1) |
| Manage | Checked at each close and each morning. **Take profit** when the structure can be bought back for ≤ 50 % of the credit. **No defensive exit on a breached short strike**: it was tested (`RESEARCH.md` §B4) and cut expectancy from +0.033R to +0.018R while moving the worst trade only from −1.02R to −0.95R. The wings are the stop (PACK.3) |
| Exit | Otherwise at **E−1, 15:00**, never into expiry day (`02` §2.2) |
| Size | Risk budget ÷ the structure's max loss per lot (credit subtracted), floored, max `fo_max_lots`. Margin from `basket_order_margins` is a ceiling only. Paper: one structure |

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
| Stock futures trend (long, short, OI-filtered) | not built | a quarterly re-test positive 3× running, **and** Maulik's decision |
| Stock condors / credit spreads | not built | measured spreads bring cost below gross edge in the re-test |
| Directional long options / debit spreads | not built | a signal first shows a positive *underlying* move after costs |
| Cash-futures carry | not built | median net basis above the liquid-fund rate for a quarter |
| Hedging the equity books with index futures | not built | QUESTIONS Q5 |
| Covered calls on the holdings | impossible | a holding enters the F&O list (none of the 20 has) |

**Sleeve codes** for `fo_sleeve`: `F1N` (NIFTY), `F1B` (BANKNIFTY). Their flags are grouped as
`F1`.
