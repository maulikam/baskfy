# 04 — Business rules: the numerical contract

**Every number here is a field of `baskfy_core.vbt.config`** (`VbtConfig` groups them as `data`,
`scan`, `trend`, `breadth`, `entry`, `sizing`, `exits`, `costs`; the default in brackets), and
every rule is a function in `baskfy_core.vbt`. The tests in
`packages/core/tests/test_vbt_*.py` **assert this document**, never current behaviour. Nothing
downstream compares against a literal: a detector, a task, a router and a page all read a field,
so a recalibration is one edit and one diff.

When a module finds this document and the code disagreeing: **the decision wins, not the doc**
(CLAUDE.md, 9 Sep 2026). `git log -S "<the value>" -- <the file>` first; a commit message naming
Maulik and giving a reason is a later fact than this file. If it is a decision, fix this file and
say so; if there is no such commit, it may be a bug — then say so and change the code. **Never
pin a value to this document; pin it to the decision and cite the commit.**

Units, stated once: every `*_pct` is a **percent** (`6.5` means 6.5%), never a fraction; every
`*_bars`/`*_sessions` counts **trading sessions on the run's own calendar** (§2); money is ₹ and
`Decimal`, never `float` (house rule 9). Bars are the **adjusted** series
(`ohlcv_daily.open/high/low/close = raw × adj_factor`) except where a rule names `close_raw`.

---

## §1 Universe and indicators (`indicators.py`, `DataConfig`)

**The universe** (`universe_expr`): `instrument.instrument_type = 'EQ'` **and**
`series ∈ {EQ, BE, BZ}` (a null series is a delisted name with no current listing row and is
**kept**) **and not an ETF**. ETF membership is the `etf` index universe (350 members), plus two
deliberately narrow regexes for a name that never made the list: `\bETF\b` on the instrument
name and `(BEES|ETF|IETF)$` on the symbol. `etf_name_pattern` / `etf_symbol_pattern` are
`DataConfig` fields.

> **BE and BZ stay in.** Every trade here is delivery, and dropping today's BE list would drop
> the history of names that were *later* demoted — a survivorship bias in reverse (STRATEGY §1).
> SME (`SM`/`ST`/`SZ`) is out: lot sizes and a different microstructure.

Computed per bar, per instrument, over the instrument (`with_vbt_indicators`):

