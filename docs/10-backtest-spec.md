# 10 — Backtest engine

The reference product ships this in Dec 2026. We build it correctly from the start, because
retrofitting point-in-time correctness is a rewrite.

## Config

```jsonc
{
  "screen_public_id": "…",          // or an inline definition
  "start": "2015-01-01",
  "end":   "2026-08-19",
  "initial_capital": 1000000,
  "rebalance": { "frequency": "monthly",   // weekly|fortnightly|monthly|quarterly
                 "day": "last_trading_day" },
  "selection": { "top_n": 20,
                 "hold_buffer": 10 },      // rank-buffer rule; 0 = strict top-N
  "weighting": "equal",                    // equal | inverse_volatility | rank | marketcap
  "position_limits": { "max_weight": 0.10, "min_weight": 0.01 },
  "costs": { "brokerage_bps": 3, "stt_bps": 10, "slippage_bps": 15, "impact_model": "fixed" },
  "cash_policy": "hold_cash",              // hold_cash | benchmark
  "benchmark": "nifty-500",
  "risk_overlay": { "enabled": false, "rule": "index_above_200dma" }
}
```

## Execution model

For each rebalance date `d`:

1. Run the screen **as of `d`** using `index_member_daily` at `d` and `factor_daily` at `d`.
   No data after `d` may touch the decision. Enforced by a query-layer guard that raises if any
   read carries `date > as_of`.
2. Target = top `top_n`. Apply the **hold buffer**: an existing holding is retained while its
   rank ≤ `top_n + hold_buffer`.
3. Compute target weights from `weighting`, clipped by `position_limits`, renormalised.
4. Execute at the **next trading day's open** (`d+1`), not at `d`'s close. This one choice
   removes the most common source of inflated backtest returns.
5. Apply costs on traded notional. Round to whole shares.
6. Between rebalances, mark to market daily on adjusted closes.
7. Corporate actions are already in the adjusted series; cash dividends are optionally credited
   as cash (`dividends: "reinvest" | "cash" | "ignore"`).
8. Delisting: liquidate at the last available close, credit cash, log the event. Never
   forward-fill a dead instrument (this is exactly how survivorship bias sneaks in).

## Outputs

**Metrics:** CAGR, total return, annualised volatility, Sharpe (rf from a configurable T-bill
series), Sortino, max drawdown + its dates, Calmar, hit rate, average win/loss, annual turnover,
total costs paid, exposure %, best/worst month, rolling 12-month return distribution,
alpha/beta vs benchmark, tracking error, information ratio.

**Artefacts:** daily equity curve, drawdown series, per-rebalance holdings with weights,
trade log (date, symbol, side, qty, price, cost, reason ∈ `enter|exit|rebalance|delist`),
monthly return heatmap.

Large artefacts (trades, per-day holdings) go to R2; `backtest.metrics` and a downsampled
equity curve live in Postgres for fast page loads.

## Performance

Vectorised in Polars. Precompute, per rebalance date, the screen result as a small DataFrame,
then run the portfolio simulation as an array walk over ~2,800 trading days. A 15-year monthly
backtest over 20 positions should complete in **< 10 seconds**. Run in a dedicated Celery queue
with a per-user concurrency cap of 1 and a global cap; stream progress over SSE.

## Correctness harness (non-negotiable tests)

1. **Look-ahead trap:** inject a factor column that is deliberately shifted forward one day; the
   guard must raise, and the test asserts it does.
2. **Buy-and-hold identity:** a "screen" that always returns the benchmark's constituents with
   marketcap weighting must reproduce the benchmark's return within costs.
3. **Zero-cost, zero-turnover identity:** `top_n = universe size`, equal weight, no costs →
   equals the equal-weighted universe return.
4. **Determinism:** same config + same `data_version` → identical metrics hash.
5. **Survivorship:** a fixture universe containing a delisted name must show the loss; a run
   that silently drops it fails the test.
6. **Cost monotonicity:** raising `slippage_bps` must never increase net return.

## What to show the user (honesty features)

- An assumptions panel stating execution timing, costs, dividend policy, and the fact that index
  membership before the first available NSE constituent file is reconstructed.
- A prominent "Past backtest results do not predict future results" line — the reference product
  says this and it is both ethically right and legally necessary.
- A "fragility" readout: the same config re-run with ±1 rebalance-day offset and ±25% costs, so
  users can see whether the result survives small perturbations. Most won't. That is the point.
