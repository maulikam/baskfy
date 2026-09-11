# 04 — Business rules: the numerical contract

**Every number in this document is a field of `baskfy_core.twt.config` and nothing downstream
compares to a literal.** A detector, a task, a router and a page each read a field of one of these
dataclasses, so a recalibration is an edit here plus an edit there, and a diff in one place. The
tests in `packages/core/tests/test_twt_*.py` assert **this document**, never current behaviour
(house rule 2), and `test_twt_no_literals.py` scans the sleeve's source for a threshold written as
a number.

Units, stated once:

* every `*_pct` is a **percent** (`20.0` means 20 %), never a fraction — except
  `max_position_vs_turnover`, which is a **fraction** (`0.01`) because it multiplies money and the
  research's own `Rules` field was a percent that read as a bug every time it was quoted;
* every `*_bars` / `*_sessions` counts **trading sessions on the run's own calendar** (§2), never
  calendar days — except `month_low_months_back`, which counts **calendar months**, because
  Chartink's monthly candle does;
* every `*_inr` is **rupees**.

What is deliberately absent: there is no `target_pct`, no `partial_*`, no `max_hold`, no
`exit_close_below_sma` and no `exit_close_below_ema` field. `01` §5 records that each was measured
and each lowered the result, and **a field that exists is a field somebody turns on.** There are
also no trend filters: `01` §4 measured them and they hurt.

---

## §1 The universe

1.1 NSE cash equities: `instrument.instrument_type = "EQ"` and `series ∈ {EQ, BE, BZ}`
(`DataConfig.instrument_type`, `series_allowed`). SME series (`SM`, `ST`, `SZ`) are out.

1.2 A **null** series is a delisted name with no current listing row and is **kept**
(`keep_null_series = True`). Dropping today's list would drop the history of names later demoted —
a survivorship bias in reverse.

1.3 ETFs are out: the `etf` index universe is the authority (`etf_universe_slug = "etf"`), and two
narrow patterns catch one that never made the list — `etf_name_pattern = r"\bETF\b"`,
`etf_symbol_pattern = r"(BEES|ETF|IETF)$"`. Narrow on purpose: "GOLD" would flag GOLDIAM.

1.4 Identical to `docs/vbt/04` §1 and asserted to be so by `test_twt_neighbours.py`: two sleeves
drawing from different universes would make `01` §6's "zero shared trades" a statement about
populations rather than about events.

## §2 The calendar and the windows

2.1 **The thin-session rule.** A session whose traded-name count is below
`thin_session_min_share` [0.25] of the centred rolling median of that count over
`thin_session_window_bars` [41] sessions (`thin_session_min_periods` [5]) **is not a trading
session for this strategy** and is removed before any rolling statistic is computed. Muhurat and
special-Saturday sessions print about 200 names against about 1,900, and a single such column
poisons every 50- and 200-session window that spans it.

The rule is the contract; the six dates it finds on the 2017 → history (`2017-10-19`,
`2018-11-07`, `2024-01-20`, `2024-03-02`, `2024-05-18`, `2025-02-01`) are a test's expectation. A
seventh muhurat session in 2027 must be found by the rule, not by an edit.

2.2 **The missing-bar tolerance.** A rolling window over *n* sessions is valid once it holds
`max(2, round(n × rolling_min_share))` bars, `rolling_min_share = 0.90` — the way a screener that
only sees traded bars computes an average. Demanding a full window would blank every name with an
occasional no-trade day and silently shrink the universe to the most liquid names, which is the job
the liquidity floor does explicitly, later and on purpose.

2.3 **`bars_required = 260`.** The deepest window the sleeve reads is not the 200-session average;
it is §3.3's month-3 low, which needs every session of the calendar month three months before the
as-of session's month. Three months plus the current month's elapsed part is at most about 85
sessions, so 200 governs — but the 200-session *average* also needs 200 valid bars under §2.2 and
the thin-session drops of §2.1 consume some. 260 is 200 + a quarter's slack + the six thin
sessions, and TW1's test walks a year of month boundaries asserting that the month-3 low is
present for every one of them.

## §3 The scan (`signals.py`)

### §3.1 The five lines, read literally on the closed daily bar

The comparison senses are part of the contract and are Chartink's own.

