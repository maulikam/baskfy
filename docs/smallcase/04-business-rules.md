# 04 — Business rules (the spec the tests assert)

Numbers observed live on smallcase, 22 Aug 2026 (spec §8). Where Baskfy deviates, the
deviation is stated here — tests assert *this file*, not the spec, and never current
behaviour. All math lives in `packages/core` as pure functions (DataFrames/values in,
values out); services feed them.

## 1. Fees (computed and journaled; never collected this run)

- **Buy / Invest-more:** `base = min(₹100, 1.5% × amount)`; `gst = 18% × base`;
  `total = base + gst` (observed as flat ₹118 on typical amounts). One `cb_fee_ledger`
  row per executed batch, `collected=false`.
- **SIP instalment:** `base = min(₹10, 1.5% × amount)` + 18% GST.
- **Rebalance, exit, partial exit, customize:** platform fee zero. Broker/statutory
  charges are the broker's business and are not modeled.
- Rounding: fee amounts round half-up to 2 decimals at write time.
- User-created (PRIVATE) baskets accrue the same buy/invest-more fees — smallcase charges
  for custom baskets too, and the ledger should mirror that from day one.

## 2. Minimum investment amount

The smallest lump sum that buys ≥1 whole share of every constituent in prescribed weights:

```
min_amount = ceil( max_i( price_i / weight_i ) )
shares_i(amount) = floor( amount × weight_i / price_i )   -- must be ≥ 1 at min_amount
```

- `price_i` = last traded price when a live quote cache exists, else latest EOD
  `close_raw` (display prices use the exchange print, house rule 6).
- Recomputed by the EOD metrics job into `cb_metrics`; recomputed on-demand (from cached
  quotes) when a plan preview is generated, since the number the user commits to must be
  current. The catalog's "Under ₹5k/25k/50k" chips filter on the EOD value.
- Property test: buying exactly `min_amount` yields ≥1 share of every constituent and
  post-buy weight error per constituent is minimized subject to whole shares.

## 3. Volatility bucket

- `volatility_value` = annualized std-dev of the basket's daily return series over the
  trailing 252 trading days (chain-linked across versions, point-in-time weights). Baskets
  younger than 126 trading days use full available history; younger than 60, bucket is
  computed from constituent volatilities weighted by target weight (the spec's "std.
  deviation of constituents" reading).
- Buckets: terciles across all PUBLISHED baskets recomputed monthly; with few baskets
  (single-tenant reality), fixed thresholds apply instead — LOW < 15% ≤ MED < 25% ≤ HIGH
  annualized — and the switch to terciles happens when the published count reaches 12.
  (Decision pre-taken here so the run doesn't stall on it; reversible, recorded.)

## 4. Returns and performance

- **Since-inception absolute %** on the detail page: chain-linked version-aware series
  from `launched_at`, base 100.
- **Headline card metric by age** (mirroring observed behaviour): ≥5y → 5Y CAGR; ≥3y →
  3Y CAGR; ≥1y → 1Y absolute; else "{n}M returns" absolute. Label always names the window.
- **Chart ranges:** 1M / 1Y / 3Y / 5Y / MAX on EOD closes; intraday granularity is out of
  scope.
- **SIP mode:** simulate a fixed monthly instalment on the first trading day of each month
  over the selected range, whole-share fills at that day's close, and plot the value of
  the accumulating position vs. money put in.
- **Benchmark compare:** overlay a chosen index normalized to the same base. Default
  benchmark: `cb_basket.benchmark_instrument_id`, else NIFTY 50 (via its index/ETF series
  as available in the price store).
- **Investor math** (per investment): Current Value (holdings × last price); Money Put In
  (sum of buy-side cash including invest-more/SIP, net of nothing); Current Investment
  (money put in minus cost basis of exited quantity); Current Returns (value vs current
  investment, % and ₹); Realized Returns (₹, can be negative — from sells at execution
  price vs avg cost); Dividends (sum of `cb_dividend.total`); **XIRR** over all cash flows
  including dividends, displayed only when the first investment is >365 days old.
- Survivorship/pre-listing caveat: any chart segment computed over dates where price
  coverage is incomplete renders the existing history-caveat disclosure component.

## 5. Rebalance semantics

- Publishing a version creates: the version row, an ENGINE update post, a
  `REBALANCE_AVAILABLE` pending action, and `cb_user_rebalance_state=PENDING` for every
  ACTIVE investment in that basket.
- The apply preview diffs *the investor's current intended holdings* (not the previous
  version) against the new version's target weights at current prices → buy/sell child
  order list. Sells fund buys; a residual cash line is shown, and a top-up amount may be
  required to reach new min-amount constraints (smallcase behaves the same way).
- Applying = generating a desk plan from the diff; the batch goes PLANNED, and EXECUTED
  only when the journal confirms. Plans expire in 30 minutes (desk non-negotiable #1);
  expiry returns the state to PENDING with the batch marked EXPIRED.
- Skipping is explicit and recorded; a skipped version leaves the investment on its old
  `version_applied_id`, and the next publish supersedes the pending state.
- Market-hours guard: apply/invest/exit plan generation outside NSE hours (9:15–15:30 IST,
  trading days from the existing calendar) returns the closed-market response with the
  next open datetime; the UI renders the modal with "notify me" creating a
  `cb_pending_action(type=GENERIC)` reminder for the next open.

## 6. Watchlist

`moved_pct = (nav_today / nav_at_watch) − 1`, where nav is the basket's chain-linked index
value. Displayed with the watch date. Daily change alongside. Unwatch deletes the row;
re-watching starts a fresh baseline.

## 7. Drift

Nightly (and on-demand) job compares `cb_investment_holding` against the desk's broker
holdings snapshot for the mapped instruments. Any shortfall → `DRIFT` pending action with
the per-instrument delta. The fix-flow re-bases `cb_investment_holding` to broker reality
(archiving the delta as a synthetic EXIT record so realized-PnL math stays honest), then
clears the action. Excess (bought more directly) is recorded as CUSTOMIZE-kind history.

## 8. Access and entitlement (the matrix)

Implemented once, as middleware — see `02-scope-and-gating.md`. While
`BASKFY_SUBSCRIPTIONS_ENABLED=false`: every basket is Free Access, constituent tables are
never locked, and plan/subscription surfaces 404. The middleware and its tests exist now;
the flag flips after D3/D7.

## 9. Timezone and calendar

All market logic in IST (`Asia/Kolkata`); trading days from the repo's NSE calendar
(post-M7 corrected). No weekend/holiday order-shaped actions. EOD jobs run after the
existing ingestion completes, sequenced on Celery Beat, idempotent per (job, date).
