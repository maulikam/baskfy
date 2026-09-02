# 04 — Business rules: the numerical contract

Every number here is a field of `baskfy_core.swing.config` (`SwingConfig` groups them as
`liquidity`, `flag`, `ep`, `parabolic`, `sizing`, `stops`, `watch`, `opening_range`, `market`; the
pack's default in brackets),
and every rule is a function in `baskfy_core.swing`. The tests in
`packages/core/tests/test_swing_*.py` assert **this document**; when a module finds the
document and the code disagreeing, the document wins and the code is fixed, unless the
document is wrong — in which case the change is a DECISIONS-SW entry that edits both.

Units: `*_pct` are percent (3.5 = 3.5%); `*_bars` are trading days; money is ₹ and `Decimal`.
Bars are the **adjusted** series (`ohlcv_daily.open/high/low/close = raw × adj_factor`).

## §1 Universe and indicators (`indicators.py`, `LiquidityConfig`)

Computed per bar, per instrument, over the instrument (`with_swing_indicators`):

| Column | Definition |
|---|---|
| `range_pct` | `(high / low − 1) × 100` |
| `adr_pct` | mean of `range_pct` over the last `adr_bars` [20] **including today** |
| `turnover_inr` | `ohlcv_daily.turnover` when present and > 0, else `close × volume` |
| `turnover_avg` | mean of `turnover_inr` over `turnover_bars` [20] |
| `ma_fast`, `ma_slow`, `ma_trend` | SMA of `close` over 10 / 20 / 50 bars |
| `vol_avg_fast` | mean volume over `flag.dryup_bars` [10] |
| `vol_avg_rvol` | mean volume over `ep.rvol_bars` [50] **excluding today** |
| `rvol` | `volume / vol_avg_rvol` (null when the average is 0) |
| `gap_pct` | `(open / prev_close − 1) × 100` |
| `ret_5/10/20/60` | `(close / close[n bars ago] − 1) × 100` |
| `up_streak` | consecutive bars with `close > prev_close`, today included; a down bar resets to 0 |
| `close_position` | `(close − low) / (high − low)`, 0.5 when `high == low` |

**Liquid** (`liquid_expr`): `adr_pct ≥ adr_min_pct` [3.5] **and** `turnover_avg ≥
turnover_min_inr` [₹5 cr] **and** `close ≥ price_min` [₹20]. Evaluated on the as-of bar. An
illiquid name never reaches a detector.

## §2 Setup 1 — FLAG (`detect_flags`, `FlagConfig`)

Window: the last `lookback_bars + base_max_bars` [65 + 60 = 125] bars ending at `as_of`; the
instrument must have a bar **on** `as_of`.

2.1 **Pole.** `pole_idx` = index of the highest `high` among the bars **before today**;
`pole_high` = that high. `pole_low` = lowest `low` in `[max(pole_idx − lookback_bars, 0),
pole_idx)`. `prior_move_pct = (pole_high / pole_low − 1) × 100 ≥ flagpole_min_gain_pct` [30].

2.2 **Base.** `base_bars = (bars in window − 1) − pole_idx` (the bars after the pole, today
included); `base_min_bars` [10] ≤ `base_bars` ≤ `base_max_bars` [60].
`base_depth_pct = (1 − base_low / pole_high) × 100 ≤ base_max_depth_pct` [30].

2.3 **Higher lows** (`require_higher_lows` [true]): lowest low of the second half of the base
≥ lowest low of the first half (`half = base_bars // 2`).

2.4 **Tightness.** `tightness_adr = ((max high − min low over the last tight_bars [10]) /
min low × 100) / adr_pct ≤ tight_max_adr_multiple` [3.0].

2.5 **MA structure.** `dist_ma_fast_pct = (close / ma_fast − 1) × 100 ≤ max_extension_adr [2.0]
× adr_pct` (not extended); `dist_ma_slow_pct ≥ −ma_tolerance_pct` [−2] (on or above the
20-day); `close ≥ ma_trend`; `ma_slow ≥ ma_slow[ma_rising_bars [5] bars ago]` (rising).
**Dry-up:** `dryup_ratio = vol_avg_fast / mean volume over the base ≤ dryup_max_ratio` [0.85].

2.6 **Status and levels.** `pivot_high` = highest high of the last `pivot_bars` [20] bars,
today included; `pivot_prev` = the same over the bars before today.
* `BREAKOUT_TODAY` when `close > pivot_prev` **and** `rvol ≥ breakout_min_rvol` [1.5] and 2.1–2.2
  hold (2.3–2.5 are waived on the breakout day itself). `trigger` = today's high.
* `SETTING_UP` when 2.1–2.5 all hold and it is not a breakout. `trigger` = `pivot_high`.
* `stop_ref` = today's low in both cases.
* **Score** (0–100): `30 × clamp(1 − tightness_adr / 3.0) + 25 × clamp(prior_move_pct / 60) +
  20 × clamp(adr_pct / 7) + 15 × clamp(1 − base_depth_pct / 30) + 10 × clamp(1 − dryup_ratio)`.
  (Full marks at twice each threshold; `clamp` is to [0, 1].) SW3 adds `+5` for
  `listed_within_2y` and `+5` for a sector in the top-3 breadth strip, capped at 100.

## §3 Setup 2 — EP (`detect_eps`, `EpConfig`)

Window: `prior_bars + 1` [61] bars ending at `as_of`, all present.

3.1 `gap_pct ≥ min_gap_pct` [10]. 3.2 `rvol ≥ min_rvol` [3.0]. 3.3 `close ≥ open` and
`close_position ≥ min_close_position` [0.5] (the gap held). 3.4 **Neglect:**
`prior_move_pct = (prev_close / close[prior_bars ago] − 1) × 100 ≤ max_prior_gain_pct` [30].
3.5 **Circuit:** `locked_upper_circuit = upper_circuit > 0 and high ≥ upper_circuit` — the row
is kept and flagged, never dropped; the plan skips it with `LOCKED_UPPER_CIRCUIT`.

Status `GAP_DAY`; `trigger` = today's high; `stop_ref` = today's low; the watch row expires
after `valid_bars` [3] sessions. **Score:** `35 × clamp(gap_pct / 20) + 35 × clamp(rvol / 6) +
15 × close_position + 15 × clamp(1 − prior_move_pct / 30)`.

## §4 Setup 3 — PARABOLIC_SHORT (`detect_parabolic`, `ParabolicConfig`) — detect only

4.1 `ret_5 ≥ min_gain_5_bars_pct` [50] **or** `ret_10 ≥ min_gain_10_bars_pct` [100].
4.2 `dist_ma_fast_pct ≥ min_extension_adr [4.0] × adr_pct`.
4.3 `RUNNING` when `up_streak ≥ min_up_streak` [3]; `EXHAUSTION` when `up_streak == 0` and
yesterday's streak ≥ `min_up_streak` (the first red close after the run).
`trigger` = today's low (the level a short would key off), `stop_ref` = today's high — recorded
for the journal of what the method *would* have done. `TRADEABLE_SETUPS` excludes it; no
plan line may carry it (SW10 asserts).

## §5 Sizing (`sizing.size_position`, `SizingConfig`)

Inputs: sleeve `equity`, `cash_available`, `entry`, `stop`, `avg_turnover_inr`, and the widest
stop tolerated `max_stop_distance_pct` [`StopConfig`: 10].

5.1 Refusals, in order: `NO_EQUITY` (equity ≤ 0); `STOP_NOT_BELOW_ENTRY`; `STOP_TOO_WIDE`
(`(entry − stop) / entry × 100 > 10` — the size is **not** shrunk to fit a bad stop);
`BELOW_MIN_TRADE_VALUE` (`entry × qty < min_trade_value_inr` [₹10,000]).

5.2 `qty = min(by_risk, by_position, by_cash, by_turnover)` with
* `by_risk = ⌊equity × risk_per_trade_pct [0.5] / 100 / (entry − stop)⌋`
* `by_position = ⌊equity × max_position_pct [20] / 100 / entry⌋`
* `by_cash = ⌊cash_available / entry⌋`
* `by_turnover = ⌊avg_turnover_inr × max_position_vs_turnover [0.01] / entry⌋` (when known)

and `cap` names which one bound. `risk_inr = (entry − stop) × qty`; `position_pct = entry × qty
/ equity × 100`. Worked example (his own): stop 4% below, risk 0.5% → position 12.5% of equity.
`r_multiple(entry, stop, exit) = (exit − entry) / (entry − stop)`, 2 dp.

## §6 Stops and management (`stops.py`, `StopConfig`)

6.1 **Initial stop** = low of the day (`stop_mode = LOW_OF_DAY`, the default); with
`stop_mode = OPENING_RANGE_LOW` and a known range, `max(range_low, low_of_day)` (the tighter).
A stop ≥ entry is an error, not a position.
6.2 **Trail** = `MA10` when `adr_pct ≥ fast_trail_min_adr_pct` [6] else `MA20`.
6.3 **Partial** = `qty × partial_numerator / partial_denominator` [1/3], integer division,
never the whole position, never 0 unless `qty < 3`.
6.4 **`manage(position, bar)`**, evaluated after each close, precedence top-down:
1. `bar.low ≤ stop` → `STOPPED_OUT / HARD_STOP_HIT` (the GTT fired or should have).
2. EP on its gap day (`bars_since_entry == 0`) with `close < open` → `SELL_ALL /
   EP_FAILED_RED_ON_DAY`.
3. `bars_since_entry > 0` and `close < trail MA` → `SELL_ALL / CLOSE_BELOW_TRAIL_MA`.
4. Not `partial_done`, `partial_earliest_bar [3] ≤ bars_since_entry ≤ partial_latest_bar [5]`,
   `close > entry` → `SELL_PARTIAL / PARTIAL_INTO_STRENGTH` then, if the stop is below entry,
   `RAISE_STOP / BREAKEVEN_AFTER_PARTIAL` to the entry.
5. Stop below entry and `(close − entry) / (entry − initial_stop) ≥ breakeven_after_r` [1.0] →
   `RAISE_STOP / BREAKEVEN_AT_R`.
6. Otherwise `HOLD / NOTHING_TO_DO` — said explicitly.
6.5 **A stop never falls:** `apply` takes `max(stop, new_stop)`; the desk refuses a
`RAISE_GTT_STOP` below the resting trigger. **Never averaged down:** `ALREADY_HELD` skips.
Sell lines execute **at the next open** (`SELL_AT_OPEN`); a stop-out is the GTT's.

## §7 The opening range and the live trigger (`opening_range.py`, `OpeningRangeConfig`)

7.1 `session_open` [09:15 IST]; windows `windows_minutes` [(1, 5, 60)], `default_window_minutes` [5]; the range is the
high/low of candles with `open ≤ start < open + window`; `complete` only once a candle at or
after the window's end exists (no clock is consulted).
7.2 `evaluate_trigger`, precedence: after `monitor_close` [10:45] → `SESSION_OVER`; range
incomplete → `RANGE_INCOMPLETE`; `last_price ≥ upper_circuit` → `LOCKED_UPPER_CIRCUIT`;
`last_price ≤ range_high × (1 + break_buffer_pct [0.1] / 100)` → `WAITING`; FLAG with
`last_price ≤ pivot_high` → `BELOW_PIVOT`; else `TRIGGERED` with `entry = last_price`,
`stop = min(range_low, low_of_day)`.
7.3 `live_gap` (EP at the open): `gap = (last / prev_close − 1) × 100 ≥ live_min_gap_pct` [10]
and `volume_pace = volume_so_far / (avg_daily_volume × minutes_elapsed / 375) ≥
live_min_volume_pace` [3.0].
7.4 The monitor polls the watchlist's quotes every 5 s from the `TickBus` (fallback: Kite
`quote` for ≤ 500 instruments per call, ≤ 1 call per 5 s, inside the 3 req/s limiter); minute
candles for the range come from `historical_data(interval="minute")` at window close. Every
`TRIGGERED` verdict is one `sw_signal` row and one desk notification; **nothing is ordered**.

