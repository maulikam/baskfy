# 03 — Data model: the `tw_` schema

Postgres, Alembic migration **`0041_twt.py`** in
`decile-blueprint/services/api/alembic/versions/`, revising **`0040_vbt_scan_run`** — the single
head on 11 Sep 2026 (`alembic heads` confirms it; the VBT pack records what two heads look like
when a concurrent session lands one, and the symptom is worth knowing). SQLAlchemy models in
`packages/core/src/baskfy_core/models/twt.py`.

Conventions inherited from `models/base.py`: `PRICE` (18,2) for prices and levels, `PRICE_RAW`
(18,4) where an exchange print must survive adjustment, `INR` (12,2) for money, `BREADTH` (7,4)
for breadth percentages, `BigIntPk`, `CreatedAt`, JSONB for `detail`. Every table carries
`user_id` (P4.1) and, where a broker is involved, `broker_account_id`. **Money and prices are
`numeric`, never `float`** (house rule 9); rounding happens at write time (house rule 8).

Nothing here replaces or edits an existing table. The joins are:

```
instrument (id, symbol, kite_token, listed_on, series, instrument_type)   ← every tw_ row
ohlcv_daily (instrument_id, date, o/h/l/c adjusted, close_raw, adj_factor, volume, turnover,
             upper_circuit)                                              ← the only bar source
trading_day (date, is_trading_day)                                       ← the calendar
desk journal (order_journal / fills, via packages/execution)             ← fills for tw_position
```

**What is deliberately not joined:** `factor_daily` and `market_health_daily`. TWT-1's 200-DMA and
its breadth are computed over the *whole scan universe* on the run's own thin-session calendar;
`market_health_daily.pct_above_200dma` is computed over an **index's point-in-time membership**,
which is a different measurement of a different population. Reusing it would change the gate that
produced every number in `01` §6. This is VB0.3's finding, inherited; DECISIONS-TW **TW0.4**
records why this sleeve stores its own row rather than reading VBT-1's.

---

## 1. `tw_config` — one row per user

| Column | Type | Meaning |
|---|---|---|
| `user_id` PK | bigint FK | sole user in this run |
| `sleeve_capital_inr` | INR | the cash this sleeve may deploy. **Default 0** — nothing is planned until Maulik sets it (`02` §3). Track A setting, user-editable, validated > 0 to plan |
| `max_open_positions` | smallint | default **10** (`SizingConfig.max_slots`); ≤ `BASKFY_TWT_MAX_OPEN_POSITIONS_MAX` [15] |
| `max_position_pct` | numeric(5,2) | default **12.50**; ≤ `BASKFY_TWT_MAX_POSITION_PCT_MAX` [15.00] |
| `stop_pct` | numeric(5,2) | default **20.00**; ≤ `BASKFY_TWT_STOP_PCT_MAX` [25.00]. A setting because `01` §7 measured 15–30 % and found the band flat |
| `trail_pct` | numeric(5,2) | default **20.00**; ≥ `BASKFY_TWT_TRAIL_PCT_MIN` [18.00]. **Bounded below, not above** (`02`): tightening is the cliff |
| `first_live_entries_left` | smallint | counts down from `SizingConfig.first_live_entries` [10] once execution is enabled; while > 0 **and** execution is enabled the plan is sized at `risk_multiplier_first_live` [0.5] × the slot (`04` §6.4). Decremented **once per filled entry** by the session that filled it (`tw_position.half_size` records which lines it applied to) — never by a request, and never by a session that took no entry |
| `dry_run_sessions` | smallint | how many `DRY_RUN` sessions have closed with a plan built and at least one line simulated. **Information, not a gate** (`02` §3) |
| `updated_at`, `updated_by` | | who last wrote the row |

### 1b. `tw_config_audit` — the history of that row

Shaped like `vb_config_audit`, `sw_config_audit` and the desk's `settings_audit`, written in the
same transaction as the change: `id`, `user_id`, `key` (the column that changed), `old_value`,
`new_value` (text, one row per field), `changed_at`, `changed_by` (a user id, or a job name for
the fields a job owns), `note`. `updated_at` cannot answer "what was `trail_pct` on the morning
that stop was armed"; this table can.

---

## 2. `tw_state_daily` — the scan's state, per session