| Column | Definition |
|---|---|
| `prev_close` | the previous **bar's** close for that instrument (not the previous calendar day) |
| `change_pct` | `(close / prev_close − 1) × 100` |
| `vol_sma_50` | mean `volume` over `scan.vol_sma_bars` [50] **including today** |
| `rvol` | `volume / vol_sma_50`, null when the average is 0 |
| `sma_200` | mean `close` over `trend.dma_bars` [200] **including today** |
| `high_20_prior` | the highest `high` of the `trend.breakout_high_bars` [20] sessions **ending yesterday** — today is excluded, or B could never be true |
| `ret_20_pct` | `(close / close[trend.ret_bars [20] sessions ago] − 1) × 100` |
| `close_position` | `(close − low) / (high − low)`; **0.5 when `high == low`** |
| `turnover_inr` | `close_raw × volume` — the exchange print times the traded quantity, not the adjusted series (STRATEGY §1's reading of a rupee number) |
| `turnover_avg_20` | mean `turnover_inr` over `trend.turnover_bars` [20] including today |
| `ema_21` | exponential MA of `close`, span `exits.trail_ema_bars` [21], `adjust=false`, **null until 21 bars exist** |

`bars_required` = `max(trend.dma_bars, scan.vol_sma_bars, exits.trail_ema_bars,
trend.breakout_high_bars + 1, trend.ret_bars + 1) + 1` = **201**. A caller may pass more; less is
a `ValueError`.

---

## §2 The calendar: thin sessions and the missing-bar tolerance (`calendar.py`, `DataConfig`)

Two rules, both from STRATEGY §1, and both changing the arithmetic rather than decorating it.

**2.1 Thin sessions are dropped before any rolling statistic.** A session whose traded-name count
is below `thin_session_min_share` [0.25] of the centred rolling median of that count over
`thin_session_window_bars` [41] sessions (`thin_session_min_periods` [5]) is **not a trading
session for this strategy** and is removed from the frame. On the plant's 2017→ history the rule
finds exactly six: `2017-10-19`, `2018-11-07`, `2024-01-20`, `2024-03-02`, `2024-05-18`,
`2025-02-01` — muhurat and special-Saturday sessions where only ~200 names printed.

> **Why it matters and is not cosmetic.** A single such column poisons every 50- and 200-session
> window that spans it. On the first research run the 200-DMA filter "vanished" for most of
> 2024–25 for exactly this reason. VB2 pins the six dates against the plant's own bars.

The six dates are **not** hard-coded as a list anywhere. The rule is the contract; the list is a
test's expectation. A seventh muhurat session in 2027 must be found by the rule, not by an edit.

**2.2 A rolling window tolerates 10% missing bars.** A rolling statistic over *n* sessions is
valid once the window holds at least `max(2, round(n × rolling_min_share [0.90]))` bars.

> **Why.** A screener that only sees traded bars computes an average over the bars it saw. An
> illiquid name that did not trade on two of fifty sessions still has a 50-day volume average;
> demanding a full window would blank it and silently shrink the universe to the most liquid
> names — which is precisely the population the strategy is *not* trying to be selective about
> at this stage (filter F does that job explicitly, later and on purpose).

---

## §3 The signal (`signals.py`, `ScanConfig` + `TrendConfig`)

Evaluated at the close of session *t*. A row that passes 3.1 is a **scan hit**; a row that also
passes 3.2 is a **signal** and the only thing the plan may act on.

### 3.1 The five Chartink lines (`chartink_scan`, `ScanConfig`)

| # | Rule | Field | Value |
|---|---|---|---|
| 1 | `volume > vol_sma_50 × vol_mult` | `scan.vol_mult` | **3.0** |
| | window for that SMA | `scan.vol_sma_bars` | **50** (includes the signal day) |
| 2 | `close_raw > min_close_raw_inr` | `scan.min_close_raw_inr` | **₹30.0** — an exchange price, never the adjusted close |
| 3 | `change_pct ≥ min_change_pct` | `scan.min_change_pct` | **6.5** |
| 4 | `vol_sma_50 ≥ min_vol_sma` | `scan.min_vol_sma` | **25,000** |
| 5 | `volume > min_volume` | `scan.min_volume` | **50,000** |

Comparison senses are part of the contract: 1 and 5 are strict `>`, 3 and 4 are `≥`. They are
Chartink's own, read literally.

### 3.2 The six trend filters (`trend_filters`, `TrendConfig`)

| | Rule | Field | Value |
|---|---|---|---|
| A | `close > sma_200` | `trend.dma_bars` | **200** |
| B | `close > high_20_prior` | `trend.breakout_high_bars` | **20** (the prior 20 sessions, today excluded) |
| C | `ret_20_pct < max_ret_20_pct` | `trend.max_ret_20_pct` | **25.0** (strict `<`) |
| D | `close_position ≥ min_close_position` | `trend.min_close_position` | **0.6** |
| E | `change_pct ≤ max_change_pct` | `trend.max_change_pct` | **15.0** |
| F | `turnover_avg_20 ≥ min_turnover_avg_inr` | `trend.min_turnover_avg_inr` | **₹2,00,00,000** (₹2 crore), window `trend.turnover_bars` **20** |

**A `null` on either side of a comparison is a fail, never a pass.** A name without 200 bars has
no `sma_200` and is not a signal.

**Exactly these six.** `Close > 50-DMA` and `20-day ADR ≤ 8%` were tested and dropped as
redundant (STRATEGY §3). They are **not** fields of `TrendConfig`, because a field that exists is
a field somebody turns on. `DECISIONS-VB.md` **VB0.2** records that the research script
`grid2.py` still carries both and is the stale half: the six-filter reading reproduces STRATEGY's
6,293 signals and 18.23% CAGR exactly, the eight-filter reading gives 6,254 and 17.47%.

### 3.3 The circuit flag

`locked_upper_circuit = upper_circuit is not null and upper_circuit > 0 and high ≥ upper_circuit`.
The row is **kept and flagged, never dropped**; the plan skips it with `LOCKED_UPPER_CIRCUIT`
(§9.1). Where `upper_circuit` is absent, **no lock is assumed** and the page says so.

### 3.4 The rank key

`rank_key = turnover_inr` of the signal day — the signal-day rupee turnover, as an integer.
Ranking by day-change instead costs 6 CAGR points and no ranking costs 4.6 (STRATEGY §4), so
this is a rule, not a tiebreak convenience.

---

## §4 Breadth and the gate (`breadth.py`, `BreadthConfig`)

**4.1 The series.** Over the §1 universe, on the session's own bars:

* `measured_count` = names with a bar on the date **and** a valid `sma_200` under §2.2's
  tolerance;
* `above_count` = of those, the ones with `close > sma_200`;
* `pct_above_dma = above_count / measured_count × 100`, or 0 when `measured_count` is 0.

**4.2 The gate.** `OPEN` when `pct_above_dma > breadth.min_pct_above_dma` [**40.0**] — strict
`>`, as the research read it — else `SHUT`.

**4.3 What the gate does and does not do.** A `SHUT` gate refuses **new entries only**. Open
positions are managed exactly as always: their stops rest, their EMA exits fire, their sells go
at the next open. Cash sits idle. STRATEGY §4: no gate is the same CAGR at twice the drawdown.

**4.4 The plateau, so nobody tunes it.** 30% → 15.6% CAGR at −45% drawdown (2018 gets in);
35% → 18.6% / −33%; **40% → 18.2% / −27.9%**; 45% → 15.4% / −31%; 50% → 10.9% / −30%. 35–45 is a
plateau and 40 sits in it. A change to this number is a strategy change with a `DECISIONS-VB`
entry, not a tuning pass.

**4.5 This is not `market_health_daily`.** That table's `pct_above_200dma` is measured over an
index's point-in-time membership. This one is measured over the whole traded universe on this
run's thin-session calendar. They answer different questions and will differ; the `vb_` series
makes no claim about the other.

**4.6 One name can sit exactly on its own average, and two libraries can disagree about it.**
Measured at VB2: over the study's 2,396 sessions this series and the research's agree on **the
gate's verdict at 40% on every single session**, and the percentages themselves agree to within
**one name in the numerator** — about 0.1 of a percentage point out of ~1,100 measured names. The
cause is a real tie rather than a bug: a penny stock whose adjusted close has been ₹0.10 for
months has a 200-day average of ₹0.10, and Polars' rolling mean returns `0.09999999999999999`
where pandas' returns `0.1`. "Is the close above its own average" then has two defensible
answers. It costs nothing at 40%; at a 35% gate one session of 2,396 flips and 0.2 CAGR points
follow it, which is a fact about how thin the 2018 margin is. `DECISIONS-VB.md` **VB2.2**.

---

## §5 Sizing (`sizing.py`, `SizingConfig`)

Inputs: the sleeve's `equity`, `cash_available`, the `limit_price`, `turnover_avg_20`, and the
config.

**5.1 Refusals, in order** (each is a `SkipReason` in §9.1, never a silently shrunk size):
`NO_SLEEVE_CAPITAL` (equity ≤ 0 — a sleeve seeded at ₹0 plans nothing, `02` §3.4);
`STOP_NOT_BELOW_ENTRY`; `BELOW_MIN_TRADE_VALUE` (`limit_price × qty <
sizing.min_trade_value_inr` [**₹10,000**] — below it the brokerage dominates the edge).

**5.2 The quantity.** `qty = floor(min(by_slot, by_position, by_cash, by_turnover) /
limit_price)` where

* `by_slot = equity / sizing.max_slots` [**10**] — equal weight, 10% of equity a slot;
* `by_position = equity × sizing.max_position_pct` [**12.5**] `/ 100` — the cap;
* `by_cash = cash_available`;
* `by_turnover = turnover_avg_20 × sizing.max_position_vs_turnover` [**0.01**] — never more than
  **1% of the name's 20-day average turnover**, applied when the turnover is known.

`cap` names which one bound, and the plan line's note says so. **Cash spent by earlier lines in
the same plan is not spent twice.**

> The liquidity cap binds nowhere at ₹10 lakh and will at ₹1 crore (STRATEGY §3). It is in from
> day one so the day it starts binding is a line on a page, not a surprise.

**5.3 Counts.** `sizing.max_slots` [**10**] positions — eight slots cost 2.7 CAGR points and
fifteen cost 5.3, so ten is the useful number for this signal count, not a round one.
`sizing.max_new_entries_per_session` [**3**] caps the `PLACE_LIMIT` lines of one plan, and counts
lines **in this plan plus the session's `CONFIRMED`/`SENT` orders**, whatever plan they came from
— so a fourth confirm of an evening is a refusal, not a fourth order. Ties are broken by §3.4's
`rank_key`, descending, then by symbol.

**5.4 The first live sessions** (`02` §3.5). For the first `sizing.first_live_sessions` [**5**]
LIVE sessions the plan is sized at `sizing.risk_multiplier_first_live` [**0.5**] × `by_slot`,
applied **at plan time** before every cap and refusal — so the line shown is the line sent, and
`vb_position.half_risk` tags what it produced. A paper plan (`DRY_RUN`, or the flag off) is full
size: the paper record rehearses the rules at the size the rules describe. `SELL_AT_OPEN`,
`CANCEL_LIMIT` and `ARM_GTT` lines are never touched. The countdown is the evening job's, once
per LIVE session (`vb_session.first_live_counted`), never a request's.

**5.5 One position per name.** A name already in `vb_position` with `quantity_open > 0`, or with
a `vb_order` in `PROPOSED`/`CONFIRMED`/`SENT`, is skipped `ALREADY_HELD` / `ALREADY_WORKING`.
**Never averaged down**, ever, in any state.

---

## §6 Stops and exits (`exits.py`, `ExitConfig`)

Checked in this order. The order is the rule; §11's engine and the live evening job walk the same
function.

**6.1 The disaster stop.** `stop_price = tick_floor(fill_price × (1 − exits.stop_pct [**12.0**] /
100))`, armed as a **GTT the same session** (non-negotiable 4). It is measured from **the fill**,
not from the signal close and not from the cost-adjusted entry. A stop at or above the fill is an
error, not a position. The stop **only ever rises** (`apply_stop` takes the max) and the desk
refuses a raise below the resting trigger — though VBT-1 has no trail, so in practice it never
moves. `exits.stop_pct` is the one exit number that is a `vb_config` setting, bounded by
`BASKFY_VBT_STOP_PCT_MAX` [15.0], because STRATEGY §4 measured 10% → 16.4% and 15% → 16.6% and
found the stop to be insurance either way.

**The GTT's cushion.** A GTT fires a LIMIT order. The VBT route passes
`limit_fraction = VBT_GTT_LIMIT_FRACTION` [**0.97**] to `place_gtt_stop`, so the resting limit
sits 3% under its trigger and fills on the way down the way a market stop would — the same
additive keyword the swing sleeve uses, defaulting to the gateway's own value, and the weekly
book never sees it. The band is `StopBand(min_pct=0.005, max_pct=0.15)`: the desk's 8–12% band
is the weekly book's and is not this one's.

**6.2 The working exit: a close below the 21-day EMA.** After the close of session *t*, for every
open position: if `close < ema_21` then the position is **queued to sell at the next session's
open** (`vb_position.exit_queued_for = next session`, `exit_reason_queued = EMA_EXIT`), and the
morning plan carries a `SELL_AT_OPEN` line for exactly `quantity_open`.

> **688 of the 761 trades left this way; 62 hit the stop** (STRATEGY §3). A 10-EMA is far too
> tight (6.3% CAGR); a 50-SMA exit holds longer for the same CAGR at a deeper drawdown. This is
> the exit; the stop is the insurance.

`exits.trail_ema_bars` [**21**]. The EMA is computed on the **adjusted** close with `adjust=false`
and is null until 21 bars exist; a position in a name with no EMA yet is held, never sold on a
null.

**6.3 No target, no partial, no time stop.** Each was tested and each lowers the result, because
the book's profit is a right tail (average winner +16.5%, average loser −6.1%, best trade +242%,
the ten best trades 24% of gross profit). `ExitConfig` has **no** `target_*`, `partial_*` or
`max_hold_*` field, deliberately — see §3.2's reasoning about fields that exist.

