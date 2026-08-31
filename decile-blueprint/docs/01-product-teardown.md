# 01 — Product teardown: momoindiascreener.in

Everything below was observed directly from the running application on 19 Aug 2026 (logged-in
session, screen id 1792, "Investing 001"). Where a formula is stated as **VERIFIED**, it was
confirmed numerically against live values from the site. Where it is stated as **INFERRED**,
it is a best-fit reconstruction that must be calibrated against real data during the build.

---

## 1. Information architecture

Authenticated sidebar navigation:

| Route | Purpose |
|---|---|
| `/dashboard` | Indices dashboard — ~145 NSE indices with %chg, level, PE, PB, Div Yield, sorted by %chg desc |
| `/market-health` | Breadth metrics per universe (above 200/50 DMA, within 10% of ATH, 1Y return > 0) |
| `/screens` | List of "Example Screens" (6, read-only templates) and "Your Screens" |
| `/screens/:id/edit` | The screener — filter form + live results table |
| `/screens/:id/columns/edit` | Column picker for that screen's results table |
| `/screens/:id/csv` | CSV export of current results |
| `/instruments/:symbol` | Single-stock factsheet |
| `/rebalance-tracker` | Upload a portfolio CSV, diff against a screen → Exits / Inside-WRH / Entries |
| `/listings` | All NSE listed securities by listing date (3,524 rows, paginated 100/page) |
| `/pricing`, `/invoices`, `/profile`, `/change-password` | Account & billing |
| `/faq`, `/support`, `/blog`, `/ama-recording`, `/inspire` | Content |
| `/about`, `/privacy-policy`, `/refund-policy`, `/terms-conditions` | Legal / marketing |
| `/december-2026-update` | Roadmap / pricing-change announcement page |

Business model: Monthly ₹500 · Yearly ₹3,999 · Forever ₹14,999 (rising to ₹899 / ₹5,999 /
₹19,999 in Dec 2026). Gated features: export, custom columns, historical ranks, community
Slack, AMAs. Explicit "not a SEBI registered advisor" disclaimer.

---

## 2. The screener form — complete field inventory

Observed DOM field names are given because they are the cleanest available spec of the
persisted screen object.

### 2.1 Core (always visible)

| Field | Control | Values |
|---|---|---|
| `index` | select | NIFTY 50, NIFTY NEXT 50, NIFTY 100, NIFTY 200, NIFTY 500, NIFTY TOTAL MARKET, NIFTY LARGE MID 250, NIFTY MIDCAP 150, NIFTY SMALLCAP 250, NIFTY MICROCAP 250, NIFTY MID SMALL 400, NIFTY FNO, All NSE Listed Stocks (`nifty-allcap`), All NSE Listed ETFs (`etf`) — **plus Baskfy's NSE SME (Emerge) (`nse-sme-emerge`), M59**, which the reference product does not have |
| `sort_by` | select | 62 factors — see §3 |
| `sort_direction` | select | Highest to Lowest \| Lowest to Highest |
| `Show More Filters` | switch | reveals everything below |

### 2.2 General Filters

| Field | Control | Notes |
|---|---|---|
| `name` | text | screen name |
| `apply-filters-on` | select | All stocks / Top 1–5 deciles / Top 50 / Top 100 **of the selected index** |
| `minimum_return_one_year` | number | % floor on 1Y absolute return |
| `median-volume-one-year-option` | select | 10L, 20L, 50L, 1Cr, 2Cr, 5Cr, 10Cr, Custom |
| `median_volume_one_year` | number | rupee value when Custom |

### 2.3 Moving Average Filters (`Apply Moving Average Filters` switch)

Eight independent boolean switches, AND-combined:
`Above 200-day MA`, `Above 100-day MA`, `Above 50-day MA`, `Above 20-day MA`,
`Below 200-day MA`, `Below 100-day MA`, `Below 50-day MA`, `Below 20-day MA`.

### 2.4 Away from High Filters

`away_from_high_all_time`, `away_from_high_one_year` — "within X% of high". **100 = ignore.**

### 2.5 Percentage of Positive Days Filters

`positive_days_percent_one_year | nine_months | six_months | three_months | one_months`
— minimum % of trading days that closed up. **0 = ignore.**

### 2.6 Circuit Filters

`circuits_one_year | nine_months | six_months | three_months | one_months`
— maximum permitted circuit-hit days in the window. **>250 = ignore.** Stocks exceeding the cap
are *excluded from the ranking*, not merely flagged.

### 2.7 Marketcap Range

`marketcap_from`, `marketcap_to` (₹ crores), inclusive, applied only within the selected index.

### 2.8 Price-to-Earnings Range (`Apply Price to Earnings Filter` switch)