PK `(user_id, date, instrument_id)`. One row per name **that holds the state** on the session —
about fifty a day. **Snapshotted, never recomputed** for a past date, the same rule as
`market_health_daily`, `sw_setup_daily` and `vb_signal_daily`: a recalibration must not rewrite
the record of what the system saw.

This table exists separately from §3 because TWT-1's scan is a **state** and its signal is the
*first day* of one. Storing only the signals would make "how long has this been tight" and "which
names left the state today" unanswerable, and both are on the page (`05` §2).

| Column | Type | From |
|---|---|---|
| `date`, `instrument_id` | | the session (a **closed** session; `04` §11) |
| `close`, `close_raw`, `high`, `low`, `open` | PRICE / PRICE_RAW | the bar, as stored: `close` adjusted, `close_raw` the exchange print |
| `adj_factor` | numeric(18,10) | the row's factor, so a later split is recognisable and a level can be turned back into an exchange price |
| `week_close_0/1/2` | PRICE | the three weekly closes the tight test compared — today's, and the last close of each of the two preceding weeks the calendar holds. Stored because the one rule anybody will dispute is this one |
| `week_range_pct` | numeric(10,4) | `(max/min − 1) × 100` — the tightness itself, ≤ 3.01 for a state row |
| `month_low_3` | PRICE | the low of the calendar month three months back |
| `month_low_ratio` | numeric(10,4) | `close / month_low_3` — ≥ 1.3 for a state row |
| `vol_sma_50`, `volume` | bigint | line 5's input |
| `turnover_inr`, `turnover_avg_20` | bigint ₹ | the liquidity floor's input, and the rank key's |
| `sma_dma` | PRICE | the 200-session mean close, so the breadth funnel is auditable from the same rows |
| `sessions_in_state` | smallint | how many consecutive sessions including this one the state has held; `1` on an entry day |
| `bars_in_window` | smallint | how many bars the 200-session window actually held, so the 10 % tolerance is auditable |
| `locked_upper_circuit` | bool | `upper_circuit > 0 and high ≥ upper_circuit`; kept and flagged, never dropped — the plan skips it |
| `pipeline_run_id` | bigint FK | which nightly run produced it (provenance, as `screen_run` has) |

Indexes: `(date, instrument_id)`; `(instrument_id, date)`.

---

## 3. `tw_signal_daily` — the entry events, per session

PK `(user_id, date, instrument_id)`. A **subset** of §2's rows: the state is true today and was
false on each of the previous `entry_min_sessions_out` [5] sessions. Written in the same
transaction as §2. A row is written for **every** entry event, including one the liquidity floor
rejects, so the funnel on the page is the funnel the strategy applied.

| Column | Type | Meaning |
|---|---|---|
| `date`, `instrument_id` | | the signal session |
| `state` | text | `SIGNAL` (the event held and the name cleared `min_turnover_inr`) or `SCAN_ONLY` (the event held, the liquidity floor did not). The second is kept for the same reason VBT-1 keeps its rejects: a system that stores only what it accepted cannot show a person what it passed over |
| `failed_filters` | text[] | `{TURNOVER}` for a `SCAN_ONLY` row, empty for a `SIGNAL` |
| `entry_reference_close` | PRICE | the signal session's close, as an exchange price — **not** an entry level. TWT-1 buys the next open at market; this number is on the page so the morning's fill can be read against it |
| `stop_preview` | PRICE | `entry_reference_close × (1 − stop_pct/100)`, snapped down to the tick — **a preview**; the real stop is set from the fill (`04` §7.1) |
| `sessions_out_before` | smallint | how many sessions the state had been false, ≥ 5 by construction; > 5 says how cold the name was |
| `rank_key` | bigint | what a same-session tie is broken by — **the signal session's own turnover**, `close_raw × volume` (`04` §6.3, and DECISIONS-TW **TW0.2** on why not the 20-day average) |
| `turnover_avg_20` | bigint ₹ | the liquidity floor's input, repeated here so the skip is readable without a join |
| `pipeline_run_id` | bigint FK | provenance |

Indexes: `(date, state, rank_key desc)` — the plan's one query; `(instrument_id, date)`.

---

## 4. `tw_breadth_daily` — the gate's reading, per session

PK `(user_id, date)`. Written by the same job as §2 and §3, in the same transaction.

