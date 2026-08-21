# 09 — Data pipeline

## Provider ports

```python
class BarsProvider(Protocol):
    def list_instruments(self) -> list[InstrumentRecord]: ...
    def daily_bars(self, token: int, start: date, end: date) -> pl.DataFrame: ...


class ReferenceProvider(Protocol):
    def index_constituents(self, index_slug: str, on: date) -> list[str]: ...
    def index_snapshots(self, on: date) -> list[IndexSnapshot]: ...  # level, PE, PB, div yield
    def corporate_actions(self, since: date) -> list[CorporateAction]: ...
    def listings(self) -> list[ListingRecord]: ...
    def bhavcopy(self, on: date) -> pl.DataFrame: ...  # incl. circuit bands, series
```

| Implementation | Provides |
|---|---|
| `KiteProvider` | `BarsProvider` — instruments dump, daily candles |
| `NSEProvider` | `ReferenceProvider` — constituents, index snapshots, corp actions, listings, bhavcopy |
| `CompositeProvider` | routes by capability; retries; per-provider circuit breaker |
| `FixtureProvider` | local dev/tests; reads Parquet fixtures, zero network |

Adding a paid vendor later = one new class. No call site changes.

### Kite specifics to handle

- Session: `api_key` + `access_token`; the token is **daily** and must be refreshed via the
  login flow. Store encrypted; alert loudly when it expires (this is the #1 pipeline failure).
- Rate limit ≈ 3 req/s → a token-bucket limiter shared across workers (Redis).
- Day-interval history is capped per request → chunk backfills into ≤ 2000-day slices per
  instrument, run with bounded concurrency (≤ 3), resume from `ingest_cursor`.
- Kite returns **unadjusted** OHLC by default. Treat everything from Kite as raw; our own
  adjustment step produces the adjusted series.
- Kite has no index constituents, no PE/PB, no corporate-action calendar → NSE fills those.

### NSE specifics

- Public files need a browser-like session (cookie priming) and are rate-sensitive. Fetch once,
  archive the raw file to R2 keyed by `nse/{kind}/{date}.csv`, and parse from the archive.
  Never re-fetch to re-parse: the archive is the reproducibility record.
- Files occasionally publish late or malformed → the QA gate, not the parser, decides whether
  the day is publishable.

## Adjustment algorithm

Process corporate actions in reverse-chronological order. For each action with ex-date `e`, all
bars with `date < e` are multiplied by a factor:

| Action | Factor applied to prices before ex-date | Volume |
|---|---|---|
| Split `a:b` | `b/a` | `× a/b` |
| Bonus `a:b` (a new for b held) | `b/(a+b)` | `× (a+b)/b` |
| Rights | `(P_cum − theoretical value of right) / P_cum` | unchanged |
| Cash dividend `D` | `(P_cum − D) / P_cum` | unchanged |

Cumulative product of all factors after date `d` = `adj_factor[d]`; then
`close = close_raw × adj_factor`. Store `adj_factor` per row so any adjusted number can be
reverse-engineered.

**Rebuild rule:** when a new corporate action arrives, recompute adjustments for that instrument
over its whole history, then recompute *all* of its `factor_daily` rows. Make this a single
idempotent task `reprocess_instrument(instrument_id)`.

## Schedule (IST)

| Time | Job |
|---|---|
| 18:45 | `refresh_instruments`, `fetch_corporate_actions`, `refresh_index_membership` |
| 19:00 | `fetch_daily_bars` (all active instruments) |
| 19:20 | `apply_adjustments` (only instruments with new actions) |
| 19:30 | `compute_factors` |
| 19:45 | `refresh_index_snapshots`, `compute_market_health` |
| 19:50 | `data_quality_gate` → `publish` |
| 20:00 | `warm_cache` (top screens), `send_ops_digest` |
| Sat 02:00 | full-history integrity audit; weekly full backup verification |

Trading-day awareness: an NSE holiday calendar table (`trading_day(date, is_open)`), refreshed
annually from the exchange circular and asserted against observed bar dates.

## Data-quality gate (hard blocker)

Fail the run — and do **not** bump `data_version` — if any assertion trips:

1. `count(bars for as_of) >= 0.9 × median(count over last 10 trading days)`
2. No instrument has `|r_t| > 0.5` unless a corporate action or a legitimate circuit explains it
3. No duplicate `(instrument_id, date)` anywhere
4. `factor_daily` row count == `ohlcv_daily` row count for the date, ±0
5. Every selectable universe has a membership row set within 5% of its nominal size
6. Nifty 50 close from our data matches the NSE-published index snapshot within 0.1%
7. Zero NULLs in `close`, `close_raw`, `volume` for the date
8. `ret_12m` distribution: median within ±3σ of its own 60-day history (catches mass mis-adjustment)

On failure: alert (Slack + email + PagerDuty-style escalation), keep serving the previous
`data_version`, and put a "data delayed" banner in the UI.

## Backfill (one-off, then repeatable)

```
python -m worker.backfill --from 2011-01-01 --to today \
       --instruments all --concurrency 3 --resume
```

Order: instruments → corporate actions → bars (chunked, resumable) → adjustments →
index membership (from NSE historical constituent files; where unavailable pre-2018, reconstruct
from the earliest available file and **record the reconstruction in `index_member_daily.source`
so backtests can exclude uncertain periods**) → factors (vectorised, year by year) →
market health.

Expect: ~2,300 instruments × 15 years. Budget a weekend and a resumable cursor.

## Observability

- Every task emits an OTel span with `trade_date`, `rows_in`, `rows_out`.
- `pipeline_run_step` is the operator UI; expose it at `/admin/pipeline` behind staff auth.
- Metrics: task duration p50/p95, provider error rate, gate pass rate, publish latency
  (EOD close → data live).
- SLO: data published by 20:15 IST on ≥ 95% of trading days.
