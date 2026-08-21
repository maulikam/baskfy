# NIFTY options research plan

Status: **paper-only, read-only market adapter complete, order execution intentionally
disabled**.

## What the return targets mean

The requested 5% option-selling figure is a **monthly portfolio benchmark**, not a quota.
Compounding 5% every month is about 79.6% a year.  The requested 10–15% option-buying
figure is a **winning-trade benchmark on premium paid**, not a minimum outcome for every
trade.  A strategy that enters merely because a quota has not yet been met is rejected by
design.

SEBI's July 2025 study reports that 91% of individual equity-derivatives traders lost
money in FY25, with aggregate net losses of Rs 1,05,603 crore after transaction costs.
These targets therefore remain hypotheses until forward data demonstrates positive net
expectancy.  The software reports them but never promises them.

## Strategy A — intraday defined-risk iron condor

Use only the nearest NIFTY weekly expiry with 1–7 calendar days remaining.  The live NFO
instrument dump decides expiry, strikes, lot size and tick size; none is hard-coded.

Entry window: 09:45–13:30 IST.

The entry is eligible only when:

- nearest-future price is within 0.35% of its session VWAP;
- the 15-minute opening range is no more than 65% of the ATM straddle-implied move;
- every leg passes bid/ask spread, traded-volume and open-interest gates;
- approximately 0.16-delta shorts and 0.05-delta wings produce credit of at least 10% of
  spread width; and
- one full lot fits inside 0.75% of available-capital maximum loss.

Protective call and put wings are bought before either short is allowed.  The payoff is
defined-risk at construction.  Maximum four lots; live broker basket margin is requested
as a read-only cross-check.

Exit:

- take profit after capturing 35% of initial credit;
- stop at 1.5 times initial credit lost, never beyond defined maximum loss;
- exit on either short-strike breach; or
- close every leg by 15:12 IST, with short legs first and fresh marketable limit prices.

The 5% monthly value is a reporting benchmark.  It does not change entries or increase
size after losses.

## Strategy B — intraday directional option buyer

Use the nearest NIFTY expiry with 1–8 calendar days remaining and a liquid, slightly ITM
option near 0.60 absolute delta.

Entry window: 09:35–14:30 IST.

Buy a call only when the nearest NIFTY future is above the first 15-minute range plus a
0.05% buffer, above VWAP, fast EMA is above slow EMA, and the last complete five-minute
volume block is at least 1.2 times its recent median.  Reverse all four conditions for a
put.  NIFTY spot remains the pricing/strike reference because futures basis must not be
mistaken for cash spot.

Risk no more than 0.50% of available capital at the 25% premium stop and allocate no more
than 5% of capital to premium.  Maximum six lots.

Exit:

- hard stop at a 25% premium loss;
- at +15%, activate an 8-percentage-point trailing drawdown instead of capping upside;
  for example, a +24% peak exits at or below +16%; or
- close by 15:12 IST.

Overnight buying is deliberately deferred.  It requires a separate NRML risk model for
gap risk, event calendars and next-session liquidity; changing MIS to NRML is not a safe
substitute.

## Why the signal uses NIFTY futures

The cash NIFTY index does not trade and has no genuine traded volume.  Its spot value is
used for option valuation, implied volatility, delta and strikes.  The nearest NIFTY
future provides one internally consistent set of opening range, VWAP, EMAs and volume.
Every output labels the future symbol and both cash and signal prices.

## How to collect honest validation data

Kite does not provide historical candles for expired option contracts.  NSE's derivative
bhavcopy is end-of-day data and cannot reconstruct an intraday spread, fill sequence or
stop.  A credible intraday backtest therefore needs licensed historical option-chain data
or forward snapshots captured while contracts are live.

After the daily Kite login, collect one full read-only observation with:

```bash
python -m scripts.options_research seller \
  --record data/outputs/options_forward.jsonl
python -m scripts.options_research buyer \
  --record data/outputs/options_forward.jsonl
```

Each JSONL record contains the complete selected chain, bid/ask, volume, OI, signal
inputs, available capital, rejection or plan, and (when a plan exists) the broker's basket
margin estimate.  It records zero orders.  Run it at fixed five-minute timestamps during
the session; no-trade rejections are data and must not be discarded.

## Live enablement gate

Do not enable live orders merely because unit tests pass.  All of the following are
required first:

1. At least 60 trading sessions and 100 eligible signals, including quiet, trend and event
   days.
2. Bid/ask fill simulation plus all brokerage, exchange charges, GST, STT and stamp duty.
3. Positive out-of-sample expectancy and profit factor above 1.20 after costs; report win
   rate, average win/loss, tail loss, maximum drawdown and monthly distribution.  Report
   whether the 5% monthly and 10–15% winning-trade benchmarks were actually achieved,
   without optimizing only for them.
4. At least 20 consecutive sessions in DRY_RUN shadow mode with no stale quote, orphan
   leg, limit-price, quantity, reconciliation or square-off failure.
5. A fill-aware multi-leg executor: buy wings, verify fills, place shorts, verify fills,
   unwind safely on partial failure, reconcile positions from broker truth, and maintain a
   hard 15:12 emergency close.  This component does not exist yet, so the strategies stay
   paper-only.
6. Confirm current broker/exchange/API requirements, static-IP setup, order tagging and
   order-rate controls.  Every submitted order must remain a limit order and go through
   `app/core/gateway.py` after explicit user approval.

## Implementation map

- `app/strategies/options.py` — pure planners, payoff/risk sizing and exits.
- `app/strategies/options_market.py` — read-only live instruments, chain, futures signal
  and basket-margin adapter.
- `scripts/options_research.py` — read-only plan/record command.
- `app/config.py` and `.env.example` — parameters and locked-off product gates.
- `tests/test_options_strategies.py` — strategy, metadata, safety-gate and price-tick tests.

## Sources reviewed

- [SEBI study on individual traders in the equity derivatives segment, July 2025](https://www.sebi.gov.in/sebi_data/attachdocs/jul-2025/1751900271726.pdf)
- [NSE NIFTY 50 derivatives contract specifications](https://www.nseindia.com/static/products-services/equity-derivatives-nifty50)
- [NSE market timings](https://www.nseindia.com/static/market-data/market-timings)
- [NSE retail-algo FAQ, November 2025](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail%20Algo_03112025_NSE.pdf)
- [SEBI implementation timeline for safer retail algo participation](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf)
- [Kite Connect basket margin API](https://kite.trade/docs/connect/v3/margins/)
- [Kite Connect quote API](https://kite.trade/docs/connect/v3/market-quotes/)
- [Kite Connect WebSocket API](https://kite.trade/docs/connect/v3/websocket/)
- [Kite Connect order API](https://kite.trade/docs/connect/v3/orders/)
- [Zerodha developer forum: expired option historical candles are unavailable](https://kite.trade/forum/discussion/12880/how-to-download-historical-option-data-which-has-expired)
- [NSE derivative reports and UDiFF bhavcopy](https://www.nseindia.com/all-reports-derivatives)
- [Intraday option return momentum research (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5018430)

Research on option-selling indices and option-return momentum was used only to motivate
mechanisms and tests.  It is not evidence that these exact NIFTY intraday rules will earn
the requested returns.
