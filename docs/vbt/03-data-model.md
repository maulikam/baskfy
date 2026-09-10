# 03 — Data model: the `vb_` schema

Postgres, Alembic migration **`0037_vbt.py`** in
`decile-blueprint/services/api/alembic/versions/`, revising `0036_check_name`. SQLAlchemy models
in `packages/core/src/baskfy_core/models/vbt.py`.

> The pack said `0036`, because `0035_split_allocation` was the head the morning it was written.
> A concurrent session landed `0036_check_name` while VB1 and VB2 were being built, so VB3 chained
> onto that instead — `alembic heads` reported two heads, which is the only symptom a linear
> history gives you and is worth knowing to look for.

Conventions inherited from `models/base.py`: `PRICE` (18,2) for prices and levels, `PRICE_RAW`
(18,4) where an exchange print must survive adjustment, `INR` (12,2) for money, `BREADTH` (7,4)
for breadth percentages, `BigIntPk`, `CreatedAt`, JSONB for `detail`. Every table carries
`user_id` (P4.1) and, where a broker is involved, `broker_account_id`. **Money and prices are
`numeric`, never `float`** (house rule 9); rounding happens at write time (rule 8).

Nothing here replaces or edits an existing table. The joins are:

```
instrument (id, symbol, kite_token, listed_on, series, instrument_type)   ← every vb_ row
ohlcv_daily (instrument_id, date, o/h/l/c adjusted, close_raw, adj_factor, volume, turnover,
             upper_circuit)                                              ← the only bar source
trading_day (date, is_trading_day)                                       ← the calendar
desk journal (order_journal / fills, via packages/execution)             ← fills for vb_position
```

**What is deliberately not joined:** `factor_daily` and `market_health_daily`. VBT-1's 200-DMA
and its breadth are computed over the *whole scan universe* on the run's own thin-session
calendar; `market_health_daily.pct_above_200dma` is computed over an **index's point-in-time
membership**, which is a different measurement of a different population. Reusing it would
change the gate. See `DECISIONS-VB.md` **VB0.3**; `04` §3 defines the series this run stores.

---

## 1. `vb_config` — one row per user

| Column | Type | Meaning |
|---|---|---|
| `user_id` PK | bigint FK | sole user in this run |
| `sleeve_capital_inr` | INR | the cash this sleeve may deploy. **Default 0** — nothing is planned until Maulik sets it (`02` §3.4). Track A setting, user-editable, validated > 0 to plan |
| `max_open_positions` | smallint | default **10** (`SizingConfig.max_slots`); ≤ `BASKFY_VBT_MAX_OPEN_POSITIONS_MAX` [15] |
| `max_position_pct` | numeric(5,2) | default **12.50**; ≤ `BASKFY_VBT_MAX_POSITION_PCT_MAX` [15.00] |
| `stop_pct` | numeric(5,2) | default **12.00**; ≤ `BASKFY_VBT_STOP_PCT_MAX` [15.00]. The one exit number that is a setting, because STRATEGY §4 measured 10–15% and found it insurance either way |
| `first_live_sessions_left` | smallint | counts down from `SizingConfig.first_live_sessions` [5] once execution is enabled; while > 0 **and** execution is enabled the plan is sized at `risk_multiplier_first_live` [0.5] × the slot (`04` §5.4). Decremented by the evening job, once, when a LIVE `vb_session` closes (`vb_session.first_live_counted`) — never by a request |
| `dry_run_sessions` | smallint | how many `DRY_RUN` sessions have closed with a plan built and at least one line simulated. **This is `02` §3.1's gate**, not a display counter; written by the evening job, idempotent per session |
| `updated_at`, `updated_by` | | who last wrote the row |

### 1b. `vb_config_audit` — the history of that row

Shaped like `sw_config_audit` and the desk's `settings_audit`, written in the same transaction as
the change: `id`, `user_id`, `key` (the column that changed), `old_value`, `new_value` (text, one
row per field), `changed_at`, `changed_by` (a user id, or a job name for the fields a job owns),
`note`. `updated_at` cannot answer "what was `stop_pct` on the morning that trade was sized";
this table can.

---

## 2. `vb_signal_daily` — what the detector found, per session

