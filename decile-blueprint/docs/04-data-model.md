# 04 — Data model

PostgreSQL 16 + TimescaleDB. All timestamps `timestamptz`, all dates `date` in IST trading-day
terms. Money in `numeric`, never float. IDs are `bigint` identity except user-facing entities,
which also carry a `public_id` (`nanoid`, 12 chars) used in URLs.

---

## Reference & instrument data

```sql
CREATE TABLE exchange (
  id            smallint PRIMARY KEY,
  code          text NOT NULL UNIQUE      -- 'NSE'
);

CREATE TABLE instrument (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  exchange_id       smallint NOT NULL REFERENCES exchange(id),
  symbol            text NOT NULL,          -- 'CUPID'
  name              text NOT NULL,          -- 'CUPID LIMITED'
  isin              text,
  instrument_type   text NOT NULL,          -- 'EQ' | 'ETF' | 'INDEX'
  series            text,                   -- 'EQ' | 'BE' | 'ST' | ...
  face_value        numeric(12,4),
  lot_size          integer,
  listed_on         date,
  delisted_on       date,                   -- NULL = active; NEVER delete rows
  kite_token        bigint,                 -- provider key
  is_active         boolean NOT NULL DEFAULT true,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (exchange_id, symbol, series)
);
CREATE INDEX ON instrument (symbol);
CREATE INDEX ON instrument (kite_token);

CREATE TABLE symbol_alias (            -- NSE renames symbols; keep history mapped
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instrument_id   bigint NOT NULL REFERENCES instrument(id),
  old_symbol      text NOT NULL,
  changed_on      date NOT NULL
);
```

## Price history

```sql
CREATE TABLE ohlcv_daily (
  instrument_id bigint  NOT NULL REFERENCES instrument(id),
  date          date    NOT NULL,
  open          numeric(18,4) NOT NULL,   -- adjusted
  high          numeric(18,4) NOT NULL,
  low           numeric(18,4) NOT NULL,
  close         numeric(18,4) NOT NULL,   -- adjusted  <- factors use this
  volume        bigint        NOT NULL,   -- adjusted for splits
  close_raw     numeric(18,4) NOT NULL,   -- exchange print, unadjusted
  volume_raw    bigint        NOT NULL,
  turnover      numeric(20,2),            -- ₹, from NSE bhavcopy when available
  adj_factor    numeric(18,10) NOT NULL DEFAULT 1,
  upper_circuit numeric(18,4),
  lower_circuit numeric(18,4),
  source        text NOT NULL,            -- 'kite' | 'nse'
  PRIMARY KEY (instrument_id, date)
);
SELECT create_hypertable('ohlcv_daily','date', chunk_time_interval => INTERVAL '1 year');
ALTER TABLE ohlcv_daily SET (timescaledb.compress,
  timescaledb.compress_segmentby='instrument_id');
SELECT add_compression_policy('ohlcv_daily', INTERVAL '90 days');
```

## Corporate actions

```sql
CREATE TABLE corporate_action (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instrument_id bigint NOT NULL REFERENCES instrument(id),
  action_type   text   NOT NULL,   -- 'dividend'|'split'|'bonus'|'rights'|'demerger'
  ex_date       date   NOT NULL,
  ratio_from    numeric(18,6),     -- split 10:1 -> from=10,to=1 ; bonus 4:1 -> from=4,to=1
  ratio_to      numeric(18,6),
  amount        numeric(18,4),     -- dividend per share
  raw           jsonb NOT NULL,
  UNIQUE (instrument_id, action_type, ex_date)
);
```

## Index universes (point-in-time — the anti-look-ahead spine)

```sql
CREATE TABLE index_def (
  id        smallint PRIMARY KEY,
  slug      text NOT NULL UNIQUE,   -- 'nifty-500', 'nifty-allcap', 'etf', 'nifty-fno'
  name      text NOT NULL,
  is_universe boolean NOT NULL DEFAULT true,   -- selectable in the screener
  sort_order  smallint NOT NULL DEFAULT 0
);

CREATE TABLE index_member_daily (
  index_id      smallint NOT NULL REFERENCES index_def(id),
  date          date     NOT NULL,
  instrument_id bigint   NOT NULL REFERENCES instrument(id),
  weight        numeric(10,6),
  PRIMARY KEY (index_id, date, instrument_id)
);
SELECT create_hypertable('index_member_daily','date', chunk_time_interval => INTERVAL '1 year');
CREATE INDEX ON index_member_daily (date, index_id);

CREATE TABLE index_snapshot_daily (   -- powers /dashboard
  index_id  smallint NOT NULL REFERENCES index_def(id),
  date      date NOT NULL,
  level     numeric(18,4),
  change_abs numeric(18,4),
  change_pct numeric(10,4),
  pe numeric(12,4), pb numeric(12,4), div_yield numeric(10,4),
  PRIMARY KEY (index_id, date)
);
```

## Fundamentals