**6.4 A gap through the stop fills at the open.** In the book this is what actually happens; in
the backtest it is modelled as: if the session's open is at or below the stop, the exit is at the
open; else if the session's low is at or below the stop, the exit is at the stop.

**6.5 No bar.** A held name that stops printing is written off at its last available close after
`exits.no_bar_tolerance_sessions` [**5**] blank sessions, `close_reason = NO_BAR`. On the live
book this is an alert (`VBT_POSITION_NO_BAR`) as well as an exit line — a delisting is a fact a
person must see.

**6.6 Precedence within one session**, top-down: a queued `EMA_EXIT` at the open → a stop gapped
through at the open → a stop touched intraday → (at the close) queue an `EMA_EXIT` for tomorrow.
A position entered today can be stopped out today: the same session's low is checked against the
fresh stop, and the fill is the stop, or the open if the open was already below it.

---

## §7 The entry: a limit that works for three sessions (`orders.py`, `EntryConfig`)

**This is the one mechanism the desk does not have today** and the reason VB7 is a module of its
own. STRATEGY §3: *"Do not chase the open."*

**7.1 The level.** `limit_price = tick(signal bar's close, as an exchange price)`. Not the high,
not a buffer above it, not the next open. `entry.limit_at` = `SIGNAL_CLOSE` names it.

