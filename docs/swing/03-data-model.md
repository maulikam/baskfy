# 03 — Data model: the `sw_` schema

Postgres, Alembic migration `0028_swing.py` (`0029_swing_backtest.py` for §10;
`0030_swing_primary_sources.py` for SW9.5's drawdown columns, two defaults and the two new skip
reasons; `0031_swing_review_corrections.py` for SW10.5's review corrections — the watch
funnel's `score` / `adr_pct` / `focus` / `reconfirmed_on`, the journal's `half_risk` tag, the
session's `first_live_counted`, and the `PENDING_RANGE` line kind; `0032_swing_catalyst.py`
for SW11B's `sw_catalyst` and the `sw_watch.earnings_date` flag) in
`services/api/alembic/versions/`, SQLAlchemy
models in `packages/core/src/baskfy_core/models/swing.py`. Conventions inherited from
`models/base.py`: `PRICE` (18,2) for prices and levels, `PRICE_RAW` (18,4) where an exchange
print must survive adjustment, `INR` (12,2) for money, `BREADTH` (7,4) for breadth percentages,
`BigIntPk`, `CreatedAt`, JSONB for `detail`. Every table carries `user_id` (P4.1) and, where a
broker is involved, `broker_account_id`. Money and prices are `numeric`, never `float` (house
rule 9); rounding happens at write time (rule 8).

Nothing here replaces an existing table. The joins are:

```
instrument (id, symbol, kite_token, listed_on, series)      ← every sw_ row's instrument_id
ohlcv_daily (instrument_id, date, o/h/l/c adjusted, close_raw, adj_factor, turnover, upper_circuit)
factor_daily (instrument_id, date, ret_1m/3m/6m, ma_20/50, high_1y, vol_avg_1m)   ← Leaders tab, breadth
market_health_daily (index_id, date, pct_above_20dma …)      ← sector strip
index_snapshot_daily (index_id, date, close)                  ← the index reading for the gate
desk journal (order_journal / fills, via packages/execution)  ← fills for sw_position
```

## 1. `sw_config` — one row per user

| Column | Type | Meaning |
|---|---|---|
| `user_id` PK | bigint FK | sole user in this run |
| `sleeve_capital_inr` | INR | the cash the swing book may deploy. **Default 0** — nothing is planned until Maulik sets it (Track A setting, user-editable, validated > 0 to plan) |
| `risk_per_trade_pct` | numeric(5,3) | default 0.500; **must be ≤ `BASKFY_SWING_RISK_PER_TRADE_PCT_MAX`** (system-only env, default 1.0) — the M4.1 boundary |
| `max_position_pct` | numeric(5,2) | default 20.00; ≤ `BASKFY_SWING_MAX_POSITION_PCT_MAX` (default **30.00** since SW9.5 — "never more than 30% of your account over night in any stock"; was 25.00) |
| `max_open_positions` | smallint | default **10** since SW9.5 (was 8; his "typically 5-10 positions"); ≤ `BASKFY_SWING_MAX_OPEN_POSITIONS_MAX` (default **20**, was 10; his "15-20 in a good market"). The plan takes `min(rung, this)` (`04` §9.1) — the worker's `load_swing_config` hands the plan this row's three sizing knobs (SW9.5.3) |
| `or_window_minutes` | smallint | 1 / 5 / 60; default 5 |
| `stop_mode` | text | `LOW_OF_DAY` / `OPENING_RANGE_LOW`; default `LOW_OF_DAY` |
| `adr_min_pct`, `turnover_min_inr`, `price_min` | numeric | the liquidity floors; defaults **4.0** (since SW9.5, was 3.5; user-raisable) / 5e7 / 20 |
| `exposure_level` | smallint | the ladder rung in force, 0–3; written by `swing-eod`, never by a form |
| `first_live_sessions_left` | smallint | counts down from 5 (`SizingConfig.first_live_sessions`) once execution is enabled; while > 0 **and** execution is enabled the plan is sized at `risk_multiplier_first_live` [0.5] × `risk_per_trade_pct` (`02` §3.5, `04` §5.4). **Decremented by `swing-eod`, once, when a LIVE `sw_session` closes** (`sw_session.first_live_counted` records which sessions were counted, so a re-run of the evening and a desk restart mid-countdown change nothing) — never by a request (SW10.5, STANDING-ANSWERS A9; until then `/swing/execute` halved the sent quantity and counted in-process) |
| `sleeve_peak_inr` | INR, nullable | the highest EOD NAV the sleeve has reached (`04` §8.5); **null until the first evening has run** — a sleeve with no session behind it is at its peak, not in drawdown. Written by `swing-eod`, only ever raised — except the night the ladder switches books (PACK.6), when it starts over at that night's NAV (SW9.5.1) |
| `drawdown_pct` | numeric(10,2) | how far below `sleeve_peak_inr` tonight's NAV sits, `(peak − nav) / peak × 100`; 0 at or above the peak, 0 when the peak is not positive. Written by `swing-eod` |
| `drawdown_locked` | bool | the lock-out in force for the next session (`04` §8.5, hysteresis: on at 15%, off inside 10%). Written by `swing-eod`, **audited** on every change (`swing-eod` in `sw_config_audit`, the NAV and the peak in the note); `sleeve_peak_inr` and `drawdown_pct` are measurements and are not audited — the day's `sw_market_daily` row is their history |
| `updated_at`, `updated_by` | | who last wrote the row |

**The sleeve's EOD NAV** (SW9.5.1) — nothing in `portfolio_nav_daily` describes this sleeve, so
`swing-eod` computes it from the book the ladder reads (PACK.6: the simulated positions until
execution is enabled, the real ones after): `sleeve_capital_inr` + Σ `pnl_inr` of `CLOSED`
positions closed on or before the session + Σ `(mark − entry_avg) × quantity_open` of `OPEN` /
`PARTIAL` positions (the mark is the latest `ohlcv_daily.close` on or before the session; the
entry when there is none yet) + Σ `(price − entry_avg) × quantity` over the `SELL` fills of those
still-open positions (a partial's realised half, which no column carries until the position
closes). `tasks/swing.py::sleeve_nav`.

### 1b. `sw_config_audit` — the history of that row (SW2.2)

`updated_at`/`updated_by` say who touched the settings last; they cannot say what
`risk_per_trade_pct` **was** on the morning a trade was sized, which is the question the desk's
own `settings_audit` exists to answer ("You cannot reconstruct why a trade was sized the way it
was without knowing what the parameters were at the time"). So the audit is a table, shaped like
the desk's and written in the same transaction as the change.

| Column | Meaning |
|---|---|
| `id` `BigIntPk`, `user_id` | |
| `key` | the `sw_config` column that changed, e.g. `risk_per_trade_pct` |
| `old_value`, `new_value` | rendered as text, as the desk's table does — one row **per field** |
| `changed_at`, `changed_by` | a user id, or a job name for the fields a job owns (`swing-eod` writes `exposure_level` and, since SW10.5, `first_live_sessions_left`) |
| `note` | free text, e.g. the reason a ceiling-bound value was lowered |

Every threshold **not** listed here (base geometry, EP gap, ladder tiers…) is a
`baskfy_core.swing.config` field with the pack's default and is not a setting in this run
(PACK.5). Changing one is a code change with a DECISIONS-SW entry.

## 2. `sw_setup_daily` — what the detectors found, per day

PK `(user_id, date, instrument_id, setup)` — `user_id` leads because Track C §6 requires it on
every `sw_` row and the liquidity floors that decide who is a candidate live in `sw_config`,
which is per user (SW2.1). One row per candidate per setup per day; **snapshotted,
never recomputed** for a past date (the same rule as `market_health_daily`), because a
detector recalibration must not rewrite the record of what the system saw.

| Column | Type | From |
|---|---|---|
| `date`, `instrument_id`, `setup` | | `CANDIDATE_COLUMNS` |
| `status` | text | `SETTING_UP` / `BREAKOUT_TODAY` / `GAP_DAY` / `RUNNING` / `EXHAUSTION` |
| `score` | numeric(5,2) | 0–100 |
| `close`, `trigger`, `stop_ref`, `pivot_high` | PRICE | **exchange prices** — the worker divides the adjusted level by `adj_factor` at write time and records `adj_factor` alongside |
| `adj_factor` | numeric(18,10) | the row's factor, so a later split can be recognised |
| `adr_pct`, `prior_move_pct`, `base_depth_pct`, `tightness_adr`, `dryup_ratio`, `dist_ma_fast_pct`, `dist_ma_slow_pct`, `rvol`, `gap_pct` | numeric(10,2) | nullable per setup |
| `turnover_avg` | bigint ₹ | |
| `base_bars`, `up_streak` | smallint | |
| `locked_upper_circuit` | bool | |
| `sector_slug` | text | the instrument's sector index at `date`, from `index_member_daily`; nullable |
| `listed_within_2y` | bool | `instrument.listed_on` ≥ date − 730 days |
| `pipeline_run_id` | bigint FK | which nightly run produced it (provenance, as `screen_run` has) |

Index: `(date, setup, score desc)`; `(instrument_id, date)`.

## 3. `sw_market_daily` — breadth and the gate, per day

PK `(user_id, date)` — same reason as §2 (SW2.1): breadth is measured over *this user's*
liquid universe. Written by `swing-eod`.

| Column | Type |
|---|---|
| `constituent_count` | int — the liquid universe that day |
| `pct_up_strong_1m`, `pct_new_52w_high`, `pct_above_ma_slow` | BREADTH |
| `index_slug`, `index_close`, `index_ma_fast`, `index_ma_slow` | the reading used (`nifty-500`, fallback `nifty-50`) |
| `gate` | `GREEN` / `AMBER` / `RED` |
| `exposure_level`, `max_open_positions`, `max_exposure_pct`, `new_entries_allowed` | the tier for the next session |
| `drawdown_pct`, `drawdown_locked` | `04` §8.5 (SW9.5): the sleeve's drawdown from its peak at this close, and whether the lock-out is in force for the next session — kept beside the rung so a rung of 0 on a GREEN day explains itself. The detection job writes its preview from `sw_config`'s stored peak; `swing-eod`'s settlement is authoritative and overwrites it |
| `parabolic_count` | int — how many `PARABOLIC_SHORT` rows today; froth gauge |
| `detail` | JSONB — the closed-trade R list the ladder read, so the rung is explainable; since SW9.5 also `drawdown: {nav, peak, pct, was_locked, locked}`, the settlement's inputs |

## 4. `sw_watch` — the watchlist with levels

`BigIntPk`; unique `(user_id, instrument_id, setup, added_on)`.

| Column | Meaning |
|---|---|
| `instrument_id`, `setup`, `source` | `source` ∈ `DETECTOR` / `MANUAL` |
| `added_on`, `expires_on` | flags expire after 10 sessions without a trigger, EPs after `ep.valid_bars` (3); **MANUAL rows after `watch.manual_valid_bars` (10) sessions unless re-confirmed** (SW10.5, STANDING-ANSWERS A14 — until then a MANUAL row never expired) |
| `trigger`, `stop_ref` | exchange prices, refreshed by `swing-premarket` from the latest bar; a MANUAL row keeps what Maulik typed. A live gap found at 09:09 has a `trigger` and **no `stop_ref`** until the opening range sets one — the MORNING plan shows it as a `PENDING_RANGE` line (§6) |
| `setup_daily_date` | FK back to the `sw_setup_daily` row it came from (nullable for MANUAL) |
| `score` | numeric(5,2), nullable — what the row is ranked by (SW10.5, A14): the detection row's score at watch time; for a live gap the provisional EP score the pre-open knows (`04` §7.3); null for a MANUAL row nobody scored (ranks at zero) |
| `adr_pct` | numeric(10,2), nullable — the name's ADR when the row was written, read by the plan and the monitor when no detection row exists (a live gap, a MANUAL name), so a stop can be measured against one ADR (`04` §6.1, SW9.5.2) |
| `focus` | bool — **the daily focus** (A14): the top `watch.focus_top_n` [5] flags by score plus every EP, recomputed by `swing-eod` and by `swing-premarket` after the gap scan (`swing_watch.refresh_focus`). The desk page puts focus names on top; the notifier (SW11) pushes only these; the rest are watched, signalled and logged below the fold |
| `reconfirmed_on` | date, nullable — when a person last re-confirmed a MANUAL row (`PATCH /swing/watch/{id}` with `reconfirm: true`); `expires_on` runs `manual_valid_bars` sessions from it |
| `note`, `catalyst` | free text (the "news check"). Since SW11B `catalyst` is **auto-filled** from the newest `sw_catalyst` headline (§11) only while empty; typed text is never overwritten |
| `earnings_date` | date, nullable — **the earnings flag** (SW11B, STANDING-ANSWERS A3): the nearest result meeting NSE's event calendar lists on or after the morning the feed ran; refreshed every run, NULL when none |
| `state` | `WATCHING` / `TRIGGERED` / `EXPIRED` / `DISMISSED` |

The web app may add/dismiss/annotate/re-confirm rows (they move no money). Nothing else on `/swing` mutates.

## 5. `sw_signal` — what the monitor raised

`BigIntPk`. Append-only.

| Column | Meaning |
|---|---|
| `watch_id` FK, `instrument_id`, `setup` | |
| `session_date`, `raised_at` (timestamptz) | |
| `state` | `TriggerState` value — `TRIGGERED` rows are what the desk page shows; `LOCKED_UPPER_CIRCUIT` / `BELOW_PIVOT` are kept for the record |
| `or_window_minutes`, `range_high`, `range_low`, `low_of_day`, `last_price` | the verdict's inputs |
| `entry`, `stop` | the verdict's outputs |
| `plan_line_id` | FK, set when the signal became a plan line |

## 6. `sw_plan` and `sw_plan_line` — a day's plan

Mirrors the desk's plan lifecycle: `plan_id` (uuid), `built_at`, **`expires_at = built_at +
30 min`**, `plan_hash` (`SwingPlan.plan_hash()`), `gate`, `exposure_level`, `total_risk_inr`,
`total_new_exposure_inr`, `source` ∈ `EOD_PREVIEW` / `MORNING` / `SIGNAL`. A plan built by the
EOD job is a **preview**; the morning plan is rebuilt at 09:10 from the same watchlist with
pre-open prices, and a `SIGNAL` plan is built the moment a trigger fires (one line).

`sw_plan_line`: `kind` (`BUY_ON_TRIGGER` / `SELL_AT_OPEN` / `RAISE_GTT_STOP` /
**`PENDING_RANGE`** — SW10.5, A7: a live gap on the MORNING plan with `quantity` 0 and `stop`
null, reserving one of the session's new-entry slots, never executable; `0031` rebuilt the
`kind` constraint), `instrument_id`,
`setup`, `quantity`, `trigger`, `stop`, `risk_inr`, `position_value`, `trail`, `note`, `state`
(`PROPOSED` / `CONFIRMED` / `SENT` / `FILLED` / `REJECTED` / `EXPIRED` / `SKIPPED`),
`client_id = plan_id:symbol:kind` (the gateway's idempotency key, as the desk does), `journal_ref`.
A `PENDING_RANGE` line's states: `PROPOSED` while the slot is reserved; `SKIPPED` once the name
has triggered (its SIGNAL plan lined or skipped it — the reservation is spent either way);
`EXPIRED` when the 10:45 sweep frees a slot nothing claimed. A `SENT` line of a live LIMIT
buy (A8) carries the broker's order id in `journal_ref` and, once any of it has filled, its
`position_id`; the desk's `on_order_update` handler raises the position and its GTT as more
fills arrive, and the 10:45 sweep cancels what is still open.

`sw_plan_skip`: `(plan_id, instrument_id, reason, detail)` — the `Skipped` tuples. A plan is
not honest without them. `reason` is check-constrained to `SkipReason`; `0030` rebuilt the
constraint with SW9.5's `SESSION_CAP` and `DRAWDOWN_LOCKOUT`.

## 7. `sw_position` — the book

`BigIntPk`; one row per entry, **never per symbol** (a second entry into a name after an exit
is a second row).

| Column | Meaning |
|---|---|
| `instrument_id`, `setup`, `user_id`, `broker_account_id` | |
| `entry_date`, `entry_avg` (PRICE_RAW), `quantity_entered` | from fills |
| `initial_stop`, `stop` | the stop in force; `stop` only ever rises (asserted) |
| `gtt_id`, `gtt_trigger`, `gtt_armed_at` | the resting GTT; `gtt_id` null with `quantity_open > 0` is an **alert** (`SWING_POSITION_NAKED`) |
| `trail` | `MA10` / `MA20` |
| `partial_done`, `partial_date` | |
| `quantity_open` | after partials; 0 when closed |
| `state` | `OPEN` / `PARTIAL` / `CLOSED` |
| `closed_on`, `exit_avg`, `close_reason` | `ActionReason` value or `MANUAL` |
| `r_multiple`, `pnl_inr` | written at close from `journal.ClosedTrade` |
| `simulated` | bool — **true for every DRY_RUN / flag-off fill**; the journal page labels them. The ladder reads real closes only, from day one (SW10.5, A10; PACK.6's paper clause is void) |
| `half_risk` | bool — the entry was planned at the first-live `risk_multiplier` (0.5 × `risk_per_trade_pct`; `04` §5.4, A9). The journal's tag — a column only until SW11 surfaces it on the page |

`sw_fill`: one row per fill (`position_id`, `side`, `quantity`, `price`, `filled_at`,
`journal_ref`, `simulated`), so `exit_avg` is derivable and auditable.

## 8. `sw_session` — one row per session the system ran

`session_date` PK, `mode` (`DRY_RUN` / `LIVE`), `monitor_ran`, `plan_ids`, `signals`,
`confirms`, `fills`, `manage_actions`, `notes`, and `first_live_counted` (SW10.5, A9: the
evening decremented `sw_config.first_live_sessions_left` for this LIVE session — set once, read
back on a re-run so the countdown moves once per session). This is the paper track record `02`
§3.2 counts.

## 9. What the worker reads to compute a day

```
bars      = ohlcv_daily ⋈ instrument (active, series in EQ/BE) for the last 200 trading days
                → with_swing_indicators(bars) → detect_setups(indicated, as_of)
breadth   = the liquid rows at as_of + factor_daily.high_1y  → breadth_snapshot
index     = index_snapshot_daily for nifty-500 (10/20-bar SMA computed in the task)
results   = sw_position closed rows, last 5 by closed_on → exposure_tier
positions = sw_position open rows + today's bar + ma10/ma20 → stops.manage → exit lines
watch     = sw_watch WATCHING rows → WatchItem (exchange prices) → build_entries → plan
```

Bars enter core **adjusted**; levels leave core adjusted and are converted to exchange prices
by the task (`level / adj_factor` of the as-of row) before they are stored or shown. A split
between detection and the morning invalidates the level: `swing-premarket` recomputes from the
latest bar rather than trusting last night's number.

## 10. `sw_backtest_run` — one row per run of the EOD backtest (SW9)

`BigIntPk`; migration `0029_swing_backtest.py`; model `SwBacktestRun`. **Append-only**: a re-run
inserts a new row and nothing edits a stored result, for the same reason `sw_setup_daily` is a
snapshot — the number that was on the page when the flag was considered must survive a detector
recalibration that produces a different one.

| Column | Meaning |
|---|---|
| `user_id` | Track C §6, as on every `sw_` row; the run is sized against `params.sleeve_inr`, never `sw_config.sleeve_capital_inr` |
| `params` | JSONB — `BacktestParams` as JSON: `start`, `end`, `sleeve_inr`, `cost_pct_per_side` and the whole `config` (the user's liquidity floors, the pack's defaults for everything else). Written on the way **in**, so a run that never finished still says what it was asked |
| `started_at`, `finished_at` | timestamptz; `finished_at` is set on success *and* on failure |
| `stats` | JSONB — `BacktestResult.to_json()` stored as-is: `params, trades, stats, by_setup, by_year, equity_curve, funnel, ladder, caveats`, every price a string of its exact decimal. Null until the run finishes, forever if it failed |
| `error` | text — `"{ExceptionType}: {message}"` and the traceback when the run raised; the exception is re-raised after it is recorded, so Celery sees the failure too |

Index `(user_id, finished_at)`: the journal's one query is this user's latest **finished** run
(`finished_at` set, `error` null) — the latest *finished*, not the latest started, so a run in
flight or a failed re-run never displaces the last good number on `/swing/journal`.

## 11. `sw_catalyst` — a headline, a stamp and a link per watched name (SW11B)

`BigIntPk`; migration `0032_swing_catalyst.py`; model `SwCatalyst`; unique
`(user_id, instrument_id, url)`; index `(user_id, instrument_id, published_at)`. Written by
`baskfy.swing.catalyst` (Beat 09:10 IST weekdays) for the WATCHING names plus the last
session's EP candidates, from the NSE provider's `announcements` and `results_calendar` reads
(cookie discipline, the shared limiter, archive-then-parse, one archived request per symbol per
day). **Never the filing's text** — STANDING-ANSWERS A3 and the Track C §7 amendment: the feed
is single-tenant own-use, it links out, nothing is redistributed. Idempotent: the same filing
is one row however many mornings see it. Fail soft: a provider error is a note on the step and
zero rows, never a raise into the morning.

| Column | Meaning |
|---|---|
| `user_id`, `instrument_id` | Track C §6; both cascade |
| `headline` | text — NSE's subject line plus its own one-line summary, capped at 160 characters (`nse.HEADLINE_MAX_CHARS`); for a calendar row, the meeting's purpose |
| `published_at` | timestamptz, nullable — the exchange's stamp (IST); NULL when it published none, and such a row is never "newest" |
| `url` | text — the filing attachment **verbatim, query string and all** (it is the uniqueness key); for a calendar row, the symbol's event-calendar page on the exchange |
| `source` | `NSE_ANNOUNCEMENT` / `NSE_EVENT_CALENDAR` |
| `earnings_date` | date, nullable — set on an `NSE_EVENT_CALENDAR` row: the nearest result meeting on or after the run; mirrored onto `sw_watch.earnings_date` |

Read by `GET /swing/setups` and `GET /swing/watch` (`catalyst_feed`: the newest announcement's
headline / stamp / url plus the earnings date) and by the desk page's triggers and plan lines,
all of which render a link (`target=_blank rel=noopener`) and an earnings badge.