PK `(user_id, date, instrument_id)`. One row per name per session. **Snapshotted, never
recomputed** for a past date — the same rule as `market_health_daily` and `sw_setup_daily`,
because a recalibration must not rewrite the record of what the system saw.

| Column | Type | From |
|---|---|---|
| `date`, `instrument_id` | | the signal session (a **closed** session; `04` §10) |
| `state` | text | `SIGNAL` (all eleven rules held) or `SCAN_ONLY` (the five Chartink lines held, a trend filter did not) — the second is kept because the ablation table is only readable if the run stores what it rejected |
| `failed_filters` | text[] | which of `A`…`F` failed, empty for a `SIGNAL` row |
| `close`, `close_raw`, `high`, `low`, `open` | PRICE / PRICE_RAW | the bar, as stored: `close` adjusted, `close_raw` the exchange print |
| `adj_factor` | numeric(18,10) | the row's factor, so a later split is recognisable and a level can be turned back into an exchange price |
| `limit_price` | PRICE | **the entry level: the signal-day close, as an exchange price** (`close_raw`), snapped to the tick |
| `stop_price` | PRICE | `limit_price × (1 − stop_pct/100)`, snapped down to the tick — a preview; the real stop is set from the fill (`04` §6.1) |
| `change_pct`, `rvol`, `close_position`, `ret_20_pct` | numeric(10,2) | the five lines' and A–F's inputs |
| `vol_sma_50`, `volume` | bigint | |
| `turnover_inr`, `turnover_avg_20` | bigint ₹ | F's input |
| `sma_200`, `ema_21`, `high_20_prior` | PRICE | A, the exit reference, B |
| `rank_key` | bigint | what a same-session tie is broken by — the signal-day turnover (`04` §5.3) |
| `locked_upper_circuit` | bool | `upper_circuit > 0 and high ≥ upper_circuit`; kept and flagged, never dropped — the plan skips it |
| `bars_in_window` | smallint | how many bars the 200-session window actually held, so the 10% tolerance is auditable |
| `pipeline_run_id` | bigint FK | which nightly run produced it (provenance, as `screen_run` has) |

Indexes: `(date, state, rank_key desc)`; `(instrument_id, date)`.

---

## 3. `vb_breadth_daily` — the gate's reading, per session

PK `(user_id, date)`. Written by the same job as §2, in the same transaction.

| Column | Type | Meaning |
|---|---|---|
| `universe_count` | int | names in the scan universe with a bar on the date |
| `measured_count` | int | of those, the ones with a valid 200-DMA (the denominator) |
| `above_count` | int | of those, the ones whose close is above it |
| `pct_above_dma` | BREADTH | `above_count / measured_count × 100` |
| `gate` | text | `OPEN` when `pct_above_dma > BreadthConfig.min_pct_above_dma` [40.0], else `SHUT`. Two words, because unlike the swing gate there is no amber: the strategy either may open new positions or may not |
| `dma_bars` | smallint | 200 — stored so a recalibration is visible in the history |
| `thin_session` | bool | this date was dropped from the rolling calendar (`04` §2); such a row is written for the record with `gate = SHUT` and null percentages |
| `detail` | JSONB | the funnel: universe → with a bar → with a 200-DMA → above it → signals → scan-only, and `dropped_thin_sessions` in the window |

The gate a plan reads is **the row of the signal session**, never a later one. `04` §10 is the
clock rule; VB4's look-ahead test shifts the series by one session and asserts the gate moves by
exactly one.

---

## 4. `vb_order` — the working limit order (the piece the desk does not have today)

`BigIntPk`. **This is VB7's table and the run's one genuinely new mechanism.** A VBT entry is a
limit at the signal-day close that works for three sessions and is then cancelled.