```sql
CREATE TABLE fundamental_daily (
  instrument_id bigint NOT NULL REFERENCES instrument(id),
  date          date   NOT NULL,
  marketcap_cr  numeric(18,2),
  pe            numeric(14,4),   -- NULL where NSE does not publish
  pb            numeric(14,4),
  div_yield     numeric(10,4),
  shares_outstanding bigint,
  PRIMARY KEY (instrument_id, date)
);
```

## The fact table

One wide row per instrument per trading day. Nullable everywhere (young listings lack history).

```sql
CREATE TABLE factor_daily (
  instrument_id bigint NOT NULL REFERENCES instrument(id),
  date          date   NOT NULL,

  close          numeric(18,4),
  close_raw      numeric(18,4),

  ret_1m  numeric(14,4), ret_3m  numeric(14,4), ret_6m  numeric(14,4),
  ret_9m  numeric(14,4), ret_12m numeric(14,4),
  ret_12m_minus_1m numeric(14,4), ret_12m_minus_2m numeric(14,4),

  vol_1m  numeric(12,4), vol_3m  numeric(12,4), vol_6m  numeric(12,4),
  vol_9m  numeric(12,4), vol_12m numeric(12,4),

  sharpe_1m numeric(14,4), sharpe_3m numeric(14,4), sharpe_6m numeric(14,4),
  sharpe_9m numeric(14,4), sharpe_12m numeric(14,4),

  rsi_1m numeric(10,4), rsi_3m numeric(10,4), rsi_6m numeric(10,4),
  rsi_9m numeric(10,4), rsi_12m numeric(10,4),

  beta_12m numeric(10,4),

  ma_20 numeric(18,4), ma_50 numeric(18,4),
  ma_100 numeric(18,4), ma_200 numeric(18,4),

  high_1y numeric(18,4), high_ath numeric(18,4),
  away_high_1y numeric(10,4), away_high_ath numeric(10,4),

  pos_days_1m numeric(7,4), pos_days_3m numeric(7,4), pos_days_6m numeric(7,4),
  pos_days_9m numeric(7,4), pos_days_12m numeric(7,4),

  circuits_1m smallint, circuits_3m smallint, circuits_6m smallint,
  circuits_9m smallint, circuits_12m smallint,

  vol_day_val      numeric(20,2),   -- today's turnover ₹
  vol_avg_1w       numeric(20,2),
  vol_avg_1m       numeric(20,2), vol_avg_3m numeric(20,2), vol_avg_6m numeric(20,2),
  vol_avg_9m       numeric(20,2), vol_avg_12m numeric(20,2),
  median_vol_12m   numeric(20,2),   -- median daily ₹ turnover, 1y

  marketcap_cr numeric(18,2),
  pe           numeric(14,4),
  series       text,

  regime       text,               -- Wasserstein regime: 'BULL'|'BEAR'|'NEUTRAL'

  -- Denormalised universe + risk flags, materialised nightly from index_member_daily.
  -- The reference product's CSV export proves it uses exactly this shape (42 booleans).
  -- Stored as a bitmask trio to avoid 42 columns; expand to booleans in the view layer.
  universe_mask      integer NOT NULL DEFAULT 0,   -- bit per index_def.id
  top_beta_mask      integer NOT NULL DEFAULT 0,   -- bit set = in that universe's top-beta cut
  top_volatility_mask integer NOT NULL DEFAULT 0,

  PRIMARY KEY (instrument_id, date)
);
SELECT create_hypertable('factor_daily','date', chunk_time_interval => INTERVAL '1 year');
CREATE INDEX ON factor_daily (date);
-- covering index for the hot screen query:
CREATE INDEX ON factor_daily (date, marketcap_cr DESC);
```

> **Precision matters.** Match the reference product's storage precision exactly (see
> `docs/13-csv-export-schema.md` §4): prices/MAs/returns/sharpe/away-from-high/positive-days at
> 2 dp, RSI at 4 dp, volatility and beta at 10 dp (volatility as a **decimal fraction**, not a
> percentage), marketcap as an integer in ₹ crore, volumes as `bigint` rupees. Round at write
> time, so the API, the UI and the CSV export can never disagree.

> **Blend factors are NOT stored.** `avg_sharpe_12_6_3_1` etc. are computed in SQL as the mean
> of the stored component columns. 44 blend columns would be dead weight and a migration
> liability; the arithmetic is free.

## Screens

```sql
CREATE TABLE screen (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id     text NOT NULL UNIQUE,
  user_id       bigint REFERENCES app_user(id),   -- NULL = system/example screen
  name          text NOT NULL,
  definition    jsonb NOT NULL,        -- validated by ScreenDefinition (Pydantic + Zod)
  columns       jsonb NOT NULL DEFAULT '[]',
  is_example    boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON screen (user_id);

CREATE TABLE screen_run (            -- audit + "historical ranks" cache
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  screen_id   bigint NOT NULL REFERENCES screen(id) ON DELETE CASCADE,
  as_of       date NOT NULL,
  definition_hash text NOT NULL,
  result_count integer NOT NULL,
  results     jsonb NOT NULL,          -- [{rank, instrument_id, factor_value}]
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (screen_id, as_of, definition_hash)
);
```