| # | Rule | Field | Value |
|---|---|---|---|
| 1 | `close_raw > min_close_raw` | `ScanConfig.min_close_raw` | **30.0** |
| 2 | `close ≥ month_low_multiple × month_low_back` | `ScanConfig.month_low_multiple` | **1.3** |
| | the month read back | `ScanConfig.month_low_months_back` | **3** (calendar months) |
| 3 | `(max(W) / min(W) − 1) × 100 ≤ tight_band_pct` | `ScanConfig.tight_band_pct` | **3.01** |
| | how many weekly closes are in `W` | `ScanConfig.tight_weeks` | **3** |
| 4 | *market cap > 1* | — | **not implemented.** A no-op in Chartink and a no-op here; naming a field for it would invite somebody to give it a number |
| 5 | `vol_sma ≥ min_vol_sma` | `ScanConfig.min_vol_sma` | **10 000.0** |
| | the volume average's window | `ScanConfig.vol_sma_bars` | **50** (includes the signal day) |

Line 1 reads **`close_raw`**, the exchange print. A ₹28 name that a 1:2 split makes ₹56 in the
adjusted series did not clear Chartink's line. Lines 2, 3 and 5 read the **adjusted** series,
because they compare a price to another price of a different date, or a volume to an average of
volumes, and a split between the two makes the raw comparison meaningless.

The state is `line 1 ∧ line 2 ∧ line 3 ∧ line 5 ∧ ¬ETF`. A name with any input missing is **not**
in the state; there is no "assume true when unknown" branch.

### §3.2 The three weekly closes — the rule §2 of `01` is about

`W = (w₀, w₁, w₂)` where

* **`w₀` is today's adjusted close.** The current week is a *partial* candle and Chartink's live
  scan reads it as such. This is the point-in-time reading and it is the whole of `01` §2.
* **`w₁`, `w₂` are the last close of each of the two preceding weeks the calendar holds.** A week
  is an **ISO week** (`isocalendar().year × 100 + week`); "the last close" is the last **non-null**
  close in that week, which is what a screener sees; and "the preceding weeks the calendar holds"
  means the two week-buckets immediately before the current one **in the data**, not the two
  calendar weeks before it. On NSE the two coincide; a whole ISO week with no session would make
  them differ, and the data's own answer is the one a screener would give.

The test is `(max(W) / min(W) − 1) × 100 ≤ 3.01`, evaluated only when **all three** are finite.
`abs()` is not needed and is not applied: `max ≥ min > 0` by construction.

**The look-ahead reading is not a configuration of this function.** `tight_state` takes no
`include_current_week` switch. TW2 keeps a separate `tight_state_lookahead` in the *test* module
only, because `01` §2's 83.1 % is the measurement that identifies the gap as Chartink's candle
semantics, and the sleeve must never be able to run it by accident. DECISIONS-TW **TW0.1**.

### §3.3 The month-3 low

`month_low_back(session)` is the **minimum adjusted low over every session of the calendar month
`month_low_months_back` [3] months before the session's own month**. January 2026's sessions read
October 2025's low; March reads December. A session whose month-3 has no bar in the panel has no
value and is **not** in the state.

Measured alternatives, all worse against Chartink's export: two months back, four months back, and
a rolling 63-session low.

### §3.4 The entry event

`entry_events(state, entry_min_sessions_out)` is true on session *t* for instrument *i* when
`state[i, t]` is true **and** `state[i, t−k]` is false for every `k ∈ 1 … entry_min_sessions_out`.

`EntryConfig.entry_min_sessions_out = 5`. Measured: 10 → 19.7 % CAGR at −27 %, 20 → 15.8 % at −32 %.

A name with fewer than `entry_min_sessions_out` sessions of history before *t* — a fresh listing —
**has no entry event**, because "was false for five sessions" is a claim about five sessions that
exist. The research's implementation seeds its counter at a large number and would fire on a
listing day; TW1 does not, and DECISIONS-TW **TW0.6** records the difference and the two trades in
the goldens it moves (if any — TW2 measures it).

### §3.5 The liquidity floor, and the one number that differs from the research

`EntryConfig.min_turnover_inr = 50 000 000` — **₹5 crore**, on the mean of `close_raw × volume`
over `turnover_avg_bars` [20] sessions ending at the signal session (the §2.2 tolerance applies).

**The research's headline used ₹2 crore.** This sleeve ships at ₹5 crore because the research was
run at ₹10 lakh and this sleeve will run at ₹25 lakh: ten slots at ₹25 lakh is a ₹2.5 lakh line,
and §6.2's 1 %-of-turnover cap does not stop binding until the name turns over ₹2.5 crore a day. A
floor below the cap means the plan is routinely sized by the cap rather than by the strategy. ₹5
crore is the coherent floor at this size, and it is also the better of the two in the research's own
sensitivity table (22.5 % CAGR at −27 % on 169 trades, against 20.9 % at −24.7 % on 164).