| Column | Meaning |
|---|---|
| `user_id`, `broker_account_id`, `instrument_id` | |
| `signal_date` | the session whose close set the limit; FK to `vb_signal_daily` |
| `limit_price` | PRICE, exchange price, snapped to the tick |
| `quantity` | the size the plan sized (`04` §5) |
| `stop_price` | PRICE — the 12% stop this order's fill will be given |
| `working_from`, `expires_after_session` | dates: the first session the order may fill on, and **the last** (`signal_date` + `entry.valid_sessions` [3] trading sessions, on the run's calendar) |
| `sessions_worked` | smallint — how many sessions it has actually been live for; the expiry sweep reads this, never a clock |
| `state` | `PROPOSED` → `CONFIRMED` → `SENT` → (`FILLED` \| `PARTIAL` \| `CANCELLED` \| `EXPIRED` \| `REJECTED`). `PROPOSED` is a plan line nobody has confirmed; `EXPIRED` is the sweep's verdict, `CANCELLED` is a person's or the sweep's cancel at the broker |
| `broker_order_id` | text, null until `SENT` |
| `client_id` | `plan_id:symbol:BUY` — the gateway's idempotency key, as the desk does |
| `filled_quantity`, `avg_fill_price` | from the journal; a partial keeps working until it expires |
| `position_id` | FK to `vb_position`, set on the first fill |
| `cancelled_on`, `cancel_reason` | `EXPIRY_SWEEP` / `MANUAL` / `GATE_SHUT` / `SLOT_TAKEN` |
| `simulated` | bool — true for every `DRY_RUN` / flag-off order |
| `detail` | JSONB — the skip context, the sizing cap that bound, the gateway's answer |

Unique `(user_id, signal_date, instrument_id)` — one working order per name per signal, so a
re-run of the evening cannot double-place. Index `(user_id, state, expires_after_session)`, which
is exactly the sweep's query.

**Why `expires_after_session` is a date and `sessions_worked` a count.** A limit that rests over
a holiday has not worked a session. The sweep counts sessions on the plant's calendar (thin
sessions excluded, `04` §2), so "three sessions" means the same thing in the book as it does in
the backtest.

---

## 5. `vb_position` — the book

`BigIntPk`; one row per entry, **never per symbol** (a second entry into a name after an exit is
a second row). The sleeve's source of truth for what it owns; Track C §5 forbids selling
anything not here.

| Column | Meaning |
|---|---|
| `user_id`, `broker_account_id`, `instrument_id` | |
| `order_id` | FK to the `vb_order` that filled it |
| `entry_date`, `entry_avg` (PRICE_RAW), `quantity_entered` | from fills |
| `stop_price` | the stop in force; **only ever rises** (asserted) |
| `initial_stop` | what it was at entry, for R |
| `gtt_id`, `gtt_trigger`, `gtt_armed_at` | the resting GTT; `gtt_id` null with `quantity_open > 0` is an alert (`VBT_POSITION_NAKED`) |
| `quantity_open` | 0 when closed |
| `state` | `OPEN` / `CLOSED` |
| `exit_queued_for` | date, nullable — the session whose open this position is to be sold at, written by the evening when the close fell below the 21-EMA (`04` §6.2). Null means hold |
| `exit_reason_queued` | `EMA_EXIT` when queued |
| `closed_on`, `exit_avg`, `close_reason` | `EMA_EXIT` / `STOP_HIT` / `STOP_GAP` / `NO_BAR` / `MANUAL` |
| `pnl_inr`, `return_pct`, `r_multiple`, `hold_sessions` | written at close |
| `simulated` | bool — true for every `DRY_RUN` / flag-off fill; the pages label them and the backtest card never mixes them |
| `half_risk` | bool — sized at the first-live multiplier (`04` §5.4) |

`vb_fill`: one row per fill (`position_id`, `order_id`, `side`, `quantity`, `price`, `filled_at`,
`journal_ref`, `simulated`) so `entry_avg` and `exit_avg` are derivable and auditable.

---

## 6. `vb_plan`, `vb_plan_line`, `vb_plan_skip` — a session's plan