`screen.definition` JSON shape (single source of truth — mirrored in Pydantic and Zod):

```jsonc
{
  "index": "nifty-total-market",
  "sort_by": "avg_sharpe_12_6_3_1",
  "sort_direction": "desc",
  "apply_filters_on": "all",           // all | decile_1..decile_5 | top_50 | top_100
  "min_return_1y": null,
  "median_volume_1y": 10000000,
  "moving_average": { "enabled": false,
      "above_200": false, "above_100": false, "above_50": false, "above_20": false,
      "below_200": false, "below_100": false, "below_50": false, "below_20": false },
  "away_from_high": { "ath": 100, "one_year": 100 },
  "positive_days": { "m12": 0, "m9": 0, "m6": 0, "m3": 0, "m1": 0 },
  "circuits":      { "m12": 999, "m9": 999, "m6": 999, "m3": 999, "m1": 999 },
  "marketcap":     { "from": null, "to": null },
  "pe":            { "enabled": false, "from": null, "to": null },
  "series":        ["EQ"],
  "ignore_top_beta":       { "enabled": false, "count": 0 },
  "ignore_top_volatility": { "enabled": false, "count": 0 },
  "ignore_above_beta": 100,
  "price": { "from": null, "to": null },
  "factor_two":   { "enabled": false, "sort_by": null, "sort_direction": "desc" },
  "factor_three": { "enabled": false, "sort_by": null, "sort_direction": "desc" },
  "historical_date": null,
  "custom_filters": [
     { "enabled": true, "left": "ma_50", "op": ">=", "right": "ma_200" }
  ]
}
```

## Market health

```sql
CREATE TABLE market_health_daily (
  index_id smallint NOT NULL REFERENCES index_def(id),
  date     date NOT NULL,
  pct_above_200dma numeric(7,4),
  pct_above_50dma  numeric(7,4),
  pct_within_10pct_ath numeric(7,4),
  pct_ret_1y_positive  numeric(7,4),
  constituent_count integer,
  PRIMARY KEY (index_id, date)
);
```

## Accounts, billing, portfolios, backtests

```sql
CREATE TABLE app_user (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id text NOT NULL UNIQUE,
  email citext NOT NULL UNIQUE,
  password_hash text,                 -- argon2id; NULL for OTP-only users
  name text, email_verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE plan (
  id smallint PRIMARY KEY, code text UNIQUE,      -- 'monthly'|'yearly'|'forever'
  price_inr numeric(12,2) NOT NULL,
  interval text,                                   -- 'month'|'year'|NULL
  features jsonb NOT NULL
);

CREATE TABLE subscription (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  plan_id smallint NOT NULL REFERENCES plan(id),
  status text NOT NULL,               -- active|past_due|cancelled|expired
  started_at timestamptz NOT NULL, current_period_end timestamptz,
  razorpay_subscription_id text, razorpay_customer_id text
);

CREATE TABLE payment (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  subscription_id bigint REFERENCES subscription(id),
  amount_inr numeric(12,2) NOT NULL, gst_inr numeric(12,2),
  status text NOT NULL, razorpay_payment_id text UNIQUE,
  invoice_number text UNIQUE, invoice_pdf_key text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE portfolio (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES app_user(id),
  name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE portfolio_holding (
  portfolio_id bigint NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
  instrument_id bigint NOT NULL REFERENCES instrument(id),
  quantity numeric(20,4), avg_price numeric(18,4), added_on date NOT NULL,
  PRIMARY KEY (portfolio_id, instrument_id)
);

CREATE TABLE backtest (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  public_id text NOT NULL UNIQUE,
  user_id bigint NOT NULL REFERENCES app_user(id),
  screen_id bigint REFERENCES screen(id),
  config jsonb NOT NULL,     -- see docs/10
  status text NOT NULL,      -- queued|running|done|failed
  metrics jsonb, equity_curve jsonb, trades_key text,   -- large artefacts in R2
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE TABLE pipeline_run (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trade_date date NOT NULL, status text NOT NULL,
  started_at timestamptz NOT NULL, finished_at timestamptz,
  data_version bigint
);
CREATE TABLE pipeline_run_step (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id bigint NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
  step text NOT NULL, status text NOT NULL,
  rows_in bigint, rows_out bigint, duration_ms integer, error jsonb
);
```

## Retention & size estimates

| Table | Rows @ 15y | Notes |
|---|---|---|
| `ohlcv_daily` | ~8.5M | compressed after 90 days, ~10× |
| `factor_daily` | ~8.5M | the hot table; keep uncompressed for 2 years |
| `index_member_daily` | ~9M | compresses extremely well |
| `screen_run` | grows with usage | prune runs older than 400 days for free users |