| Column | Type | Meaning |
|---|---|---|
| `universe_count` | int | names in the scan universe with a bar on the date |
| `measured_count` | int | of those, the ones with a valid 200-DMA (the denominator) |
| `above_count` | int | of those, the ones whose close is above it |
| `pct_above_dma` | BREADTH | `above_count / measured_count × 100` |
| `gate` | text | `OPEN` when `pct_above_dma > BreadthConfig.min_pct_above_dma` [40.0], else `SHUT`. Two words: the strategy either may open new positions or may not |
| `dma_bars` | smallint | 200 — stored so a recalibration is visible in the history |
| `thin_session` | bool | this date was dropped from the rolling calendar (`04` §2); such a row is written for the record with `gate = SHUT` and null percentages |
| `detail` | JSONB | the funnel: universe → with a bar → with a 200-DMA → above it → in state → entries → signals, and `dropped_thin_sessions` in the window |

**Why this table exists when `vb_breadth_daily` holds the same measurement.** It is the same
reading over the same universe with the same threshold, and `baskfy_core.twt.breadth` calls
VBT-1's implementation so there is exactly **one** arithmetic (`04` §4.3). What is not shared is
the *row*: a sleeve whose gate is stored in another sleeve's table stops having a gate the day
that sleeve's nightly flag is set false, and the two thresholds are independent numbers that
happen to agree today. DECISIONS-TW **TW0.4**; the cost is one small table and the alternative is a
cross-sleeve dependency that nothing in `02` would allow.

The gate a plan reads is **the row of the signal session**, never a later one. `04` §11 is the
clock rule; TW4's look-ahead test shifts the series by one session and asserts the gate moves by
exactly one.

---

## 5. `tw_position` — the book

`BigIntPk`; one row per entry, **never per symbol** (a second entry into a name after an exit is a
second row). The sleeve's source of truth for what it owns; Track C §5 forbids selling anything
not here.

| Column | Meaning |
|---|---|
| `user_id`, `broker_account_id`, `instrument_id` | |
| `order_id` | FK to the `tw_order` that filled it |
| `signal_date` | the session whose close produced the signal |
| `entry_date`, `entry_avg` (PRICE_RAW), `quantity_entered` | from fills |
| `initial_stop` | 20 % under the fill, what it was at entry, for R |
| `stop_price` | the stop in force; **only ever rises** (asserted by a check constraint and by TW10) |
| **`high_since`** | PRICE_RAW — **the highest high since entry, the exchange print**. The ratchet's only input besides `trail_pct`. Initialised to the fill price and raised after every close (`04` §7.2) |
| `high_since_date` | the session that set it, so a page can say "the stop has not moved in 40 sessions" |
| `gtt_id`, `gtt_trigger`, `gtt_armed_at` | the resting GTT; `gtt_id` null with `quantity_open > 0` is an alert (`TWT_POSITION_NAKED`) |
| `next_trigger` | PRICE — **tomorrow's trailing trigger**, computed by the evening job (`04` §7.2), null when it does not exceed `gtt_trigger`. This is what makes the morning's ratchet line arithmetic that has already been done |
| `next_trigger_for` | date — the session `next_trigger` was computed for; a plan never reads a trigger computed for another session |
| `quantity_open` | 0 when closed |
| `state` | `OPEN` / `CLOSED` |
| `closed_on`, `exit_avg`, `close_reason` | `STOP_HIT` / `STOP_GAP` / `STOP_DAY0` / `NO_BAR` / `MANUAL`. **There is no `EMA_EXIT` and no `TIME_EXIT`** — TWT-1 has no exit that is not a stop (`01` §5), and a reason that exists is a reason somebody writes code for |
| `pnl_inr`, `return_pct`, `r_multiple`, `hold_sessions` | written at close |
| `simulated` | bool — true for every `DRY_RUN` / flag-off fill; the pages label them and the backtest card never mixes them |
| `half_size` | bool — sized at the first-live multiplier (`04` §6.4) |

`tw_fill`: one row per fill (`position_id`, `order_id`, `side`, `quantity`, `price`, `filled_at`,
`journal_ref`, `simulated`) so `entry_avg` and `exit_avg` are derivable and auditable.

