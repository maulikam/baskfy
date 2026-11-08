---
name: data-date-clocks
description: Which date or clock each Baskfy surface shows (last completed session vs live Kite overlay), and what was tried and reverted. Read before changing as_of, the freshness pill, live overlays, publish timing, or anything that looks like the data date is wrong.
---

## Which date the product shows, and why it is not today (settled 9 Sep 2026)

**Read this before "fixing" the data date. It has been asked three times in one week and twice
led to a change that had to be reverted.**

The product serves **two different clocks** and they are both correct:

| Surface | Clock | Why |
|---|---|---|
| Baskets, factors, market health, the screener's ranks and patterns, the freshness pill's `as_of` | The **last completed trading session** | A daily bar is a *closed* day. House rules 5 and 7: point-in-time, idempotent. There is no "today" bar until today ends |
| The **price column** on the four screen pages — screener, `/swing`, `/vbt`, `/twt` | **Close, plus live overlay 09:15–15:30 when the API has a Kite session** (the market-data switch is `BASKFY_LIVE_QUOTES`, default on — never `DRY_RUN`, which governs orders; LV1.1, 27 Sep 2026) | Maulik, in session, 21 Sep 2026: "screens should have live data". `/meta/live-marks` returns last price + % today, polled every 30 s; ranks and `as_of` do not move. Outside hours or without a session the cell is the close and the header says why (`docs/DECISIONS-MERGE.md`, last entry) |
| The **swing book's** setups and triggers, on the desk — and, since SW30 (29 Sep 2026), the **gate's index half** | **Live, from Kite quotes** (the gate: EMA(10)/EMA(20) with today's quote over the last 249 published closes, 09:15–15:30 — SW31; breadth stays at the last close) | These are prices, not bars, and a quote is available whenever the market is. The published `sw_market_daily` row is still the closed day's |
| The **web app's** portfolio marks — net worth, today's P&L, every holding's value | **Close, plus live overlay when a Kite session exists** | `ohlcv_daily` closes are the base; `live_prices.py` overlays Kite `last_price` when a live broker read is available (M82). Without a Kite session the page stays on the last close |
| The **options tab** (`/options`, OP5): scan states, candidates, the chain, IV/greeks | **The options collector's latest minute** (`op_scan.as_of_minute`) while the NSE session is open — labelled `Live · 13:14`, amber `stale` beyond two minutes; outside the session **the last scanned session's final minute** — `As of close, Mon 21 Sep · market closed`. Paper marks: the desk's persisted mark (`Marked 13:14:30`). Journal and backtests: closed records, dates only | The collector is the one reader of the chain; the page never calls Kite for an option price (`docs/options/05` §2). The only live overlay on the tab is the shared `useLiveMarks` read on the two header index levels (NIFTY 50, India VIX), whose base is the collector's minute bar or the last daily close (DECISIONS-OP OP5.3). With the collector off — its default — there are no scan rows and the tab says so |
| The **Overnight** page (`/options/overnight`, FO5): F1's scan state, the proposed condor, IV/RV; F2's candidates; F3's night per underlying and its proposed spread (F3-7) | **The last completed session's F&O bhavcopy** (`fo_scan.trade_date`) — labelled `As of close, Tue 22 Sep`. Open structures and F2 positions: **that session's settle** (`fo_mark`) — `Marked at settle, 22 Sep`. The **only** live number is the underlying's index level, from the shared `useLiveMarks` overlay during market hours with a Kite session — `NIFTY live 13:14`; otherwise the level the bhavcopy printed. Journal and re-tests: closed records, dates only | The scan reads the file after the close (`docs/fno/05` §2). **The page never prices an option live**: live leg prices exist only on the desk's `/fno` (and its staff web view `/options/desk`, DESK-UI), where they decide the plan, and the page says so |
| The **Stock F&O** page (`/options/fno`, FO5): `04` §5's table, the verdict table, the spread-sample count | **The last completed session's bhavcopy** and the series derived from it (`fo_underlying_daily`) — `As of close, <date>`, every column. `05` §3 *allows* the price column the four-screen-pages overlay; the column is the **futures settle**, and a cash last price is not a settle, so FO5 overlays nothing (DECISIONS-FO FO5.5) | Facts after the close, never a candidate (PACK.8); no column is named or coloured as a signal |

⚠️ **THE WEB APP'S PORTFOLIO-MARKS ROW ABOVE WAS WRONG UNTIL 11 Sep 2026, and it was wrong in the direction that matters.**
It read *"Portfolio marks and holdings, the swing book's setups and triggers | Live, from Kite
quotes"*, which is true of the swing book and has **never** been true of the web app. `get_ltp`
and `get_quotes` appear only in `kite-momentum-rebalancer/app/` — the desk's monitor — and
`git log -S "get_ltp" -- services/api` is empty: the API has never had one, not once, in any
commit.

The cost of the error was not theoretical. An agent read this table, told Maulik that his
portfolio screen was showing live quotes, and he had to correct it. **A working agreement that
misstates where a money figure comes from is worse than one that says nothing**, because it is
believed. Fixed by splitting the row, and the correction stays here rather than being tidied away.

**Decision restated 12 Sep 2026 (audit 4.1):** after M82 the web portfolio *does* overlay live
Kite marks when a session exists. The clock is therefore **"close, plus live overlay when a Kite
session exists"** — not "always live" and not "close only". The freshness pill, live-refresh copy
and this table must say exactly that. The 11 Sep correction above remains: do not claim live when
there is no Kite path.

So on a weekday at 13:15 the pill's published `as_of` still reads **yesterday** (last completed
session). Portfolio marks may additionally show a live overlay when Kite is connected. Baskets,
factors and market health stay end-of-day. **Do not make the published series claim today while
today is still running.**

### What was tried and reverted, so nobody tries it again

* **M88 (8 Sep 2026)** moved the publish cutoff to 15:45 because Kite returned that day's bars
  for RELIANCE, TCS and INFY five minutes after the close. Three of the most liquid symbols in
  India are not evidence about a 4,855-instrument universe: Kite's `historical_data` covers
  ~3,000 of them and the rest arrive only in NSE's bhavcopy. The early run wrote a **partial**
  day, the quality gate refused it three times, and the change was reverted the next morning.
  `baskfy_worker.catch_up.SESSION_DATA_READY_IST` carries the numbers.

### What IS legitimate to improve

* Making a *label* say what it means — the freshness pill appends "· market open" during a
  session and its tooltip names which surfaces are live (9 Sep 2026).
* Getting the published day out **sooner after the close**. M90 did this properly: the bhavcopy
  leads, so the chain takes ~14 minutes instead of 73. Earlier is good; *earlier than the day's
  end* is not.
* Widening what counts as live where a quote genuinely answers the question — M91 lets a login
  at any hour after 09:15 scan today, because after the close a quote *is* the day's final
  price. Before 09:15 it is yesterday's, and stamping that as today would be a lie.