`price_to_earnings_from`, `price_to_earnings_to`. Documented behaviour: stocks with undefined
P/E from NSE are **excluded** when this filter is on.

### 2.9 Series

`EQ` and/or `BE` switches. EQ = delivery + intraday; BE = delivery only (trade-to-trade).

**Baskfy extends this (M59).** The reference product screens the main board only. Baskfy also
carries the NSE Emerge (SME) platform, whose series are `SM` (normal), `ST` (trade-to-trade) and
`SZ` (surveillance), so the switch set is `EQ, BE, SM, ST, SZ`. The default is unchanged —
`["EQ"]` — so no screen written against the reference product changes meaning. Emerge is a
separate NSE *platform* with its own listing register, contained in no NIFTY index; the
`nse-sme-emerge` universe is a 15th entry in §2.1's select and is derived from these series.
SME names are screenable but never sizeable: see `docs/DECISIONS-MERGE.md` M59.

### 2.10 Ignore Top Beta / Volatility

`Ignore Top Beta Stocks`, `Ignore Top Volatility Stocks` switches (each with a count/percentile
input), plus `ignore_above_beta` (100 = ignore).

### 2.11 Price (CMP) Range

`price_from`, `price_to` — inclusive on last close.

### 2.12 Multi-Factor Combined Ranking

`Apply Factor Two` → `factor-two-sort-by`, `factor-two-sort-direction`;
`Apply Factor Three` → same pair. The site's own documentation of the algorithm:

> 1. All filters except Sort By / Sort Direction are applied to the stocks in the selected index.
> 2. The survivors are ranked by factor one.
> 3. The survivors are independently ranked by factor two, then factor three.
> 4. The ranks are summed and a final **ascending** sort is done on the combined rank.

### 2.13 Historical Ranks

`Apply Historical Date` → `historical_date` (date input). Re-runs the entire screen as of a past
date. Site states historical data is available **from 1 Nov 2024**.

### 2.14 Custom Filters (three slots)

Each slot is a **field-to-field comparison**:
`custom-filter-one-value-one` `custom-filter-one-operator` `custom-filter-one-value-two`

Operator ∈ `>=`, `<=`, `=`.
Both operands are drawn from the same list:
Absolute return 1 year · Volatility 1y/9m/6m/3m/1m · Beta · Close · Close raw ·
Away from high all time · Away from high 1 year · Ma 200 · Ma 100 · Ma 50 · Ma 20 ·
Volume day · Volume 1y avg · Volume 9m avg · Volume 6m avg · Volume 3m avg · Volume 1m avg ·
Volume week average.

This is what lets users express things like `Ma 50 >= Ma 200` (golden-cross state) or
`Volume week average >= Volume 1 year average` (volume expansion).

---

## 3. The 62 ranking factors

Grouped by family. `12 9 6 3 1` notation = the blend windows in months.

**Absolute return (16):** 1y, 9m, 6m, 3m, 1m; averages of 12·9·6·3·1, 12·9·6·3, 12·9·6, 12·9,
12·6·3·1, 12·6·3, 12·6, 12·3·1, 12·3, 12·9·3·1, 12·9·3.

**Sharpe return (17):** same five single windows + the same eleven blends + `6 3 months`.

**RSI (16):** same five single windows + the same eleven blends.

**Risk-adjusted-by-beta (5):** Absolute÷beta 1y · Sharpe÷beta 1y · Avg sharpe÷beta 12·9·6·3 ·
12·6·3 · 12·6.

**Skip-month momentum (2):** Return 12 minus 1 months · Return 12 minus two months.

**Non-momentum sort keys (6):** Volatility 1 year · Beta · Price to earnings · Marketcap ·
Close · Close raw · Away from high all time · Away from high 1 year.

> `Close` vs `Close raw`: `Close` is the adjusted/derived close used in calculations, `Close raw`
> is the exchange's unadjusted close. Both are exposed as sort keys and as custom-filter operands.

---

## 4. Results table

Default columns: `#`, Name, Symbol, **Sorting Factor** (the value of the chosen factor),
Last Close, Series, Marketcap (₹ cr), 1Yr Return %, 1Yr Sharpe Return %, 1Yr Volatility, Beta,
MA 200. Header row repeats every ~16 data rows (a nice touch for long tables).

Header shows `N results`, `Results are shown for <date>`, and
`Sorting Factor Column's Value = <FACTOR NAME>`.

### Column picker (`/screens/:id/columns/edit`) — 34 available columns

Series · Marketcap · Price To Earnings · Absolute Return 1y/9m/6m/3m/1m ·
Sharpe Return 1y/9m/6m/3m/1m · Rsi 1y/9m/6m/3m/1m · Beta · Volatility One Year ·
High One Year · High All Time · Away From High One Year · Away From High All Time ·
Ma 200 · Ma 100 · Ma 50 · Ma 20 · Median Volume One Year · Close · Close Raw ·
Circuits 1y/9m/6m/3m/1m.