**7.2 The window.** The order works for `entry.valid_sessions` [**3**] sessions after the signal
session: it may fill on *t+1*, *t+2* or *t+3*, and at the close of *t+3* it is cancelled.
**Sessions are counted on §2's calendar**, so a holiday does not consume one.

> **This is the parameter with a cliff.** Two sessions → 11.4% CAGR; three → **18.2%**; five →
> 17.1% (STRATEGY §4). Two is too short for the pullback to arrive. Anyone shortening it is
> changing the strategy, and 06's VB2 pins all three numbers.

**7.3 The fill model, and where the book and the backtest part company.** In the backtest a
session fills the order when its `low ≤ limit_price × (1 − entry.fill_through_pct [0.0] / 100)`,
at `min(open, limit_price)` — the better of the open and the limit. Requiring the low to trade
0.25% *through* the limit changes CAGR by 0.02 pt, which is why the default is 0.0 and the field
exists anyway.

> **In life the order rests at the exchange and fills or does not.** The backtest's assumption is
> optimistic in exactly one way: it fills every touch. The live book journals the truth —
> `vb_order.filled_quantity` and the broker's own fills — and `05` §2 shows the two side by side
> as "modelled fill rate 91%" against "this book's fill rate", because that difference is the
> single most likely place the live result parts company with the study. VB9's drift flag reads
> CAGR; this line is the one a human reads.

