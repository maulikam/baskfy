# 04 — Business rules: the numerical contract

Every number here is a field of `baskfy_core.swing.config` (`SwingConfig` groups them as
`liquidity`, `flag`, `ep`, `parabolic`, `sizing`, `stops`, `watch`, `opening_range`, `market`; the
pack's default in brackets),
and every rule is a function in `baskfy_core.swing`. The tests in
`packages/core/tests/test_swing_*.py` assert **this document**; when a module finds the
document and the code disagreeing, the document wins and the code is fixed, unless the
document is wrong — in which case the change is a DECISIONS-SW entry that edits both.

Units: `*_pct` are percent (4.0 = 4.0%); `*_bars` are trading days; money is ₹ and `Decimal`.
Bars are the **adjusted** series (`ohlcv_daily.open/high/low/close = raw × adj_factor`).

**Amended at SW9.5** from his own words (`07-primary-source-corrections.md`, which quotes them):
the ADR floor (§1), the session cap and the trader's position cap in the plan (§5, §9.1), the
widest stop (§6), the index rule, the ladder's top rung and the drawdown containment (§8.2–8.5),
and the swing GTT's cushion (§9.4). Where a number changed, the old one is named in the section.

**Amended at SW10.5** from Maulik's review (`STANDING-ANSWERS.md` A7, A8, A9, A10, A14): the
first-live risk multiplier (§5.4), the marketable limit and the fill poll (§7.5), the live
gap's provisional score (§7.3), the `PENDING_RANGE` line (§9.1), the live buy's fill sequence
(§9.4), the watch funnel and the MANUAL expiry (§9.5), and the ladder's book (§10).

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

**Liquid** (`liquid_expr`): `adr_pct ≥ adr_min_pct` [4.0] **and** `turnover_avg ≥
turnover_min_inr` [₹5 cr] **and** `close ≥ price_min` [₹20]. Evaluated on the as-of bar. An
illiquid name never reaches a detector. The ADR floor was 3.5 until SW9.5; his screens use
5%+ and 3.5–4% is the floor he names on stream (`07`), so the default is 4.0 and
`sw_config.adr_min_pct` lets the trader raise it.

## §2 Setup 1 — FLAG (`detect_flags`, `FlagConfig`)

Window: the last `lookback_bars + base_max_bars` [65 + 60 = 125] bars ending at `as_of`; the
instrument must have a bar **on** `as_of` and at least one bar before it (a name listed today has
no pole and is not a flag — it is skipped, never an error).

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
  20 × clamp(adr_pct / 8) + 15 × clamp(1 − base_depth_pct / 30) + 10 × clamp(1 − dryup_ratio)`.
  (Full marks at twice each threshold — the ADR term's 8 is twice `adr_min_pct` [4.0], and
  follows it; `clamp` is to [0, 1].) SW3 adds `+5` for
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
**Score** (0–100; written down at SW12, it was the engine's from SW1): `40 × clamp(ret_5 / 100)
+ 30 × clamp(dist_ma_fast_pct / (8 × adr_pct)) + 30 × clamp(up_streak / 6)` — each denominator
twice its threshold, so an `EXHAUSTION` row earns nothing for the streak it no longer has.

## §5 Sizing (`sizing.size_position`, `SizingConfig`)

Inputs: sleeve `equity`, `cash_available`, `entry`, `stop`, `avg_turnover_inr`, and the widest
stop tolerated for this name — `stops.widest_stop_pct(adr_pct)` (§6), never more than
`max_stop_distance_pct` [`StopConfig`: 10].

5.1 Refusals, in order: `NO_EQUITY` (equity ≤ 0); `STOP_NOT_BELOW_ENTRY`; `STOP_TOO_WIDE`
(`(entry − stop) / entry × 100 > widest_stop_pct(adr_pct)` — the size is **not** shrunk to fit
a bad stop; his words: "stop should not be wider than the ATR or ADR of the stock");
`BELOW_MIN_TRADE_VALUE` (`entry × qty < min_trade_value_inr` [₹10,000]).

5.2 `qty = min(by_risk, by_position, by_cash, by_turnover)` with
* `by_risk = ⌊equity × risk_per_trade_pct [0.5] / 100 / (entry − stop)⌋`
* `by_position = ⌊equity × max_position_pct [20] / 100 / entry⌋`
* `by_cash = ⌊cash_available / entry⌋`
* `by_turnover = ⌊avg_turnover_inr × max_position_vs_turnover [0.01] / entry⌋` (when known)

and `cap` names which one bound. `risk_inr = (entry − stop) × qty`; `position_pct = entry × qty
/ equity × 100`. Worked example (his own): stop 4% below, risk 0.5% → position 12.5% of equity.
`r_multiple(entry, stop, exit) = (exit − entry) / (entry − stop)`, 2 dp.

5.3 **Counts** (SW9.5, his words in `07`). `max_open_positions` [10] — "typically 5-10
positions; 15-20 in a good market; all cash in a bad one" — is the trader's own cap, a
`sw_config` setting bounded by `BASKFY_SWING_MAX_OPEN_POSITIONS_MAX` [20]; the plan takes
`min(tier.max_open_positions, sizing.max_open_positions)` (§9.1). `max_new_entries_per_session`
[3] — "1, 2, 3 stocks per day… there's really no need to trade more than that" — caps the
`BUY_ON_TRIGGER` lines in one plan; it counts lines in **this** plan, not positions held —
**plus the entries the session has already taken** (SW10.4, STANDING-ANSWERS A5):
`build_entries(..., entries_already_today=n)` refuses `SESSION_CAP` once `n + lines ≥ 3`, where
`n` is the number of `BUY_ON_TRIGGER` lines of the day already `CONFIRMED`, `SENT` or `FILLED`,
whatever plan they came from. The evening and the morning plans pass nothing (a plan is the
session's first set of lines); the monitor's SIGNAL plan and the desk's confirm pass today's
count, so a fourth trigger of a morning is a skip and a fourth confirm is a refusal.
`max_position_pct` [20] stays; its ceiling `BASKFY_SWING_MAX_POSITION_PCT_MAX` is **30**
("never more than 30% of your account over night in any stock"). `risk_per_trade_pct` [0.5],
ceiling 1.0 (PACK.9).

5.4 **The first live sessions** (SW10.5, STANDING-ANSWERS A9; `02` §3.5 — "start small"). For
the first `first_live_sessions` [5] LIVE sessions the plan is sized at **half risk at plan
time**: `plan.first_live_multiplier(sessions_left, execution_enabled)` is
`risk_multiplier_first_live` [0.5] while `sw_config.first_live_sessions_left > 0` **and**
execution is enabled, else 1, and `build_entries(..., risk_multiplier)` scales
`risk_per_trade_pct` by it *before* `size_position` (`plan.sizing_at`) — so the line shown is
the line sent and every cap and refusal sees the real size. A paper plan (`DRY_RUN`, or the
flag off) is full size: the paper record rehearses the rules at the size the rules describe.
Never a quantity halved at send time (SW7.2's rule, void). `SELL_AT_OPEN` / `RAISE_GTT_STOP`
lines are never touched. The countdown is the evening job's: `first_live_sessions_left` comes
down by one when a LIVE `sw_session` closes, once (`sw_session.first_live_counted`), never by a
request, and a restart changes nothing — the fifth live session decrements to 0 and the sixth
plans at full risk. The plan header says "first live sessions: N left · risk 0.250%";
`sw_position.half_risk` tags the entries the journal shows.

## §6 Stops and management (`stops.py`, `StopConfig`)

6.1 **Initial stop** = low of the day (`stop_mode = LOW_OF_DAY`, the default); with
`stop_mode = OPENING_RANGE_LOW` and a known range, `max(range_low, low_of_day)` (the tighter).
A stop ≥ entry is an error, not a position.
**Widest stop** (SW9.5): `widest_stop_pct(adr_pct) = min(adr_pct × max_stop_adr_multiple [1.0],
max_stop_distance_pct [10])`, 2 dp — one ADR, and never more than the absolute cap. A stop
further below the entry than that is **skipped** (`SIZE_REFUSED / STOP_TOO_WIDE`), never sized
down: "stop should not be wider than the ATR or ADR of the stock". A name whose ADR is unknown
(0) admits no stop. Until SW9.5 the only limit was the 10% cap.
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
7.2 `evaluate_trigger`, precedence: after `monitor_close` [**15:30**, SW26 — 10:45 until
9 Sep 2026] → `SESSION_OVER`; range
incomplete → `RANGE_INCOMPLETE`; `last_price ≥ upper_circuit` → `LOCKED_UPPER_CIRCUIT`;
`last_price ≤ range_high × (1 + break_buffer_pct [0.1] / 100)` → `WAITING`; FLAG with
`last_price ≤ pivot_high` → `BELOW_PIVOT`; else `TRIGGERED` with `entry = last_price`,
`stop = min(range_low, low_of_day)`.
7.3 `live_gap` (EP at the open): `gap = (last / prev_close − 1) × 100 ≥ live_min_gap_pct` [10]
and `volume_pace = volume_so_far / (avg_daily_volume × minutes_elapsed / 375) ≥
live_min_volume_pace` [3.0]. Its **provisional score** (SW10.5, A14 — the watch row is ranked
by it until the detectors score the close): `live_gap_score = 35 × clamp(gap / 20) + 35 ×
clamp(volume_pace / 6)`, the two terms of §3's score the pre-open knows, out of 70. The watch
row also carries the ADR the bars measured (`sw_watch.adr_pct`), because a live gap has no
detection row and a stop must be measured against one ADR (§6.1).
7.4 The monitor reads the watchlist's ticks from the `TickBus` and builds the opening range
**from the ticks inside the window** at window close (SW11, STANDING-ANSWERS A4); the minute
candles from `historical_data(interval="minute")` are fetched once, `range_reconcile_delay_minutes`
[1] after the window closed, only to reconcile the tick range (a reconciled range replaces the
tick range for the verdicts that follow; a signal already raised stands). Fallback for a name
the ticker has gone quiet on: Kite `quote` for ≤ 500 instruments per call, at most one call
every `quote_poll_min_seconds` [5], inside the limiter — anything faster is a bug (B10). Every
`TRIGGERED` verdict is one `sw_signal` row and one desk notification (email, one-way, for the
daily focus; A2); **nothing is ordered** on this path (auto-execute is a separate, flagged
step — SW25). The same process is also the desk's clock, and since SW26 it runs the session's
two chores **from inside the watch**, each once, at its own hour rather than after the watch
ends: at `pending_cutoff_at` [**10:45**] the cutoff cancels every open remainder and frees every
unclaimed slot (A7, A8), and at `gtt_sweep_at` [15:15] the sweep re-arms any filled quantity
without a GTT (A8; what is still naked afterwards is `SWING_GTT_MISSING_AT_1515`). `run_after_close`
still runs both once more when the watch ends, as a backstop for the empty-watchlist and
died-early paths; both chores are idempotent and keyed on the day.

**Why `pending_cutoff_at` is a setting of its own.** `monitor_close` used to carry both jobs, so
SW26's widening would have moved the cutoff to 15:30 — *after* the 15:15 sweep. It also belongs
at 10:45 on its own merits: a gap whose opening range has not resolved by then will not, and its
slot would starve the watchlist all afternoon. The invariant, and it is what the test pins:
`pending_cutoff_at < gtt_sweep_at ≤ monitor_close`.
7.5 **The entry cap, the market protection and the fill poll** (SW10.5 / STANDING-ANSWERS A8;
order type amended by Maulik 4 Sep 2026, DECISIONS-SW **SW22**). The **cap** is unchanged:
`plan.marketable_limit = min(trigger × (1 + entry_limit_buffer_pct [0.5] / 100), range_high +
entry_limit_max_adr [0.25] × ADR)` snapped down to the tick (ADR in rupees, `adr_pct / 100 ×
trigger`; a line with no range reads the trigger as the range high). It is the most this setup
is worth paying.

What changed is how the cap reaches the exchange. A confirmed buy is sent as **MARKET** with
Kite's `market_protection` set to `plan.market_protection_pct = (cap / last_price − 1) × 100`,
clamped into `[market_protection_floor_pct [0.05], market_protection_max_pct [3.0]]` — the same
ceiling, at the exchange, on an order that crosses the spread. A resting LIMIT at the cap did not
fill when the tape ran past it, which is how a breakout gets missed. `entry_order_type` selects
between the two and `"LIMIT"` restores A8 exactly.

The **live price is read per confirm** (the desk's own Kite read, M85's interactive lane), not
taken from the plan, which `_validate` lets be half an hour old. A live buy without one is
`BLOCKED`, never guessed: no price means no protection percentage and no value for the risk
check. When `last_price` is already **at or above the cap** the confirm is `BLOCKED` — the
breakout has run past what the setup justifies, and the stop is a technical level that does not
move up with a chased entry, so a share bought there carries more risk than §5 sized for. A8
expressed that same refusal as a LIMIT nobody filled.

The request then polls the order for at most `fill_poll_seconds` [10] at one read every
`fill_poll_interval_seconds` [0.5] — Kite's orders endpoint at ≤ 2 req/s — and stops early on
COMPLETE or a dead status. §9.4 says what each answer writes. The GTT is unchanged
(non-negotiable 4): armed in the same call, for exactly the quantity filled, at the plan's stop.

## §8 Market gate and progressive exposure (`market.py`, `MarketConfig`)

8.1 **Breadth** over the liquid universe at the close: `pct_up_strong_1m` = share with
`ret_20 ≥ strong_move_pct` [25]; `pct_new_52w_high` = share with `close ≥ high_1y`;
`pct_above_ma_slow` = share with `close > ma_slow`.
8.2 **Index reading:** **NIFTY MidSmallcap 400**'s `index_ma_fast` [10]- and `index_ma_slow`
[20]-bar SMAs (fallback NIFTY 500; none → the index is ignored).

> ⚠ **This said NIFTY 500 until 9 Sep 2026 and that was stale, not the rule.** SW17 (`67df9b4`,
> 3 Sep) changed the benchmark on Maulik's call — *"the book trades mid- and small-caps, so the
> tape it asks about is nifty-mid-small-400"* — and this line was never updated. M87 then read
> this line, decided the code was wrong, and reverted the setting; it took two days and a live
> RED gate to notice. **The decision is the authority; if the code and this paragraph disagree
> again, fix the paragraph.** A large-cap-weighted index answers a question about a market this
> book does not trade. `IndexReading.long_bias = ma_fast > ma_slow`;
`bearish = ma_fast < ma_slow`; equal averages are neither. The close itself is **not**
consulted (SW9.5): his filter for longs is the 10-day above the 20-day — a pullback under both
averages on a rising 10-day is still a long tape, a bounce over both under a falling one is
not. Until SW9.5 the rule read the close against both averages.
8.3 **Gate:** empty universe → RED; index `bearish` → RED; `pct_up_strong_1m ≤
red_max_pct_up` [2] → RED; `pct_up_strong_1m ≥ green_min_pct_up` [5] and (no index or index
`long_bias`) → GREEN; else AMBER. Breadth decides; the index can only make it worse.
8.4 **Ladder** `tiers` [(2, 25%), (4, 50%), (6, 75%), (10, 100%)] as (max open positions, max
sleeve exposure %); the top rung is his "typically 5-10 positions" (it was 8), and his 15–20
of a great market is the env ceiling a setting may climb to, not a rung. From rung `L`, the
last `lookback_trades` [5] closed trades and the gate:
RED → rung 0, `new_entries_allowed = false`; a loss streak ≥ `step_down_loss_streak` [3] → `L − 1`;
GREEN with ≥ 5 closed trades and net R > 0 → `L + 1`; AMBER holds; the ladder never skips a
rung. AMBER **entries are allowed at the current rung** (the size is what shrinks the book,
via the tier's exposure ceiling). The drawdown lock-out (8.5) outranks all of it.
8.5 **Drawdown containment** (SW9.5; "I try to contain them at 15-20%"). The sleeve's EOD NAV
against the highest EOD NAV it has reached (`03` §1: `sw_config.sleeve_peak_inr`, the peak
only rises; how the NAV is computed is SW9.5.1): `drawdown_pct = (peak − nav) / peak × 100`,
0 at or above the peak and 0 when the peak is not positive. `market.drawdown_locked(drawdown_pct,
was_locked)`: locked at `max_drawdown_pct` [15] or more; once locked, stays locked until the
drawdown is back inside `resume_drawdown_pct` [10] — hysteresis, so a sleeve oscillating around
15% does not flap. `exposure_tier(..., drawdown_pct, was_drawdown_locked)` applies it **first**:
a locked sleeve is rung 0 with `new_entries_allowed = false` and `ExposureTier.drawdown_locked =
true`, whatever the tape and the results say; released, it resumes at rung 0. A sleeve with no
peak yet (its first evening) is at its peak, not in drawdown. The state is `swing-eod`'s
(`03` §1, §3), never a form's. Exits are managed as always; only new entries stop.

## §9 The plan (`plan.py`)

9.1 `build_entries`: watch items sorted by `(−score, symbol)`; for each, in order: not in
`TRADEABLE_SETUPS` → `NOT_TRADEABLE_SETUP`; `tier.drawdown_locked` → `DRAWDOWN_LOCKOUT` (8.5);
gate RED or entries disallowed → `GATE_RED`; symbol held → `ALREADY_HELD`;
`locked_upper_circuit` → `LOCKED_UPPER_CIRCUIT`; `lined ≥ max_new_entries_per_session` [3] →
`SESSION_CAP` (§5.3 — lines in this plan, not positions held); `open + lined ≥
min(tier.max_open_positions, sizing.max_open_positions)` → `TIER_FULL` (the detail names the
number that bound: "rung L allows N positions"); size refused → `SIZE_REFUSED(detail)`
(a stop wider than `widest_stop_pct(adr_pct)` is `STOP_TOO_WIDE` here, §6.1); `open exposure +
value > equity × tier.max_exposure_pct / 100` → `EXPOSURE_FULL`; else a `BUY_ON_TRIGGER` line
with `trigger` and `stop` snapped to ₹0.05, `quantity`, `risk_inr`, `position_value`, `trail`
and a note naming the cap. Cash spent by earlier lines is not spent twice.
9.2 `exit_lines` maps `manage` actions to `SELL_AT_OPEN` (partial or all) and `RAISE_GTT_STOP`.
9.3 `assemble`: exits first, then entries; totals over entries; `plan_hash` is the sha256 of the
canonical lines — the same plan hashes the same.
9.4 The desk gives a plan a `plan_id` and a 30-minute expiry; `POST /swing/execute` needs
`confirm=true`, an unexpired `plan_id` and a `line_id`; `client_id = plan_id:symbol:kind` so a
re-post cannot double-send. **A line's size is a preview; the confirm is the gate** (SW10.4,
STANDING-ANSWERS A5): under a row lock on the day's `sw_session` (`SELECT … FOR UPDATE`, held
from before the book is re-derived until the gateway has answered and the rows are written) the
desk re-reads the book — open positions at cost + every `BUY_ON_TRIGGER` line of the day
already `CONFIRMED`/`SENT` and not yet a position, at its trigger + cash — and re-sizes a
`BUY_ON_TRIGGER` through this section's own `build_entries` (the same `entries_now` the monitor
sizes a SIGNAL line with) against the rung's ceiling, `min(rung, max_open_positions)` and the
session cap counting today's entries. A line that fits goes as planned; one that only the
exposure ceiling refuses is **shrunk to the ceiling's headroom** (never grown past what the page
showed) and the row rewritten with `quantity`, `risk_inr`, `position_value` and a note; one the
rules cannot line at all is `BLOCKED` with the skip's code leading the reason
(`EXPOSURE_FULL` / `TIER_FULL` / `SESSION_CAP` / `SIZE_REFUSED` / `GATE_RED` /
`DRAWDOWN_LOCKOUT` / `ALREADY_HELD`) and the line marked `REJECTED` — never sent, never left
`CONFIRMED`. The market row read is the latest strictly before the session (the close the plan
was built on). The evening and the morning plans do not shrink a name to fit (§9.1's
`EXPOSURE_FULL` stands for a plan of many names); the re-size is the rule for one name at the
moment of its trigger and its confirm (DECISIONS-SW SW10.5). A `BUY_ON_TRIGGER` line is sent as a **LIMIT buy at `trigger` (or
market once a `TRIGGERED` signal exists for it)**, and its GTT stop is armed in the same call
with a `StopBand(min_pct=0.005, max_pct=0.10)` (PACK.3) — the desk's 8–12% band is the weekly
book's, not this one's. **The GTT's cushion** (SW9.5, PACK.8): a GTT fires a LIMIT order, and
he uses market stops ("I always use market stops, never limit stops"); the swing route passes
`limit_fraction = SWING_GTT_LIMIT_FRACTION` [0.97] to `place_gtt_stop`, so the resting limit
sits 3% under its trigger and fills on the way down like a market stop would. The weekly book
keeps the gateway's own `GTT_LIMIT_FRACTION` (0.995); the keyword is additive and defaults to
it. Every swing GTT — a buy's, a partial's re-arm, a raised stop's, a re-arm — carries it.

9.5 **The watchlist** (`WatchConfig`, SW5; the funnel since SW10.5, STANDING-ANSWERS A14 —
his own: universe → weekly focus list of 5–20 → daily focus under 5, `07`). Each evening the
**top `auto_watch_top_n` [20] `SETTING_UP` flags by score** at `auto_watch_min_score` [60] or
above are auto-watched, and **every** `GAP_DAY` EP whatever it scored, because an EP is
enterable for three sessions and there is no second chance to notice it; the row carries its
score and ADR (`sw_watch.score`, `adr_pct`). The monitor watches **all** `WATCHING` rows. The
**daily focus** — `sw_watch.focus`, recomputed by the evening and by the premarket once the
live gaps are on the list — is the top `focus_top_n` [5] flags by score plus every EP: what the
notifier (SW11) pushes and the desk page puts on top; the other rows are watched, signalled
and logged to `sw_signal`, never pushed, and feed the journal's "missed setups". A flag row
expires after `flag_valid_bars` [10] sessions without a trigger, an EP after `ep.valid_bars`
[3]; a `MANUAL` row keeps the levels the person typed and expires after `manual_valid_bars`
[10] sessions **unless re-confirmed** on the watchlist page (`PATCH /swing/watch/{id}` with
`reconfirm: true` restarts its clock from `reconfirmed_on`; until SW10.5 a MANUAL row never
expired) — a two-week-old typed pivot is stale, and MANUAL levels are not refreshed premarket.
Expiry is a state change (`WATCHING` → `EXPIRED`), never a delete — the record of what was
watched is the record of what was passed over.

## §10 The journal (`journal.py`)

`ClosedTrade.r_multiple = (exit_avg − entry) / (entry − initial_stop)`, 2 dp; `pnl_inr = (exit_avg
− entry) × quantity`; `exit_avg` share-weighted over fills. `summarize`: trades, win rate %,
mean win R, mean loss R, expectancy (mean R), profit factor (gross win R / gross loss R, null
when no losses), net R, largest win/loss, current loss streak. Empty → zeros, not an error.
Simulated fills are summarised **separately** from real ones on the page. **The ladder reads
real closes only, from day one** (SW10.5, STANDING-ANSWERS A10): there is no paper book for
it — PACK.6's paper clause is void, the rung starts at 0 — and a simulated close never moves
the rung whatever the flag says. One idempotent, date-bound settlement at 21:05 after `manage`
and the fill reconciliation; the 09:09 job settles the previous session **only as a catch-up**
when no settlement record exists for it, never a second time.

## §11 The backtest (SW9) — an EOD approximation, labelled as one

For each session 2017→: run the detectors at the close; for each `SETTING_UP` flag / `GAP_DAY`
EP that is liquid and not locked, enter **at the next session's open if it is ≥ trigger**, else
at `trigger` if the next session's high ≥ trigger (fill at trigger); stop = the prior day's low;
apply `manage` daily with fills at the next open; size by §5 on a constant ₹10 lakh sleeve with
the ladder in force; costs 0.13% per side. Report the §10 statistics, by setup and by year, and
the equity curve. State on the page: no intraday data (so no ORH filter — real entries are
more selective), no circuit history before 2020, survivorship handled by `instrument.delisted_on`,
and that where `upper_circuit` is absent no lock is assumed, so a name that was locked may have
been entered here.

**Amended at SW9.6** (STANDING-ANSWERS A12, B1–B5; his words in `07`: the index filter, the
drawdown containment). The frame is a **constant ₹10 lakh sleeve and the index rule**:

* **The index.** The runner hands the engine NIFTY 500's close series from `index_snapshot_daily`
  (NIFTY 50 when NIFTY 500 has fewer than `index_ma_slow` rows in the window; neither → no
  index, and the caveats say so); `params.index_slug` names which. The `index_ma_fast` / `index_ma_slow`
  averages of §8.2 are computed in-frame from the closes on or before the session — **including**
  the session's own close, never a later one — and `market_gate` reads them as the desk does.
* **The drawdown lock-out** (§8.5) runs on the sleeve's own equity curve — cash plus open
  positions marked at the close — measured peak-to-trough as a percentage of the **constant
  sleeve** (the live rule measures the compounding sleeve against its peak; this sleeve never
  compounds, so 15% is thirty trades' risk whatever the curve has made), with the same
  `max_drawdown_pct` / `resume_drawdown_pct` hysteresis through `drawdown_locked`. The result
  carries the deepest drawdown, its dates, and how many sessions the lock-out held.
* **Gate-on against gate-off.** Every run keeps three books over one detection pass — `gate_off`
  (`market_gate` replaced by GREEN every session; the ladder and the lock-out still apply),
  `breadth_only` (`market_gate(breadth, None)`) and `full` (`market_gate(breadth, index)`) — and
  reports entries, net R, expectancy, win rate and the curve's max drawdown for each, overall,
  by the year **entered** (the gate decides entries) and by setup. **Breadth's contribution** is
  `breadth_only − gate_off`; **the index rule's** is `full − breadth_only`. The primary book is
  `full` when an index was supplied and `breadth_only` otherwise.
* **Delisted names** (B2): a held name whose `instrument.delisted_on` the runner knows is sold at
  its last close on its last bar and counted `DELISTED`; a name that merely stops printing with
  no delisting known is sold after the screener engine's five-session tolerance as `NO_BAR`.
* Costs are read from `params.cost_pct_per_side`; circuits from `upper_circuit` where present, no
  lock assumed where absent; only sessions the calendar names trade; the partial sells at the
  next open after the day-3–5 signal and the trail exit at the next open after the close below
  the MA, both decided by `stops.manage` — the backtest adds no exit rule of its own.