Mirrors the desk's plan lifecycle exactly: `plan_id` (uuid), `built_at`, **`expires_at = built_at
+ 30 min`**, `plan_hash` (sha256 of the canonical lines), `gate`, `sleeve_equity_inr`,
`total_new_exposure_inr`, `source` ∈ `EVENING` (built by the nightly job after the close) /
`MORNING` (rebuilt before the open from the same signals) / `MANUAL` (a desk rebuild).

`vb_plan_line`: `kind` ∈
* **`PLACE_LIMIT`** — a new working order at a signal's close, with `quantity`, `limit_price`,
  `stop_price`, `value_inr`, and a note naming the cap that bound;
* **`SELL_AT_OPEN`** — a position whose close fell below its 21-EMA, `quantity = quantity_open`;
* **`CANCEL_LIMIT`** — a working order at the end of its third session (VB7's sweep line);
* **`ARM_GTT`** — a filled position with no resting stop (the re-arm path);

plus `instrument_id`, `state` (`PROPOSED` / `CONFIRMED` / `SENT` / `FILLED` / `REJECTED` /
`EXPIRED` / `SKIPPED`), `client_id = plan_id:symbol:kind`, `journal_ref`, `order_id`,
`position_id`.

`vb_plan_skip`: `(plan_id, instrument_id, reason, detail)` — **a plan is not honest without
them.** `reason` is check-constrained to the `SkipReason` values of `04` §9.1: `GATE_SHUT`,
`ALREADY_HELD`, `ALREADY_WORKING`, `SESSION_CAP`, `SLOTS_FULL`, `EXPOSURE_FULL`,
`BELOW_MIN_TRADE_VALUE`, `TURNOVER_CAP`, `LOCKED_UPPER_CIRCUIT`, `NO_SLEEVE_CAPITAL`,
`STOP_NOT_BELOW_ENTRY`.

---

## 7. `vb_session` — one row per session the system ran

`session_date` PK (plus `user_id`): `mode` (`DRY_RUN` / `LIVE`), `plan_ids`, `signals`,
`orders_placed`, `orders_expired`, `confirms`, `fills`, `exits`, `gate`, `notes`,
`first_live_counted`, and `counted_for_dry_run_gate` (set once, so `02` §3.1's twenty is counted
once per session and a re-run of the evening does not inflate it).

---

## 8. `vb_backtest_run` — one row per run of the backtest (VB9)

`BigIntPk`. **Append-only**, exactly as `sw_backtest_run` is: a re-run inserts a new row and
nothing edits a stored result, because the number that was on the page when the flag was
considered must survive a recalibration that produces a different one.

| Column | Meaning |
|---|---|
| `user_id` | Track C §6; the run is sized against `params.sleeve_inr`, never `vb_config.sleeve_capital_inr` |
| `params` | JSONB — `start`, `end`, `sleeve_inr`, `cost_pct_per_side` and the whole `VbtConfig`. Written on the way **in**, so a run that never finished still says what it was asked |
| `started_at`, `finished_at` | timestamptz; `finished_at` is set on success *and* on failure |
| `source` | `PLANT` (the run's own bars) or `RESEARCH_EXPORT` (VB2's reproduction against `research/volume-breakout/aws/`) — so the two can never be confused on the page |
| `stats` | JSONB — trades, the yearly and monthly tables, the equity curve, the funnel, the gate-on/gate-off comparison, and every price a string of its exact decimal. Null until the run finishes, forever if it failed |
| `drift` | JSONB — the comparison against STRATEGY §4's published numbers: `cagr_pct_delta`, `max_dd_pct_delta`, `trades_delta`, and `flagged` when `abs(cagr_pct_delta) > 1.0` (VB9) |
| `error` | text — `"{ExceptionType}: {message}"` and the traceback; the exception is re-raised after it is recorded |

Index `(user_id, source, finished_at)`: the page's one query is this user's latest **finished**
run per source — the latest *finished*, not the latest started, so a run in flight or a failed
re-run never displaces the last good number.

---

## 9. What the worker reads to compute a session

```
calendar  = trading_day rows  → drop_thin_sessions(...)          (04 §2)
bars      = ohlcv_daily ⋈ instrument (active, EQ, series EQ/BE/BZ, not ETF)
              for the last `bars_required` [201] trading sessions
            → with_vbt_indicators(bars)  → detect_signals(indicated, as_of)
breadth   = the same indicated frame at as_of → breadth_above_dma  → vb_breadth_daily
book      = vb_position OPEN rows + today's bar + ema_21 → exits.manage → SELL_AT_OPEN lines
orders    = vb_order WORKING rows → expire_orders(as_of) → CANCEL_LIMIT lines
plan      = build_plan(signals, breadth, book, orders, config, equity)
```

Bars enter core **adjusted**; `close_raw` rides along because Chartink's line 2 and every level
the desk sends are exchange prices. Levels leave core adjusted and are converted by the task
(`level / adj_factor` of the as-of row) before they are stored or shown — the same rule the swing
tree follows, for the same reason: a split between the signal and the morning invalidates a level
that was never converted.