> **And the fill test is an equality, not an inequality, more often than it looks.** Yesterday's
> close *is* today's low often enough to matter — it is what a pullback to the previous close
> looks like — so the comparison has to be exact. Computing `limit × 1` before comparing is not a
> no-op: a bar price converted from a float carries about fifty significant digits and Decimal
> rounds a product to twenty-eight, which pushes such a limit a hair above the low that touched
> it and the order never fills. VB2 found this as 589 missing trades; `_fill_threshold` in
> `backtest.py` is the one line that fixes it. `DECISIONS-VB.md` **VB2.1**.

**7.7 The book sizes at plan time; the backtest sizes at the fill.** A resting order must carry a
quantity when it is placed, so the evening plan sizes it against the sleeve as it stands that
night. The study sized each entry at the moment it filled, against the equity of the session
before. Over a three-session window the difference is small, and it is a **structural** one: the
same `size_entry` runs in both places, at different moments. Where a page compares the two, it
says which.

**7.4 A locked open.** A session whose `open == high == low` for the name is a locked circuit and
no fill is possible; the order keeps working and the session is not counted against §7.2's three.

**7.5 Partial fills.** A partially filled order keeps working for the rest of its window for the
remainder, and the position it opened is real from the first fill: its GTT is armed for the
quantity filled and **modified** as more arrives (the swing sleeve's `modify_gtt_quantity` path,
reused, not re-invented). At expiry the remainder is cancelled; the position stands.