**Why `high_since` is a column and not a query.** It could be recomputed from `ohlcv_daily` every
evening, and TW4 asserts that it agrees with such a recomputation. It is stored because the stop
that is resting at the exchange was derived from a specific number on a specific evening, and when
a corporate action re-writes the adjusted series underneath a 600-session hold, the question
"what was this stop derived from" must still have an answer. `high_since` is `PRICE_RAW` for the
same reason: the exchange trades in exchange prices.

---

## 6. `tw_order` — one entry order

`BigIntPk`. Simpler than VBT-1's `vb_order`: TWT-1 buys **at the next open, at market**, so there
is no working order, no expiry sweep and no `sessions_worked`. An order is proposed by an evening
plan, confirmed by a person the next morning, and either fills or does not.

| Column | Meaning |
|---|---|
| `user_id`, `broker_account_id`, `instrument_id` | |
| `signal_date` | the session whose close produced the signal; FK to `tw_signal_daily` |
| `side` | `BUY` or `SELL`. A `SELL` exists only for a `MANUAL` exit a person asked for; the strategy's own exits are the GTT's |
| `quantity` | the size the plan sized (`04` §6) |
| `stop_price` | PRICE — the 20 % stop this order's fill will be given |
| `state` | `PROPOSED` → `CONFIRMED` → `SENT` → (`FILLED` \| `PARTIAL` \| `CANCELLED` \| `REJECTED`) |
| `broker_order_id` | text, null until `SENT` |
| `client_id` | `plan_id:symbol` — the gateway's idempotency key, the desk's own convention (non-negotiable 6) |
| `filled_quantity`, `avg_fill_price` | from the journal |
| `position_id` | FK to `tw_position`, set on the first fill |
| `simulated` | bool — true for every `DRY_RUN` / flag-off order |
| `detail` | JSONB — the skip context, the sizing cap that bound, the gateway's answer |

Unique `(user_id, signal_date, instrument_id, side)` — one entry order per name per signal, so a
re-run of the evening cannot double-place. Index `(user_id, state)`.

---

## 7. `tw_plan`, `tw_plan_line`, `tw_plan_skip` — a session's plan

