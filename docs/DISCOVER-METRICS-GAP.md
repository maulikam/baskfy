# The metrics gap — what Discover asks for that nothing computes

The brief's card, its twenty advanced filters, its comparison table and most of its basket detail
page are built out of figures **this product does not hold**. This is the register of exactly
which, where each would have to come from, and what it blocks. It is the bridge between the UI
that shipped and the UI the brief describes.

Measured, `cb_metrics` holds precisely this
(`packages/core/src/baskfy_core/models/curated_baskets.py:342-368`):

```
min_amount · volatility_bucket · volatility_value · volatility_basis
ret_1m · ret_6m · ret_1y · cagr_3y · cagr_5y · since_inception_pct
months_available · return_convention · dividends_included · computed_at
```

Everything below is absent from that list. Discover renders each as a labelled blank with the
reason attached (`lib/discover/metrics.ts` → `uncomputedMetrics()`), never as a zero and never as
a dropped row — a comparison that omits the drawdown row reads as "these baskets are alike on
drawdown", which is a claim nobody made and nobody checked.

## Register

| Metric | Where it would come from | What it blocks | Notes |
|---|---|---|---|
| `max_drawdown` | backtest — `packages/core/src/baskfy_core/backtest.py` already walks an equity curve; peak-to-trough is one pass over it | the brief's card table, "lower-drawdown strategies" shelf, the downside filter, comparison Downside section | The single highest-value missing number. Everything the brief says about risk assumes it. |
| `recovery_days` | backtest — same equity curve, time from trough back to the prior peak | comparison Downside, "recovery duration" filter | Cheap once drawdown exists; the same pass yields both. |
| `sharpe` | backtest + a risk-free series. **No risk-free curve is stored** — `BacktestConfig.risk_free_curve` exists as a config input, not as data | Sharpe filter, comparison Downside | Needs a decision on the rate source before it is an engineering task. |
| `sortino` | backtest — downside deviation over the same series | Sortino filter | Same pass as Sharpe, different denominator. |
| `rolling_1y_positive_pct` | backtest — every 1Y window over the equity curve | comparison Consistency, "positive rolling-return percentage" filter | Only meaningful once a basket has years of history; today's oldest is months old. |
| `turnover_pct` | `cb_basket_version` + `cb_constituent` — the diff between consecutive versions is already counted as `added_count` / `removed_count` | comparison Operations, "estimated transaction cost", the brief's overfitting warning | The data exists. This is the cheapest item on the list and needs no backtest. |
| `benchmark_delta_pct` | `index_snapshot_daily` — a benchmark series exists; the basket's return minus its benchmark's over the same window | the card's "vs Nifty 500 +4.2%", comparison Performance | Needs a per-basket benchmark column on `cb_basket`, which does not exist either. |
| `top10_concentration_pct` | `cb_constituent` weights — sum of the ten largest | comparison Portfolio, "top-10 concentration" filter, concentration warnings | Data exists; one query. |
| sector / market-cap concentration | `cb_constituent` joined to instrument sector — **no sector column exists on `instrument`** | "sector concentration", "market-cap exposure", "diversified across market caps" shelf | Blocked twice: needs a sector reference source first. |
| holdings count | `cb_constituent` — a count | the card's "25 stocks" | Data exists; not exposed. |
| `volatility_basis` | `cb_metrics.volatility_basis` — **exists in the database and is not exposed by the API** | telling a measured volatility from one blended out of the holdings, which reads high | The only row here that is purely an API-surface change. `routers/explore.py` `CardMetrics` would gain one field. |
| holdings list | `cb_constituent` — **no route exposes it for a catalogue basket**; `/basket/[slug]/constituents` is an SC5 stub | portfolio overlap in Compare, "what is different", holdings tab | `lib/discover/overlap.ts` is written and tested against this shape and takes `symbols: null` for "could not be read", so the UI is ready for the route the day it exists. |

## What this means for the brief, section by section

- **§4 (card design).** Shipped without max drawdown, benchmark delta, turnover and holdings
  count. The card shows volatility, the headline return with its window named, and the minimum —
  each with an explanation and the return convention beside it.
- **§5 (filters).** Nine of the simple filters are real and wired. Every advanced filter except
  "number of holdings" and "backtest start date" is blocked by this register. The filter rail
  names the ones it cannot offer rather than rendering controls that quietly match everything.
- **§6 (comparison).** Performance, Downside (volatility only), Operations, Construction,
  Capital and Evidence are real. Consistency and Portfolio are visible blanks. Portfolio overlap
  is implemented and waits only on the constituents route.
- **§7 (basket detail).** The performance chart, growth of ₹1 lakh and benchmark comparison have
  a series behind them (`lib/explore/performance.ts`). The drawdown chart, recovery periods,
  downside capture and calendar heatmap are all blocked here.
- **§9 (smart warnings for overfitting, concentration, high turnover).** Every one of the three
  needs a number from this table. They are not "AI features" — they are arithmetic waiting on
  inputs.

## The order worth doing them in

1. **`turnover_pct` and `top10_concentration_pct`** — the data is already in `cb_constituent`, no
   backtest and no new source. Two of the brief's three smart warnings become possible.
2. **The constituents route** — unblocks portfolio overlap, which is the single most useful thing
   a comparison of six near-identical momentum baskets can say.
3. **`volatility_basis` on the API** — one field, and it stops a blended figure being read as a
   measured one.
4. **`max_drawdown` and `recovery_days`** — one pass over an equity curve the backtest engine
   already produces. Unblocks most of §4 and the Downside half of §6.
5. **A benchmark per basket, then `benchmark_delta_pct`** — needs a schema decision first.
6. **Sharpe and Sortino** — need the risk-free source decision; least valuable of the set until
   the baskets have enough history for the ratio to mean anything.

Sector data is deliberately last: it needs a reference source this product has never had, and
every shelf and filter that depends on it should stay unbuilt until it does.
