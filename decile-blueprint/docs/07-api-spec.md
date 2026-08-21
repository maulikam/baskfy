# 07 — API specification (FastAPI)

Base: `/api/v1`. Auth: `Authorization: Bearer <JWT>` issued by the Next.js app (HS256, shared
secret, 15-min access + refresh) or an API key (`X-API-Key`) for the public read API.
All responses are `application/json`; errors follow RFC 9457 `application/problem+json`.

## Conventions

- Cursor pagination: `?limit=100&cursor=…` → `{ "data": [...], "next_cursor": "…" }`
- Every analytics response includes `"as_of": "2026-08-19"` and `"data_version": 1421`
- Rate limits: 60 req/min authenticated, 10 req/min anonymous, 600 req/min for API keys
- Idempotency: `Idempotency-Key` header honoured on all POSTs that create resources

## Metadata

```
GET  /meta/factors              → factor registry (key, label, family, unit, higher_is_better)
GET  /meta/columns              → the 34 selectable result columns
GET  /meta/universes            → index_def rows where is_universe
GET  /meta/trading-days?from&to → list of trading dates (for the historical date picker)
GET  /meta/status               → { as_of, data_version, last_pipeline_run }
```

## Screens

```
GET    /screens                          → user screens + example screens
POST   /screens                          { name, definition, columns? }
GET    /screens/{public_id}
PATCH  /screens/{public_id}              { name?, definition?, columns? }
DELETE /screens/{public_id}
POST   /screens/{public_id}/duplicate
```

`definition` is validated against the `ScreenDefinition` Pydantic model
(see `04-data-model.md`). Unknown keys are rejected (`extra="forbid"`).

## Running a screen

```
POST /screens/{public_id}/run
     { "as_of": "2026-08-19" | null, "override_definition": {…} | null }
→ 200 {
    "as_of": "2026-08-19",
    "data_version": 1421,
    "result_count": 270,
    "sorting_factor": { "key": "avg_sharpe_12_6_3_1",
                        "label": "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS" },
    "columns": ["symbol","name","sorting_factor","close_raw","series","marketcap_cr", …],
    "rows": [ { "rank": 1, "symbol": "CUPID", "name": "CUPID LIMITED",
                "sorting_factor": 5.19, "close_raw": 284.56, "series": "EQ",
                "marketcap_cr": 38264, "ret_12m": 753.00, "sharpe_12m": 13.00,
                "vol_12m": 57.93, "beta_12m": 0.85, "ma_200": 121.39 }, … ]
  }

POST /screens/preview          # run an unsaved definition (the edit form's live preview)
     { "definition": {…}, "as_of": null }

GET  /screens/{public_id}/csv?as_of=…     → text/csv  (entitlement-gated)
GET  /screens/{public_id}/runs?limit=…    → historical run summaries
```

## Instruments

```
GET /instruments?search=…&limit=…                → typeahead
GET /instruments/{symbol}                        → factsheet payload
GET /instruments/{symbol}/history?from&to&field= → sparkline series
GET /instruments/{symbol}/corporate-actions
GET /instruments/{symbol}/rank-history?screen=…  → this stock's rank over time in a screen
GET /listings?from&to&series=&cursor=            → NSE listings, newest first
```

Factsheet payload mirrors the teardown §5 exactly: `header`, `key_stats`, `pros`, `cons`,
`metric_cards` (value + own-history median), `price_and_mas`, `returns`, `sharpe_returns`,
`volatility`, `rsi`, `market_quality` (incl. `regime` + the two Wasserstein distances),
`corporate_actions`, `index_memberships`.

## Market data surfaces

```
GET /indices/dashboard?date=            → ~145 rows: level, chg, chg_pct, pe, pb, div_yield
GET /market-health?universe=nifty-500&date=
GET /market-health/history?universe=&from=&to=   → the four series for charting
```

## Portfolios & rebalance

```
GET    /portfolios
POST   /portfolios                      { name, holdings:[{symbol, quantity?, avg_price?}] }
POST   /portfolios/import-csv           multipart; returns parse report + unmatched symbols
GET    /portfolios/{id}
PUT    /portfolios/{id}/holdings
POST   /portfolios/{id}/rebalance
       { "screen_public_id": "...", "top_n": 20, "hold_buffer": 10, "as_of": null }
     → { "exits":[…], "inside_wrh":[…], "entries":[…],
         "as_of":"…", "target_weights":[…] }
```

`inside_wrh` = held names whose current rank is `> top_n` but `<= top_n + hold_buffer`.

## Backtests

```
POST /backtests            { config }        → 202 { public_id, status:"queued" }
GET  /backtests/{id}                          → status + metrics + equity curve
GET  /backtests/{id}/trades?cursor=           → paginated fills
GET  /backtests/{id}/export                   → CSV/Parquet signed URL
DELETE /backtests/{id}
```

## Account & billing

```
POST /auth/register  /auth/login  /auth/refresh  /auth/logout
POST /auth/request-otp  /auth/verify-otp
POST /auth/forgot-password  /auth/reset-password
GET  /me                                  → profile + entitlements
PATCH /me
POST /me/change-password
GET  /plans
POST /checkout/session                    { plan_code } → Razorpay order/subscription payload
POST /webhooks/razorpay                   (signature-verified, idempotent by event id)
GET  /invoices  /invoices/{id}/pdf
```

## Entitlements

`GET /me` returns:

```json
{ "entitlements": { "screener": true, "export_csv": true, "custom_columns": true,
                    "historical_ranks": true, "backtests": true, "api_access": false,
                    "max_screens": 50 } }
```

Enforcement is server-side on every gated endpoint; the UI only *reflects* entitlements.
A 402 `payment_required` problem response carries `{"upgrade_url": "/pricing"}`.

## Error catalogue

| Status | `type` | When |
|---|---|---|
| 400 | `invalid-screen-definition` | schema violation; `errors[]` lists field paths |
| 401 | `unauthenticated` | missing/expired token |
| 402 | `payment-required` | entitlement missing |
| 404 | `not-found` | unknown screen/instrument |
| 409 | `stale-data-version` | client sent a `data_version` that no longer exists |
| 422 | `no-trading-day` | `as_of` before the data start date |
| 429 | `rate-limited` | includes `Retry-After` |
| 503 | `pipeline-degraded` | last run failed its QA gate |

## OpenAPI → TypeScript

`services/api` emits `openapi.json` on build; `packages/api-client` is generated with
`openapi-typescript` + `openapi-fetch` in CI. Hand-written request types are a CI failure.