---

## 5. Instrument factsheet (`/instruments/CUPID` observed)

Blocks, in order:

1. **Header** — symbol, ₹ price, full name, `NSE: SYMBOL`, index membership chips
   (e.g. *Nifty Total Market*, *Nifty Microcap 250*).
2. **Key stats** — P/E, Marketcap (cr), Beta, Series, Listed On.
3. **PROS** — auto-generated bullet list from rules, e.g.
   "The close is above 200-day moving average." (×4 for 200/100/50/20),
   "The close is within 25% of all time high.", "The beta is less than 1.25".
   (A CONS list presumably renders the inverse.)
4. **Sparkline/metric cards with medians** — Closing Price · Rolling 1-Yr Returns (%) with
   `Median: …` · Price to Earnings with median · Marketcap with median · 1-Year RSI with median.
   The medians are the stock's own historical medians, giving instant "cheap/dear vs its own
   history" context.
5. **Price & Moving Averages** — Face Value, 1Y High, Away from 1Y High, ATH, Away from ATH,
   MA 200/100/50/20.
6. **Returns** — 1y, 9m, 6m, 3m, 1m, 12M−1M, 12M−2M.
7. **Sharpe Returns** — 1y, 9m, 6m, 3m, 1m.
8. **Volatility** — 1y, 9m, 6m, 3m, 1m.
9. **RSI** — 1y, 9m, 6m, 3m, 1m.
10. **Market Quality** — **Wasserstein Regime** (`BULL`), Median Vol 1Y (₹ cr),
    Circuits 1y/9m/6m/3m/1m, Positive Days 1y/9m/6m/3m/1m.
11. **Corporate Actions** — type / value / ex-date table (bonus 4:1, split 10:1, bonus 1:1).

---

## 6. Market Health

`Market Health — <universe>`, "Data available from 1st Nov 2024", universe selector
(`nifty-allcap`, `nifty-50`, `nifty-next-50`, `nifty-100`, `nifty-200`, `nifty-500`,
`nifty-total-market`, `nifty-large-mid-250`, `nifty-midcap-150`, `nifty-smallcap-250`,
`nifty-microcap-250`, `nifty-mid-small-400`).

Four breadth gauges: **Above 200 DMA 57.7%**, **Above 50 DMA 49.2%**,
**Within 10% of ATH 17.6%**, **1Y Return > 0% 45.1%**.

Because history starts 1 Nov 2024, these are stored as a daily snapshot table, not recomputed.

---

## 7. Indices dashboard

~145 index rows, each: name, % change, level, absolute change, PE, PB, Div Yield.
Sorted by % change descending. Includes derived indices with no fundamentals
(`Nifty50 PR 1x Inverse`, `India VIX` → PE/PB/DivYield shown as `-`).

---

## 8. Rebalance tracker (beta)

Upload a portfolio CSV of symbols (sample CSV provided) → the tool diffs it against a screen's
current output and returns three copyable lists:

- **Exits** — held but no longer in the screen (and outside the tolerance band)
- **Inside WRH** — held, outside the strict top-N but still inside a "Within Rebalance Hold"
  buffer, so keep
- **Entries** — in the screen, not held

This is the classic *rank-buffer* rebalancing rule (buy top N, hold until rank > N + buffer),
which materially reduces turnover.

---

## 9. Announced roadmap (Dec 2026) — build these in from day one

- **Backtest engine** — test a strategy on historical data.
- **Adjusted price series from 2011** — adjusted for cash dividends, splits, bonus, rights.
- Price increase to fund the extra compute.

> Design implication: the reference product is currently running on **unadjusted** prices, which
> is why a stock like CUPID (bonus 4:1 in Mar 2026, split 10:1 + bonus 1:1 in Apr 2024) shows a
> 753% 1-year return. **Our build treats adjusted prices as the default from day one** and keeps
> the raw close alongside it (`close` vs `close_raw`, exactly as the reference product does).

---

## 10. What we should do better

| Gap in the reference product | Our answer |
|---|---|
| Unadjusted prices distort every factor | Adjusted series is the default; `close_raw` retained |
| Backtest not yet available | Point-in-time engine in Module 15 |
| Historical ranks only from Nov 2024 | Store point-in-time index membership + factors from day 1 of backfill |
| No survivorship-bias handling documented | Delisted instruments retained, PIT membership enforced |
| Circuits/positive-days definitions undocumented | Formally specified in `05-factor-formulas.md` |
| No API for users | Public read API + API keys (Module 20) |
| Rebalance tracker is a one-off CSV diff | First-class portfolios with saved holdings + history |