## §8 Market gate and progressive exposure (`market.py`, `MarketConfig`)

8.1 **Breadth** over the liquid universe at the close: `pct_up_strong_1m` = share with
`ret_20 ≥ strong_move_pct` [25]; `pct_new_52w_high` = share with `close ≥ high_1y`;
`pct_above_ma_slow` = share with `close > ma_slow`.
8.2 **Index reading:** NIFTY 500 close vs its `index_ma_fast` [10]- and `index_ma_slow` [20]-bar SMAs
(fallback NIFTY 50; none → the index is ignored).
8.3 **Gate:** empty universe → RED; index below both MAs → RED; `pct_up_strong_1m ≤
red_max_pct_up` [2] → RED; `pct_up_strong_1m ≥ green_min_pct_up` [5] and (no index or index
above both) → GREEN; else AMBER. Breadth decides; the index can only make it worse.
8.4 **Ladder** `tiers` [(2, 25%), (4, 50%), (6, 75%), (8, 100%)] as (max open positions, max
sleeve exposure %). From rung `L`, the last `lookback_trades` [5] closed trades and the gate:
RED → rung 0, `new_entries_allowed = false`; a loss streak ≥ `step_down_loss_streak` [3] → `L − 1`;
GREEN with ≥ 5 closed trades and net R > 0 → `L + 1`; AMBER holds; the ladder never skips a
rung. AMBER **entries are allowed at the current rung** (the size is what shrinks the book,
via the tier's exposure ceiling).

## §9 The plan (`plan.py`)

9.1 `build_entries`: watch items sorted by `(−score, symbol)`; for each, in order: not in
`TRADEABLE_SETUPS` → `NOT_TRADEABLE_SETUP`; gate RED or entries disallowed → `GATE_RED`; symbol
held → `ALREADY_HELD`; `locked_upper_circuit` → `LOCKED_UPPER_CIRCUIT`; `open + lined ≥
tier.max_open_positions` → `TIER_FULL`; size refused → `SIZE_REFUSED(detail)`; `open exposure +
value > equity × tier.max_exposure_pct / 100` → `EXPOSURE_FULL`; else a `BUY_ON_TRIGGER` line
with `trigger` and `stop` snapped to ₹0.05, `quantity`, `risk_inr`, `position_value`, `trail`
and a note naming the cap. Cash spent by earlier lines is not spent twice.
9.2 `exit_lines` maps `manage` actions to `SELL_AT_OPEN` (partial or all) and `RAISE_GTT_STOP`.
9.3 `assemble`: exits first, then entries; totals over entries; `plan_hash` is the sha256 of the
canonical lines — the same plan hashes the same.
9.4 The desk gives a plan a `plan_id` and a 30-minute expiry; `POST /swing/execute` needs
`confirm=true`, an unexpired `plan_id` and a `line_id`; `client_id = plan_id:symbol:kind` so a
re-post cannot double-send. A `BUY_ON_TRIGGER` line is sent as a **LIMIT buy at `trigger` (or
market once a `TRIGGERED` signal exists for it)**, and its GTT stop is armed in the same call
with a `StopBand(min_pct=0.005, max_pct=0.10)` (PACK.3) — the desk's 8–12% band is the weekly
book's, not this one's.

9.5 **The watchlist** (`WatchConfig`, SW5). A detected flag is auto-watched at
`auto_watch_min_score` [60] or above with status `SETTING_UP`; **every** `GAP_DAY` EP is watched
whatever it scored, because an EP is enterable for three sessions and there is no second chance to
notice it. A flag row expires after `flag_valid_bars` [10] sessions without a trigger, an EP after
`ep.valid_bars` [3]; a `MANUAL` row never expires and keeps the levels the person typed. Expiry is
a state change (`WATCHING` → `EXPIRED`), never a delete — the record of what was watched is the
record of what was passed over.

## §10 The journal (`journal.py`)

`ClosedTrade.r_multiple = (exit_avg − entry) / (entry − initial_stop)`, 2 dp; `pnl_inr = (exit_avg
− entry) × quantity`; `exit_avg` share-weighted over fills. `summarize`: trades, win rate %,
mean win R, mean loss R, expectancy (mean R), profit factor (gross win R / gross loss R, null
when no losses), net R, largest win/loss, current loss streak. Empty → zeros, not an error.
Simulated fills are summarised **separately** from real ones on the page; the ladder reads
real trades when execution is enabled and simulated ones before (PACK.6).

## §11 The backtest (SW9) — an EOD approximation, labelled as one

For each session 2017→: run the detectors at the close; for each `SETTING_UP` flag / `GAP_DAY`
EP that is liquid and not locked, enter **at the next session's open if it is ≥ trigger**, else
at `trigger` if the next session's high ≥ trigger (fill at trigger); stop = the prior day's low;
apply `manage` daily with fills at the next open; size by §5 on a constant ₹10 lakh sleeve with
the ladder in force; costs 0.13% per side. Report the §10 statistics, by setup and by year, and
the equity curve. State on the page: no intraday data (so no ORH filter — real entries are
more selective), no circuit history before 2020, survivorship handled by `instrument.delisted_on`.
