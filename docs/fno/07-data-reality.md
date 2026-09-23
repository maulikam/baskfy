# 07 — Data reality: what an overnight F&O book can and cannot be tested on

**Read before any backtest module (FO9).** Extends `docs/options/07` (which remains authoritative
for the intraday NIFTY sleeves O1–O3). The one fact that makes this pack different: **every sleeve
here decides at the close and holds across closes, so end-of-day prices are the right
granularity** — and NSE publishes them free, for every contract, including the expired ones.

## 1. What Baskfy has

| Data | Source | Depth | Good for |
|---|---|---|---|
| **NSE F&O bhavcopy**, one row per contract per day: OHLC, close, settle, OI, change in OI, volume, turnover; from 8 Jul 2024 (UDiFF) also the **underlying's price** and the **lot size** | `NSEProvider.fo_bhavcopy(on)` (added 23 Sep 2026 with this pack; archive-then-parse, the NSE limiter, cookie discipline) | Legacy layout back to at least **Jan 2022** (fetched and verified); UDiFF from 8 Jul 2024 | **Tier 2E** for every sleeve: entries at a close, exits at a later close, stops on daily highs and lows, and expired contracts included |
| Stock and index daily closes | `ohlcv_daily`, `index_snapshot_daily` | 2011→ | trend filters, the regime gate, realised volatility |
| The NFO master | `instruments("NFO")` → `op_contract` (OP2), **NIFTY rows only today** | from OP2 onward | FO2 widens it to every F&O underlying; the lot size, tick size and strike step of a *live* contract |
| Live quotes with depth and OI | Kite `quote()` (OP3's `option_quotes`) | now only | plans, the paper fill, the stop monitor |

The research fetch on 23 Sep 2026 read **every trading day from 3 Jan 2022 to 22 Sep 2026** through
the provider. The only misses were exchange holidays (the file 404s). A day is ~50,000 contracts
in the legacy layout and ~36,000 in UDiFF (after SEBI's Nov 2024 cut to one weekly index per
exchange). About 186 stocks had futures in mid-2023 and 210 do on 22 Sep 2026.

## 2. What the bhavcopy cannot tell you

* **The bid-ask spread.** A bhavcopy close is the last trade (or a volume-weighted average near
  the end of the session). For a stock option it may be from 11:40, and the other legs' closes may
  be from other minutes. The research therefore (a) prices a leg only if it traded that day, (b)
  clamps every vertical to its no-arbitrage bounds `[0, width]`, and (c) charges a slippage of
  `max(₹0.05, 3 % of premium)` **per leg per crossing**. That 3 % is an assumption, not a
  measurement. FO3's live collector exists to replace it with a measured number per underlying.
* **Where the stop would have filled intraday.** Futures stops use the daily high and low: a stop
  inside the range fills at the stop, and a gap through it fills at the open. That is exact for a
  resting SL-M and optimistic in fast markets.
* **Corporate actions inside a cycle.** A split or bonus re-cuts the strikes and lot size in the
  middle of a contract (JUBLFOOD, 1:5, Apr 2022 was the first one the research tripped on). A
  cycle whose future jumps more than 30 % in a session is excluded and counted, not priced. The
  live system reads NSE's adjusted contract from the master and never carries a pre-adjustment
  strike across the ex-date.
* **Point-in-time lot sizes before 8 Jul 2024.** The legacy file carries none. Brokerage per share
  before that date uses the symbol's median lot from the UDiFF era, which is a small cost term
  and labelled as an approximation.
* **Earnings dates.** No free, point-in-time archive of result dates is wired. So no sleeve in v1
  trades *around* an earnings date, and no backtest can claim to have avoided one (§4).

## 3. Physical settlement: the constraint that shapes every stock-option rule

**Stock options and stock futures settle by delivery**, not cash. An in-the-money stock option held
to expiry becomes a delivery of shares, and brokers raise margins on those positions through
expiry week. (Zerodha's published ramp is the reference, and FO0 re-verifies it before any rule
depends on it.) Two consequences are written into `04`:

1. **No stock-option or stock-futures position is held into expiry day.** Every such sleeve exits
   by the session before expiry (`E−1`), and the research exits there too.
2. **The research also tests exiting at `E−4`**, before the delivery-margin ramp, and reports how
   much of each sleeve's result that costs. The live default is whichever the numbers support, and
   it is recorded in `DECISIONS-FO`.

Index options and futures (NIFTY, BANKNIFTY and the rest) settle in cash. Those sleeves may hold
to expiry day, but still exit before the final settlement, for the same reason the intraday
sleeves do.

## 4. The evidence tiers for this pack

| Tier | Prices | What it may claim | Must say on the card |
|---|---|---|---|
| **1 — signals** | none, or the underlying only | how often a sleeve trades; the underlying's move after a signal | "No P&L. This measures selectivity, not profitability." |
| **2E — end-of-day observed** | the F&O bhavcopy: real closes of the real contracts, expired ones included; legs that did not trade are Black-76-modelled at the entry IV and **counted** | an expectancy in R, a win rate, a drawdown, per year | "End-of-day closes, not fills. Slippage is an assumed 3 % of premium per leg per crossing (0.03 % for futures), not measured. `n` legs were modelled because they did not trade." |
| **3 — observed live** | the FO3 collector's snapshots and the paper fills at the moments the sleeve actually trades | a measured P&L with measured spreads | the sample size and start date |

A Tier 2E number is **far better evidence than the intraday pack's Tier 2** (`docs/options/07`
§4), because the contracts' own prices are used, not a flat-IV model. It is still not a fill. No
Tier 2E number is shown without its caveat, and none shares a card with a Tier 3 number.

## 5. What is honestly unknowable in v1

* Whether the assumed 3 % slippage is right for each underlying's options. It is certainly wrong for
  the least liquid names, which is why the universe filter in `04` is a turnover floor rather than
  a list.
* How a sleeve behaves in a regime the 2022–2026 sample did not contain.
* Whether SEBI changes stock F&O eligibility or lot sizes again. The universe is read from the
  master each night, so the code survives a change, but a sleeve's sample may not.