**Both numbers are named**, because TW2's goldens must reproduce the research and the research used
the other one: `EntryConfig.research_min_turnover_inr = 20 000 000` exists for exactly one caller,
the golden parameter set. It is never read by the detector, the plan or the page, and
`test_twt_no_literals.py` asserts that its only reference outside `config.py` is in the goldens.
DECISIONS-TW **TW0.3**.

An entry event below the floor is stored as a `SCAN_ONLY` row (`03` §3), never dropped.

## §4 The regime gate (`breadth.py`)

4.1 `BreadthConfig.dma_bars = 200`, `BreadthConfig.min_pct_above_dma = 40.0`.

4.2 The reading, for one session: the denominator is the names that **printed a bar** on the
session **and** have a valid 200-day average under §2.2's tolerance; the numerator is those whose
adjusted close is above it. A name whose average is still warming up is in neither — counting it as
"not above" would report a listing wave as a bear market.

`pct_above_dma = above_count / measured_count × 100`, four decimal places. `measured_count = 0`
gives `0.0000` and a **SHUT** gate: a session the panel cannot measure is not a session the book
enters on.

4.3 `gate = OPEN` when `pct_above_dma > min_pct_above_dma`, strictly; `SHUT` at or below it. Two
values, not three: `01` §5 is a single threshold and the sensitivity table measures it as one.
There is no amber and no exposure ladder — that is the swing book's shape, and this book has ten
equal slots.

4.4 **One implementation.** `baskfy_core.twt.breadth` calls `baskfy_core.vbt.breadth.breadth_series`
and `breadth_above_dma`, passing a `VbtConfig` whose breadth section is TWT's own
`BreadthConfig(dma_bars=200, min_pct_above_dma=40.0)` — **spelled out, not inherited**, so a VBT
recalibration cannot silently move this sleeve's gate, and `test_twt_docs_parity.py` pins both
numbers literally. The arithmetic is shared because two implementations of one measurement is the
one thing that can make two pages disagree about the same day. DECISIONS-TW **TW0.4**.

4.5 The gate a plan reads is the row of the **signal session** — the last completed session — never
a later one. §11 is the clock rule.

## §5 The entry

5.1 **Next session's open, market order.** `EntryConfig.entry = NEXT_OPEN` and it is the only
member of its enum. `01` §5 measured the alternatives: a buy-stop at the 15-session base high
returns 3.7 % CAGR, a limit at the signal close 11.6 %. There is no `entry_valid_sessions` field
because a market order at the open does not work for sessions.

5.2 A name whose session opens `open == high == low` is **limit-locked at the open** and is
skipped, `LOCKED_UPPER_CIRCUIT`: no fill is possible at a price the book would accept. So is a name
with no bar (`NO_BAR`).

5.3 The fill price the book records is the broker's. The *backtest* models it as the session's open
and charges `CostConfig.cost_bps_per_side` [25.0] basis points on top, so the modelled book entry
is `open × (1 + 0.0025)`. §7.1's stop is computed from the **open**, not from the cost-inclusive
entry, which is what the research does and what a person reading a chart would do.

## §6 Sizing (`sizing.py`)

6.1 Equal weight. The target value of one new line is `sleeve_equity / SizingConfig.max_slots`,
`max_slots = 10`. Eight slots returns 26.3 % CAGR at −27 % and fifteen returns 14.7 % at −29 %: ten
is the useful number for this signal count, not a round one.

6.2 The caps, applied in this order, each recorded in the line's note when it binds:

| cap | field | value |
|---|---|---|
| per-position ceiling | `SizingConfig.max_position_pct` | **12.5 %** of sleeve equity — above the slot size, so it binds only when equity has drifted |
| the name's liquidity | `SizingConfig.max_position_vs_turnover` | **0.01** — 1 % of the 20-session average turnover |
| the sleeve's cash | — | never more than `cash_available` (`03` §1, `06`'s TW5) |
| the floor | `SizingConfig.min_trade_value_inr` | **₹10 000** — below it brokerage dominates the edge; a line that cannot clear it is skipped `BELOW_MIN_TRADE_VALUE` |

`quantity = floor(target_value / entry_price)`, an integer number of shares.