**7.6 The order is placed only from a confirm.** A `PLACE_LIMIT` line is `PROPOSED` until a
person confirms it on the desk page (`02` Track C §3). The evening job never places anything.

---

## §8 Costs and the fill economics (`CostConfig`)

`costs.cost_pct_per_side` [**0.25**] — 25 basis points a side, covering STT, exchange and
brokerage charges and slippage. **It is used by the backtest only**; a live fill's cost is
whatever the broker charged and is read from the journal, never modelled.

In the backtest the buy's cost basis is `fill × (1 + cost)` and the sell's proceeds are
`fill × (1 − cost)`. At ₹10 lakh in ₹2 crore+ names 25 bps is realistic; 40 bps → 15.5% CAGR and
60 bps → 11.9% (STRATEGY §4), which is what scale does to this strategy and is why §5.2's
turnover cap is in from day one.

---

## §9 The plan (`plan.py`)

**9.1 `build_entries`.** Signals of the session sorted by `(−rank_key, symbol)`; for each, in
order — the order *is* the contract, because it decides which reason a skip carries:

| Check | Skip reason |
|---|---|
| sleeve equity ≤ 0 | `NO_SLEEVE_CAPITAL` |
| gate `SHUT` (§4.2) | `GATE_SHUT` |
| `locked_upper_circuit` | `LOCKED_UPPER_CIRCUIT` |
| the name is held | `ALREADY_HELD` |
| the name has a working or confirmed order | `ALREADY_WORKING` |
| `lined + entries_already_this_session ≥ sizing.max_new_entries_per_session` | `SESSION_CAP` |
| `open_positions + working_orders + lined ≥ min(vb_config.max_open_positions, sizing.max_slots)` | `SLOTS_FULL` |
| the stop is not below the limit | `STOP_NOT_BELOW_ENTRY` |
| the sized value is below the floor | `BELOW_MIN_TRADE_VALUE` |
| the turnover cap alone makes it too small | `TURNOVER_CAP` |
| open exposure + this value > equity | `EXPOSURE_FULL` |

else a `PLACE_LIMIT` line with `limit_price` and `stop_price` snapped to the tick, `quantity`,
`value_inr`, and a note naming the cap that bound.

> **A working order holds a slot.** It has to: the strategy's whole point is that it bids and
> waits, and a book that could line eleven limits for ten slots would over-commit its cash on the
> day they all filled. `SLOTS_FULL` counts positions **and** working orders.

**9.2 `exit_lines`.** From §6: `SELL_AT_OPEN` for every position queued by last night's close,
`CANCEL_LIMIT` for every working order at the end of its third session, `ARM_GTT` for every
filled position with no resting GTT.

**9.3 `assemble`.** Exits first, then cancels, then entries; totals over the entries; `plan_hash`
is the sha256 of the canonical lines — **the same plan hashes the same**.

**9.4 The desk's contract.** A plan gets a `plan_id` and a **30-minute expiry**;
`POST /vbt/execute` needs `confirm=true`, an unexpired `plan_id` and a `line_id`;
`client_id = plan_id:symbol:kind` so a re-post cannot double-send. **A line's size is a preview;
the confirm is the gate**: under a row lock on the day's `vb_session`, the desk re-reads the book
and re-sizes the line through this section's own `build_entries` against the current cash, slots
and session cap. A line that fits goes as planned; one that only the exposure or cash ceiling
refuses is **shrunk to the headroom** (never grown past what the page showed); one the rules
cannot line at all is `BLOCKED` with the skip's code leading the reason and the line marked
`REJECTED` — never sent, never left `CONFIRMED`.

**9.5 What a confirm sends.** `PLACE_LIMIT` → `OrderGateway.place(side=BUY, product=CNC,
order_type=LIMIT, price=limit_price, validity=DAY, client_id=…)`. **Not** a GTT-buy and not a
market order: the level is the strategy. The GTT stop is armed by the *fill*, not by the placement
— §6.1's "the same session" means the session the fill happened in, and the evening's `ARM_GTT`
sweep is the backstop. `SELL_AT_OPEN` → `place(side=SELL, order_type=MARKET, product=CNC)` for at
most `quantity_open` of a position **this sleeve owns**. `CANCEL_LIMIT` → the gateway's guarded
`cancel_order`. Every one of them goes through the gateway: guards → risk → rate limit → journal.