Mirrors the desk's plan lifecycle exactly: `plan_id` (uuid), `built_at`, **`expires_at = built_at
+ 30 min`**, `plan_hash` (sha256 of the canonical lines), `gate`, `sleeve_equity_inr`,
`total_new_exposure_inr`, `source` ∈ `EVENING` (built by the nightly job after the close) /
`MORNING` (rebuilt before the open from the same signals) / `MANUAL` (a desk rebuild).

`tw_plan_line`: `kind` ∈

* **`BUY_AT_OPEN`** — a signal from last night, `quantity`, `value_inr`, `stop_price` (a preview
  of the 20 % stop the fill will be given), and a note naming the cap that bound;
* **`ARM_GTT`** — a filled position with no resting stop: the same-session GTT of non-negotiable 4
  and the 15:15 sweep's re-arm;
* **`RAISE_GTT_STOP`** — **the ratchet**: a position whose `next_trigger` exceeds its resting
  `gtt_trigger`. `stop_price` is the new trigger, and the line carries `high_since` and the old
  trigger so the page can show the move;
* **`SELL_AT_OPEN`** — **present in the schema, produced by nothing in TWT-1.** The strategy has no
  end-of-day sell rule; the GTT is the exit (`01` §5). The kind exists so a person can be given a
  line for a `MANUAL` exit without a migration, and TW10 asserts that **no TWT rule ever emits
  one** — a check the swing and VBT packs did not need and this one does, because the shape it
  copied has such a rule and copying shapes is how rules get imported by accident;

plus `instrument_id`, `state` (`PROPOSED` / `CONFIRMED` / `SENT` / `FILLED` / `REJECTED` /
`EXPIRED` / `SKIPPED`), `client_id = plan_id:symbol:kind`, `journal_ref`, `order_id`,
`position_id`.

`tw_plan_skip`: `(plan_id, instrument_id, reason, detail)` — **a plan is not honest without them.**
`reason` is check-constrained to the `SkipReason` values of `04` §10.1: `GATE_SHUT`,
`ALREADY_HELD`, `SESSION_CAP`, `SLOTS_FULL`, `EXPOSURE_FULL`, `BELOW_MIN_TRADE_VALUE`,
`TURNOVER_CAP`, `LOCKED_UPPER_CIRCUIT`, `NO_SLEEVE_CAPITAL`, `NO_BAR`, `BELOW_LIQUIDITY_FLOOR`,
`STOP_NOT_BELOW_ENTRY`.

---

## 8. `tw_session` — one row per session the system ran

`session_date` PK (plus `user_id`): `mode` (`DRY_RUN` / `LIVE`), `plan_ids`, `states`, `signals`,
`orders_placed`, `confirms`, `fills`, `ratchets`, `exits`, `naked_at_1515`, `gate`, `notes`,
`first_live_entries_counted` (how many of `first_live_entries_left` this session consumed), and
`counted_for_dry_run` (set once, so a re-run of the evening does not inflate the counter).

`ratchets` is its own column because it is the number that says whether the sleeve's one new
mechanism actually ran: a book of ten open lines in a rising market should ratchet most nights,
and a week of zeroes with the market up is a bug, not a quiet spell.

---

## 9. `tw_backtest_run` — one row per run of the backtest (TW9)

`BigIntPk`. **Append-only**, exactly as `sw_backtest_run` and `vb_backtest_run` are: a re-run
inserts a new row and nothing edits a stored result, because the number that was on the page when
the flag was considered must survive a recalibration that produces a different one.

| Column | Meaning |
|---|---|
| `user_id` | Track C §6; the run is sized against `params.sleeve_inr`, **never** `tw_config.sleeve_capital_inr` |
| `params` | JSONB — `start`, `end`, `sleeve_inr`, `cost_bps_per_side` and the whole `TwtConfig`. Written on the way **in**, so a run that never finished still says what it was asked |
| `started_at`, `finished_at` | timestamptz; `finished_at` is set on success *and* on failure |
| `source` | `PLANT` (the run's own bars) or `RESEARCH_EXPORT` (TW2's reproduction against the research panel) — so the two can never be confused on the page |
| `stats` | JSONB — trades, the yearly and monthly tables, the equity curve, the funnel, the gate-on/gate-off comparison, and every price a string of its exact decimal. Null until the run finishes, forever if it failed |
| `drift` | JSONB — the comparison against `01` §6's published numbers: `cagr_pct_delta`, `max_dd_pct_delta`, `trades_delta`, and `flagged` when `abs(cagr_pct_delta) > 1.0` (TW9) |
| `error` | text — `"{ExceptionType}: {message}"` and the traceback; the exception is re-raised after it is recorded |

Index `(user_id, source, finished_at)`: the page's one query is this user's latest **finished** run
per source — the latest *finished*, not the latest started, so a run in flight or a failed re-run
never displaces the last good number.

---

## 10. What the worker reads to compute a session

```
calendar  = trading_day rows  → drop_thin_sessions(...)                  (04 §2)
bars      = ohlcv_daily ⋈ instrument (active, EQ, series EQ/BE/BZ, not ETF)
              for the last `bars_required` [260] trading sessions
            → with_twt_indicators(bars)  → tight_state(indicated, as_of) → tw_state_daily
                                        → entry_events(...)              → tw_signal_daily
breadth   = the same indicated frame at as_of → breadth_above_dma        → tw_breadth_daily
book      = tw_position OPEN rows + today's bar → ratchet(...)           → next_trigger
plan      = build_plan(signals, breadth, book, config, equity)           → tw_plan
```

`bars_required` is **260**, not VBT-1's 201: the 200-session average needs 200, and the month-3 low
needs the calendar month three months back, which on a long holiday-heavy quarter can sit 65
sessions behind the current month's first session. 260 covers both with room; `04` §2.3 gives the
arithmetic and TW1 asserts it with a test that walks a year of month boundaries.

Bars enter core **adjusted**; `close_raw` rides along because Chartink's line 1 and every level the
desk sends are exchange prices. Levels leave core adjusted and are converted by the task
(`level / adj_factor` of the as-of row) before they are stored or shown — the same rule the swing
and VBT trees follow, for the same reason: a split between the signal and the morning invalidates a
level that was never converted. **On this sleeve the rule has teeth it does not have elsewhere:** a
line is held for months, so a split *during* the hold is ordinary, and `04` §7.3 says what the
ratchet does about it.