6.3 **At most `SizingConfig.max_new_entries_per_session` [3] new entries a session**, counted as
lines in this plan **plus** the session's already-confirmed or sent orders, whatever plan they came
from — so a fourth confirm of an evening is a refusal, not a surprise. When more signals than slots
exist they are ranked by **`rank_key` descending, then symbol ascending**, and `rank_key` is
`EntryConfig.rank_key = SIGNAL_TURNOVER`: the **signal session's own turnover**, `close_raw ×
volume` of that day.

> **Why not the 20-day average.** `research/tight-close/STRATEGY.md` §3 says "ranked by 20-day
> turnover". The code that produced every number in `01` §6 ranks by `ind.turnover`, which is the
> signal day's own turnover (`vbt/scan.py`, `vbt/sim.py`'s `key["turnover"]`). The numbers are the
> fact and the prose is the stale half, so this sleeve ranks the way the measured strategy ranked.
> DECISIONS-TW **TW0.2** records it, and TW0 adds one correcting line to the research note rather
> than leaving the two disagreeing — CLAUDE.md's rule of 9 Sep 2026, applied to a research note
> instead of a setting.

Ranking matters: by nothing 15.8 %, by relative volume 17.2 %, by day-change 16.3 %, by turnover
20.9 %.

6.4 **Half size for the first ten live entries.** While `tw_config.first_live_entries_left > 0`
**and** `BASKFY_TWT_EXECUTION_ENABLED` is true, the target value of a new line is multiplied by
`SizingConfig.risk_multiplier_first_live` [0.5] **before** every cap in §6.2, so the line shown is
the line sent. `SizingConfig.first_live_entries = 10`.

It counts **entries, not sessions**. The swing book and VBT-1 count sessions because they enter
most days; this book enters about eighteen times a year, and a five-session allowance would be
spent by a quiet week. The counter is decremented once per **filled** entry by the session that
filled it (`tw_session.first_live_entries_counted`), never by a request, never by a plan that
proposed a line nobody confirmed. A `DRY_RUN` plan is **full size**: half size is a live-money
discipline, and a paper plan that is not the plan is not a rehearsal.

## §7 The exits — and there are exactly two

### §7.1 The disaster stop

`ExitConfig.stop_pct = 20.0`. The initial stop is

```
initial_stop = tick_floor(fill_price × (1 − stop_pct / 100))
```

armed as a GTT **in the same session as the fill** (non-negotiable 4). `fill_price` is the
exchange's, not the cost-inclusive book entry (§5.3).

Measured: 15 % → 21.6 % CAGR at −23 %, 30 % → 19.4 % at −25 %, **10 % breaks the strategy** — these
names swing more than 12 % inside their own bases, so a tight stop converts the median trade into a
stop-out. The band 15–30 is flat, which is why `stop_pct` is a bounded setting (`02`) rather than a
constant.

A stop that is not below the entry is refused, `STOP_NOT_BELOW_ENTRY`. It cannot arise at 20 % and
the check is there because a ceiling edit could make it arise.

### §7.2 The trailing stop — the ratchet

`ExitConfig.trail_pct = 20.0`. After every close on which the position is open:

```
high_since    = max(high_since, session_high)                       # exchange prints
raw_trigger   = max(stop_in_force, high_since × (1 − trail_pct / 100))
trigger       = tick_floor(min(raw_trigger, close × close_clamp_fraction))
                if raw_trigger < close
                else tick_floor(close × close_clamp_fallback)