---

## §10 Which session the sleeve reads, and why it is not today

CLAUDE.md, "Which date the product shows": a daily bar is a *closed* day.

* **Signals and breadth are always the last completed trading session.** There is no VBT signal
  for a half-finished day, ever, and no configuration produces one. The nightly job runs after
  `publish`; the 21:00 IST retry runs against the same session.
* **The plan's `source=EVENING`** is built from that session's close, for orders to be sent the
  next morning. `source=MORNING` rebuilds the same plan before the open — same signals, same
  levels, re-sized against the sleeve's current cash — because the desk's plans expire in 30
  minutes and an evening plan cannot be confirmed at 09:20.
* **A working order's session count** advances on published sessions (§2's calendar), so it does
  not tick over a holiday and does not tick twice on a re-run.
* **Marks**, when a page shows the sleeve's value during a session, are live Kite quotes — the
  same split CLAUDE.md draws for the portfolio and the swing book, for the same reason: those are
  prices, not bars.

---

## §11 The backtest (`backtest.py`) — the study, re-run by the book's own functions

VB2 reproduces STRATEGY §4 from the same bars; VB9 re-runs it from the plant's bars and flags
drift. **Both use the functions above** — `detect_signals`, `breadth_above_dma`, `size_position`,
`manage`, `expire_orders` — so the backtest cannot drift from the book by construction. The
engine adds only the sequencing:

For each session *j* on §2's calendar, in this order:

1. **Exits at the open.** A queued `EMA_EXIT` fills at the open. Otherwise, if `open ≤ stop` the
   stop fills at the open; else if `low ≤ stop` it fills at the stop. A name with no bar for
   `exits.no_bar_tolerance_sessions` [5] sessions is written off at its last close.
2. **Working orders.** Yesterday's signals become working orders; orders past their window
   expire. **Both happen whether or not the gate is open** — the gate governs fills, not
   bookkeeping, which is how the research read it and is what `05` §2's "waiting" count means.
3. **Fills**, only if the gate was `OPEN` at session *j−1*'s close: candidates ranked by §3.4,
   each filled at `min(open, limit)` when `low ≤ limit`, subject to §5's caps and §9.1's order of
   refusals, at most `max_new_entries_per_session` a session.
4. **The same session's low can take a fresh entry out** — conservative: the stop is checked
   before any favourable move is counted.
5. **At the close**: mark equity, and queue tomorrow's `EMA_EXIT` for every position whose close
   is below its 21-EMA.
6. At the end of the run, open positions are liquidated at the last close so the equity curve
   reads cleanly. Those exits are labelled `END_OF_RUN` and are **counted as trades**: the
   study's 761 includes ten of them, and dropping them would flatter the win rate by hiding the
   positions the book was still carrying when the history ran out.

**The gate is read at *j−1*.** A signal at session *t*'s close is acted on at *t+1*, and the gate
that governs it is the breadth of *t* — the same close that produced the signal. VB4's
look-ahead test shifts the series by one session and asserts the number of entries changes.

**Three details the sequencing turns on**, each of which changes a number if it is read the
other way and each of which was settled against the study at VB2:

* **The turnover cap reads the previous session's 20-day average**, not the signal day's — the
  last one a book sizing at the moment of the fill could have seen. For a next-session fill they
  are the same number; for a fill on the third session they are not.
* **Every price in the engine is the adjusted series**, as the study computed it. The live plan
  converts a level to an exchange price by dividing by the row's `adj_factor` before an order
  carries it (`03` §9). On the signal day those are the same number by definition.
* **The engine's tick is the paise (₹0.01)**, which is what the study floored stops and rounded
  exits to. The desk snaps the levels it *sends* to ₹0.05 (§7.1). Reproducing the study means
  using the study's tick, and `BacktestParams.tick` is the field that says so.

**Reported**: CAGR, max drawdown and its dates, Sharpe (daily, 0 rf), Calmar, trades, win rate,
profit factor, average win/loss/trade, average hold, exposure %, the yearly and monthly tables,
the equity curve, the funnel (universe → with a bar → scan hits → signals → orders → fills), and
**three books over one detection pass**: `gate_off` (breadth replaced by `OPEN` every session),
`full` (the gate as specified), and `raw_scan` (the five Chartink lines with the same gate and
the same execution). The primary book is `full`. Breadth's contribution is `full − gate_off`; the
trend filters' is `full − raw_scan`.

**Stated on the page** (`05` §3), and this list is part of the contract: fills are modelled, not
experienced (§7.3); no intraday data, so a limit that the day's low touched is assumed filled;
`upper_circuit` is absent before 2020 and no lock is assumed where it is missing; survivorship is
handled by `instrument.delisted_on` where known and by §6.5 where it is not; corporate actions
before 2024 are as the source adjusted them; and STRATEGY §5 verbatim.

---

## §12 The contract, in one table

Every field of `baskfy_core.vbt.config`, with the value it holds. **This table is generated from
the code and asserted against it** (`packages/core/tests/test_vbt_docs_parity.py`): a field added,
renamed or re-valued without an edit here turns that test red, and a row here with no field
behind it turns it red too. The sections above say *why* each number is what it is; this one
exists so the two can never quietly disagree.

Units are §0's: `*_pct` is a percent, `*_bars` and `*_sessions` count sessions of §2's calendar,
`*_inr` is rupees.

| Field | Value |
|---|---|
| `data.instrument_type` | `EQ` |
| `data.series_allowed` | `EQ, BE, BZ` |
| `data.keep_null_series` | `true` |
| `data.etf_universe_slug` | `etf` |
| `data.etf_name_pattern` | `\bETF\b` |
| `data.etf_symbol_pattern` | `(BEES|ETF|IETF)$` |
| `data.thin_session_min_share` | `0.25` |
| `data.thin_session_window_bars` | `41` |
| `data.thin_session_min_periods` | `5` |
| `data.rolling_min_share` | `0.9` |
| `scan.vol_mult` | `3.0` |
| `scan.vol_sma_bars` | `50` |
| `scan.min_close_raw_inr` | `30.0` |
| `scan.min_change_pct` | `6.5` |
| `scan.min_vol_sma` | `25000.0` |
| `scan.min_volume` | `50000.0` |
| `trend.dma_bars` | `200` |
| `trend.breakout_high_bars` | `20` |
| `trend.ret_bars` | `20` |
| `trend.max_ret_20_pct` | `25.0` |
| `trend.min_close_position` | `0.6` |
| `trend.max_change_pct` | `15.0` |
| `trend.turnover_bars` | `20` |
| `trend.min_turnover_avg_inr` | `20000000.0` |
| `breadth.dma_bars` | `200` |
| `breadth.min_pct_above_dma` | `40.0` |
| `entry.limit_at` | `SIGNAL_CLOSE` |
| `entry.valid_sessions` | `3` |
| `entry.fill_through_pct` | `0.0` |
| `sizing.max_slots` | `10` |
| `sizing.max_position_pct` | `12.5` |
| `sizing.max_new_entries_per_session` | `3` |
| `sizing.max_position_vs_turnover` | `0.01` |
| `sizing.min_trade_value_inr` | `10000.0` |
| `sizing.risk_multiplier_first_live` | `0.5` |
| `sizing.first_live_sessions` | `5` |
| `exits.stop_pct` | `12.0` |
| `exits.gtt_limit_fraction` | `0.97` |
| `exits.gtt_band_min_pct` | `0.005` |
| `exits.gtt_band_max_pct` | `0.15` |
| `exits.trail_ema_bars` | `21` |
| `exits.no_bar_tolerance_sessions` | `5` |
| `costs.cost_pct_per_side` | `0.25` |

Four of these are also bounded `vb_config` settings a person may change without a code change —
`sizing.max_slots`, `sizing.max_position_pct`, `exits.stop_pct` and the sleeve's capital — and
`02` §2 carries their ceilings. Everything else is a code change with a `DECISIONS-VB.md` entry
(PACK.5).