```

`ExitConfig.close_clamp_fraction = 0.9999`, `ExitConfig.close_clamp_fallback = 0.999`.

`high_since` is initialised to the **fill price** and includes the entry session's own high.

**The clamp is not decoration.** A trigger at or above the last traded price fires the moment it is
armed, which on a GTT means selling the position at the next tick for no reason. The research code
clamps for the same purpose and this sleeve reproduces it exactly so TW2's goldens can be a tick
comparison. The desk's own `_raise_stop` refuses a trigger at or above the last price
independently, so the clamp is belt and braces — but the belt is what makes the plan line
*correct*, and the braces only make it *safe*.

**A stop never falls.** `raw_trigger` takes the maximum with the stop in force, `tw_position` has a
check constraint, and the desk refuses a `RAISE_GTT_STOP` at or below the resting trigger. Three
places, because this is the rule whose violation is silent: a stop that quietly fell is a stop
nobody notices until it does not fire.

**The ratchet is a plan line, never a job.** The evening computes `next_trigger` and stores it
(`03` §5); the morning's plan carries a `RAISE_GTT_STOP` line for every position whose
`next_trigger` exceeds its resting `gtt_trigger`; a person confirms it. Track C §3.

137 of the research's 164 exits are this stop. Tighter is a cliff (15 % → 9.6 % CAGR at −43 %),
wider thins the book to nothing (30 % → 15.5 % on 73 trades). 20–25 % is the plateau.

### §7.3 What a corporate action does to a stop that has rested for months

`high_since` and `gtt_trigger` are **exchange prices** (`PRICE_RAW` and `PRICE`). When
`ohlcv_daily.adj_factor` changes for an instrument the sleeve holds — a split, a bonus — the
evening job detects it (the as-of row's factor differs from the position's `entry_adj_factor`) and:

1. re-derives `high_since` from the **adjusted** high series scaled by the new factor, so the level
   means the same fraction of the price it meant yesterday;
2. writes `next_trigger` from that re-derived `high_since`;
3. raises `TWT_ADJUSTMENT_RESET`, because the resting GTT at the exchange is now quoting a
   pre-split price on a post-split instrument and **a person has to look at it**. The plan's
   `RAISE_GTT_STOP` line carries the reason.

It cannot silently lower a stop: if the re-derived trigger is *below* the resting one — which is
what a split does arithmetically — the line is **not** emitted and the alert is. Cancelling a
resting stop and arming a lower one is the one thing this sleeve must never do on its own, and a
split is the one event that would make it look correct. DECISIONS-TW **TW0.7**.

### §7.4 The fill-day rule

A position whose **entry session's own low** is at or below its initial stop is out that session
(`STOP_DAY0`), filled at the stop, or at the open when the open was already below it. The research
calls it `stop_day0` and it is 20 %-down-from-the-open on the day of purchase: rare, and real.

In the live book the GTT armed in the same session **is** this rule, which is why non-negotiable 4
is not negotiable here: a fill without a same-session GTT is a position with no fill-day
protection at all. TW7 is that module, and its sweep asserts that every open line has a resting
GTT before 15:30.

### §7.5 The no-bar write-off

`ExitConfig.no_bar_sessions = 5`. A position whose instrument prints no bar for five consecutive
sessions is closed at its last known close, reason `NO_BAR` — a delisting or a suspension, and a
book that carries such a line forever reports an equity it cannot realise.

### §7.6 What there is not

No target. No partial. No time stop. No moving-average exit. `01` §5 measured all four. The
50-SMA exit is a *different strategy* with the same signal (543 trades, 15.6 % CAGR, −38.3 %
drawdown) and it is not this one.

## §8 Costs

`CostConfig.cost_bps_per_side = 25.0`. Charged on both sides in the backtest; the live book's
costs are the broker's and are journalled, not modelled. Long holds make costs nearly irrelevant
here: 40 bps a side gives 20.3 % and 60 bps gives 19.5 %.

## §9 The sleeve's own money

9.1 `sleeve_equity(user_id, as_of)` = `tw_config.sleeve_capital_inr` + realised P&L of `CLOSED`
positions + the marked value of `OPEN` ones. The mark is the latest `ohlcv_daily.close` on or
before the session; a position with no bar on the session falls back to its last close **and says
so** in the plan's detail.

9.2 `cash_available` = equity − the value of open positions at their marks. There are no working
orders in this sleeve to reserve against (`03` §6).

9.3 **The sleeve is never sized against the account.** It is a `MY_STRATEGY` capital portfolio in
the M34 / `PORTFOLIO_REDESIGN` sense, exactly as the swing and VBT sleeves are, and it never reads
the account's total holdings. A sleeve at ₹0 plans nothing: every signal is skipped
`NO_SLEEVE_CAPITAL`.

9.4 **It never sells what it did not buy.** `tw_position` is the source of truth for what the
sleeve owns. A holding in the account that the sleeve never bought is invisible to §9.1 and can
never appear in a TWT plan line. Track C §5; TW10 proves it.

## §10 The plan (`plan.py`)

10.1 `build_entries`: signals sorted by `(−rank_key, symbol)`; for each, in order —
gate `SHUT` → `GATE_SHUT`; the instrument is already in an `OPEN` position → `ALREADY_HELD` (the
book **never averages down**); below the liquidity floor → `BELOW_LIQUIDITY_FLOOR`; no bar on the
entry session → `NO_BAR`; `open == high == low` → `LOCKED_UPPER_CIRCUIT`;
`lined ≥ max_new_entries_per_session` → `SESSION_CAP`; `open + lined ≥ max_open_positions` →
`SLOTS_FULL`; sleeve capital 0 → `NO_SLEEVE_CAPITAL`; size below the floor →
`BELOW_MIN_TRADE_VALUE`; the turnover cap alone binding is **not** a skip, it is a smaller line
with a note; `open exposure + value > equity` → `EXPOSURE_FULL`; else a `BUY_AT_OPEN` line.
**Cash spent by earlier lines is not spent twice.**

10.2 `exit_lines`: for every `OPEN` position — no `gtt_id` with `quantity_open > 0` → `ARM_GTT`;
`next_trigger > gtt_trigger` and `next_trigger_for` is the session just closed → `RAISE_GTT_STOP`.
**Nothing else.** In particular this function can emit no `SELL_AT_OPEN`, and TW10 asserts it
(`03` §7 says why the kind exists at all).

10.3 `assemble`: exits first, then entries; totals over entries; `plan_hash` is the sha256 of the
canonical lines — the same plan hashes the same.

10.4 The desk gives a plan a `plan_id` and a **30-minute expiry**; `POST /twt/execute` needs
`confirm=true`, an unexpired `plan_id` and a `line_id`; **`client_id = plan_id:symbol`** for an
order and `plan_id:symbol:kind` for a GTT leg, so a re-posted plan cannot double-send
(non-negotiable 6).

10.5 **A line's size is a preview; the confirm is the gate.** Under a row lock on the day's
`tw_session` (`SELECT … FOR UPDATE`, held from before the book is re-derived until the gateway has
answered and the rows are written) the desk re-reads the book and re-sizes a `BUY_AT_OPEN` through
§6 against the same caps and the session cap counting today's entries. A line that fits goes as
planned; one that only the exposure ceiling refuses is **shrunk to the ceiling's headroom** (never
grown past what the page showed); one the rules cannot line at all is `BLOCKED` with the skip's
code leading the reason and the row marked `REJECTED` — never sent, never left `CONFIRMED`. This is
the swing book's SW10.5 rule, restated for this surface because it is the rule that stops a stale
page becoming a wrong order.

10.6 **The GTT's cushion.** A GTT fires a LIMIT order and Maulik uses market stops. The TWT route
passes `limit_fraction = TWT_GTT_LIMIT_FRACTION` [0.97] to `place_gtt_stop`, so the resting limit
sits 3 % under its trigger and fills on the way down like a market stop would — the swing book's
PACK.8 value, for a book whose stops are three times as wide but whose exits are gaps just as
often. The weekly book keeps the gateway's own `GTT_LIMIT_FRACTION` (0.995); the keyword is
additive and defaults to it (Track C §8).

10.7 The band a TWT stop is checked against is `StopBand(min_pct=0.005, max_pct=0.30)` —
`TWT_STOP_BAND`, additive, named here because the desk's own band is 8–12 % (the weekly book's) and
the swing book's is 0.5–10 %. A 20 % stop is outside both, and a band that refuses this sleeve's
own stop would make non-negotiable 4 unsatisfiable. DECISIONS-TW **TW0.8**.

## §11 The clock

11.1 **Published sessions only.** The signal, the state and the breadth reading come from the
**last completed trading session** — the root `CLAUDE.md`'s settled rule of 9 Sep 2026. There is no
"today" bar until today ends, and this sleeve's weekly test reads a close: a partial day's close is
not a close.

11.2 The portfolio marks the desk page shows are live quotes, like every other sleeve's, and the
freshness pill says which is which. The two clocks are both correct and the table in `CLAUDE.md`
governs.

11.3 A plan built in the evening for tomorrow's open reads the session that has just closed. A plan
rebuilt in the morning reads the **same** session — it re-sizes against the sleeve's equity, it
does not re-detect. TW4's look-ahead test shifts the series by one session and asserts every gate,
every signal and every trigger moves by exactly one.

## §12 The backtest's own parameters

Named so TW2 and TW9 cannot disagree about what they ran:
`BacktestConfig.initial_capital_inr = 1 000 000`, `BacktestConfig.start = 2017-10-16`,
`BacktestConfig.is_oos_split = 2023-01-01`. The research's headline used
`EntryConfig.research_min_turnover_inr` [₹2 crore]; TW9's run from the plant's bars uses the
shipped `min_turnover_inr` [₹5 crore] and says which on the page. They are different numbers and
the page shows both (`05` §4).
