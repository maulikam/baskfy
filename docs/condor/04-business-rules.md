# 04 — Business rules: the numerical contract

Every number here is a field of `baskfy_core.condor.config` (`CondorConfig` groups them as
`calendar`, `gate`, `chain`, `structure`, `costs`, `sizing`, `exits`, `risk`; the pack's default
in brackets), and every rule is a function in `baskfy_core.condor`. The tests in
`packages/core/tests/test_condor_*.py` assert **this document**; when a module finds the document
and the code disagreeing, the document wins and the code is fixed, unless the document is wrong —
in which case the change is a DECISIONS-OC entry that edits both.

Units: `*_pct` are percent (0.75 = 0.75 %); points are index points; money is ₹ and `Decimal`;
times are IST. The pure core takes a `now` argument everywhere it needs the clock (law 1).

## §1 Calendar and event days (`calendar.py`, `CalendarConfig`)

1.1 **Expiries come from the instrument master.** `expiries_for(underlying, rows)` reads the
distinct `expiry` values of the NFO index-option rows; `monthly_expiry(underlying, year, month,
rows)` is the **last** of those inside the calendar month. No weekday arithmetic, no holiday
table of the run's own: if the exchange moved an expiry, the master already says so.

1.2 **`is_trading_day(date)`** is true iff `date` is a monthly expiry of a configured underlying
**and** `date ∉ oc_event_day`. Everything downstream — the desk process, the collector, the
plan builder — asks this one function.

1.3 **Seeded event days** (`source=SEED`): RBI monetary-policy announcement days (the six
scheduled MPC decision dates a year, seeded for the current financial year and refreshed by
hand — the pack does not scrape RBI), the Union Budget day (1 Feb, or the date announced),
election-result days when announced. `oc_event_day` is the source of truth; the seed is a
starting list, not a rule. A monthly expiry that is also an event day is **skipped**, not
shifted.

1.4 **Lot size** is read from the master for the specific expiry (`lot_size_for(underlying,
expiry, rows)`), shown on every plan, and the sizing refuses when it is missing or 0.
[`NIFTY 65`, `BANKNIFTY 30` are the values at write time and appear **only** in test fixtures.]

## §2 The gate (`gate.py`, `GateConfig`)

Inputs: the previous close `prev_close` (from `index_snapshot_daily`), the index's one-minute
bars from 09:15 to 09:59 inclusive (45 bars; the desk builds them from ticks and reconciles once
against `historical_data(interval="minute")` — the swing run's A4 pattern), and the event-day
set. Output: a `DayVerdict(trade: bool, reasons: tuple[str, ...], numbers: GateNumbers)`.

| Rule | Definition | Skip when |
|---|---|---|
| 2.1 Gap | `gap_pct = |open_0915 / prev_close − 1| × 100` | `gap_pct > gap_max_pct` [0.75] |
| 2.2 Range | `range_pct = (obs_high − obs_low) / prev_close × 100` over 09:15–09:59 | `range_pct > range_max_pct` [0.80] |
| 2.3 Containment | opening range = high/low of the 09:15–09:44 bars (30 bars) | `p_0959 > or_high` or `p_0959 < or_low`, where `p_0959` is the 09:59 bar's close |
| 2.4 Efficiency ratio | `er = |p_0959 − p_0915| / Σ_{i=1}^{44} |c_i − c_{i−1}|` over the one-minute closes; `p_0915` is the 09:15 bar's **open**, `c_0` likewise; `er = 0` when the denominator is 0 | `er > er_max` [0.30] |
| 2.5 Event day | `date ∈ oc_event_day` | always |
| 2.6 Completeness | fewer than `min_bars` [45] bars, or any bar missing a close | always, reason `INCOMPLETE_OBSERVATION` |

2.7 **Every reason is reported**, in the order above, never just the first; the page and the
journal show all of them. `trade` is true iff `reasons` is empty.

2.8 **The ER is Kaufman's**: the same bars, the same formula, no smoothing. A test asserts a
straight line of 45 rising closes gives `er = 1.0` and a perfect zig-zag gives `er ≈ 0`.

## §3 The chain (`chain.py`, `ChainConfig`)

3.1 **Which strikes are read.** From the master, the same-expiry CE and PE rows within
`snapshot_strikes` [12] strikes either side of the spot rounded to the strike step
(`strike_step` from the master's distinct strikes, [50] for NIFTY, [100] for BANKNIFTY). One
`quote()` call per side (≤ 25 symbols each) through the provider's limiter.

3.2 **`OptionQuote`** = `tradingsymbol, token, strike, option_type, bid, ask, last, bid_qty,
ask_qty, depth (5 levels each side), volume, oi, ts`. `mid = (bid + ask) / 2`; `spread_pct =
(ask − bid) / mid × 100`.

3.3 **Delta on expiry day** is Black–Scholes with `years = hours_to_1530 / (365 × 24)`,
`rate` [0.065], and the IV implied from `mid`. When the IV solver fails (deep OTM, `mid` below
`min_premium` [₹1.0]) the quote is `delta = None` and cannot be a short. The arithmetic is
ported from the frozen lab's `options.py` (`black_scholes_price`, `implied_volatility`,
`option_delta`) with a fixture asserting agreement to 1e-6 on twenty recorded quotes (PACK.1).

3.4 **Liquid** (`is_liquid`): `spread_pct ≤ max_spread_pct` [3.0] **and** `bid_qty ≥ lots ×
lot_size` at the top level or within the first `depth_levels` [3] levels **and** `oi ≥ min_oi`
[10,000 contracts]. An illiquid strike is never a leg.

## §4 The structure (`structure.py`, `StructureConfig`)

4.1 **Short call**: the liquid CE with `|delta| ∈ [delta_min, delta_max]` [0.20, 0.25] whose
strike is `> or_high + strike_buffer_points` [0]; if several, the one nearest `delta_target`
[0.22]. If none satisfies both, the plan is `REJECTED_NO_SHORT_CALL` — the range condition is
not relaxed to find a delta, nor the reverse.

4.2 **Short put**: symmetric, `|delta|` in band and strike `< or_low − strike_buffer_points`.

4.3 **Wings**: `long_call = short_call + wing_width_points`, `long_put = short_put −
wing_width_points`; `wing_width_points` per underlying [`NIFTY 150`, `BANKNIFTY 300`]. Both
wings must exist in the master and be quotable (a bid/ask, even if thin — the wing is bought,
its liquidity test is `ask_qty ≥ lots × lot_size` within `depth_levels`); otherwise
`REJECTED_NO_WING`.

4.4 **Credit**: `credit_points = (short_call.bid + short_put.bid) − (long_call.ask +
long_put.ask)` — shorts at bid, wings at ask, the conservative side. `credit_inr = credit_points
× lot_size × lots`. Required: `credit_points ≥ credit_floor_frac × wing_width_points`
[0.25; the method's range is 25–30 %, the floor is the low end and `credit_target_frac` [0.30]
is shown on the page]; else `REJECTED_CREDIT`.

4.5 **Limit prices**: each leg is sent as a LIMIT at its quoted side improved by `limit_improve_ticks`
[1 tick = ₹0.05] toward the market (a wing at `ask + 0.05`, a short at `bid − 0.05`), never at
market. A leg unfilled after `fill_wait_seconds` [20] is re-priced once by one more tick; a leg
unfilled after the second wait is **cancelled**, and §4.6 applies.

4.6 **Entry sequence**: `[LONG_PUT, LONG_CALL, SHORT_PUT, SHORT_CALL]`. A short is sent only
after **both** wings are `FILLED` for the full quantity. If a wing is `PARTIAL` or `CANCELLED`
the plan is `ABANDONED_ENTRY`: any filled wing is closed at once (a long has no obligation, so
this is a cost, not a risk), and the session is `CLOSED / NEVER_OPENED`. If a short partially
fills, the *other* short is not sent; the filled short is closed at once with its wing left to
close after it; the session closes `NEVER_OPENED`. There is no state in which the book is short
more than it is long.

4.7 **Exit sequence**: `[SHORT_CALL, SHORT_PUT, LONG_CALL, LONG_PUT]` — shorts at `ask + 0.05`,
wings at `bid − 0.05`; the same wait-and-reprice, but the **third** attempt on a short is at
market (`exit_final_at_market` [true]) because being flat by 14:30 outranks the slippage.

4.8 **C is measured from fills**, not from the plan: `entry_credit_points = Σ short fills −
Σ wing fills` per lot. The exits in §7 use this C.

## §5 Costs (`costs.py`, `CostRates`)

5.1 Ported from the frozen lab's `options_costs.py` with the rates as **fields**, not literals:
brokerage per order [₹20, capped per Zerodha's flat rate], STT on the sell side of an option
premium [0.1 %], NSE transaction charge on premium [0.0353 %], SEBI turnover fee [₹10 per crore],
stamp duty on the buy side [0.003 %], GST [18 %] on brokerage + transaction + SEBI, clearing
[0]. Every rate is dated in the docstring; OC5 verifies each against the broker's published
schedule and records the source.

5.2 `charges(plan)` = the statutory charges and brokerage on eight orders (four in, four out)
at the plan's quantities and prices. `spread_cost(plan)` = `Σ_legs (ask − bid) / 2 × quantity`
over the eight fills — the half-spread each crossing is expected to pay beyond the conservative
side the credit was already computed at (`04` §4.4). `expected_round_trip(plan) = charges +
spread_cost`. Separately, `reserve_per_lot_inr` [₹1,000] is the method's flat allowance for
"slippage, fees and contingencies" that the **sizing** (§6.2) subtracts; a test asserts the
fixture plan's `expected_round_trip / lots ≤ reserve_per_lot_inr`, and the page shows both
numbers so a day on which the estimate exceeds the reserve is visible before the confirm
(`RESERVE_EXCEEDED` is a warning on the plan, not a rejection — the cost test in 5.3 is the
rejection).

5.3 **Cost test**: `cost_share = expected_round_trip / profit_target_inr` where
`profit_target_inr = (1 − profit_take_frac) × credit_inr` (§7.1). Required: `cost_share ≤
cost_share_max` [0.20]; else `REJECTED_COST`.

5.4 **STT on exercise** does not apply to this book (nothing is held to 15:30), but
`costs.exercise_stt` is ported and a test asserts the exit engine's hard exit makes it
unreachable — so the day somebody relaxes `hard_exit_time` the cost appears in the journal
rather than on the contract note.

## §6 Sizing (`sizing.py`, `SizingConfig`)

6.1 `max_loss_per_lot_inr = (wing_width_points − credit_points) × lot_size`.

6.2 `lots = floor( risk_budget_inr / (max_loss_per_lot_inr + reserve_per_lot_inr [1000]) )`,
then `min(lots, oc_config.max_lots, BASKFY_CONDOR_MAX_LOTS_MAX)`. `lots = 0` is
`REJECTED_BUDGET` with the arithmetic in the message.

6.3 **Margin is a ceiling, not a source.** The plan carries `margin_required_inr` from the
broker's basket estimate for the four legs at the plan's lots; required
`margin_required_inr ≤ oc_config.margin_pool_inr`, else `REJECTED_MARGIN`. The estimate is
taken with the wings included (the hedged margin) **and** the plan also shows the naked-short
figure the broker would ask between the wing fills and the short fills — it is never larger than
the pool either, or the plan is `REJECTED_MARGIN_TRANSIENT`. The expiry-day ELM of 2 % is
whatever the broker's estimate contains; the code does not model it separately.

6.4 **First-live multiplier**: while `oc_journal` holds fewer than `first_live_sessions` [5]
rows with `simulated=false`, `risk_budget_inr` is multiplied by `first_live_risk_multiplier`
[0.5] at plan time and the journal row is tagged `half_size`. The method's own first step
(₹10,000–₹15,000, one or two lots) is Maulik's `risk_budget_inr` setting; this multiplier sits
on top of it (the swing run's A9, for the same reason).

## §7 Exits (`exits.py`, `ExitConfig`)

Every tick after `OPEN`, with C from §4.8 and `D = (short_call.ask + short_put.ask) −
(long_call.bid + long_put.bid)` per lot — the cost to close **now**, conservative side:

| Rule | Condition | Code |
|---|---|---|
| 7.1 Profit | `D ≤ profit_take_frac × C` [0.50] | `PROFIT` |
| 7.2 Stop | `D ≥ stop_frac × C` [1.50] | `STOP` |
| 7.3 Strike touch | spot `≥ short_call.strike` or `≤ short_put.strike` (the index last-traded price, any single print) | `STRIKE_TOUCH` |
| 7.4 Hard exit | `now ≥ hard_exit_time` [14:30] | `HARD_EXIT` |
| 7.5 Manual | the operator's Close button | `MANUAL` |

7.6 Precedence when several fire on one tick: `HARD_EXIT > STRIKE_TOUCH > STOP > PROFIT`. The
first decision closes the session's exit logic — a second `ExitDecision` for the same session is
a bug (test).

7.7 **Stale marks**: if either short's quote is older than `stale_quote_seconds` [15] the engine
does not evaluate 7.1 (never take profit on a stale number) but **does** evaluate 7.2–7.4 on the
last known D; if the desk has had no index tick for `stale_index_seconds` [30] after 14:00 the
engine raises `HARD_EXIT` early with reason `FEED_LOST`.

7.8 **No rolling, no averaging, no conversion**: there is no function in `baskfy_core.condor`
that produces a plan from an open position other than the exit plan; a property test asserts
`ExitPlan.legs` closes exactly the position's four legs and nothing else.

## §8 The risk ledger (`risk.py`, `RiskConfig`)

8.1 **Per expiry**: `max_loss_inr` on the plan ≤ `risk_budget_inr` (after §6.4) — enforced by
the sizing; the engine additionally raises `STOP` if the marked loss exceeds
`risk_budget_inr × budget_breach_frac` [1.0] regardless of D/C (a gap through both strikes on
a thin print).

8.2 **Daily emergency**: today's realised + marked net P&L ≤ `−daily_loss_limit_inr` → close
now, `paused_until = today`, reason `DAILY_LIMIT`. With one trade a day this is the same event
as 8.1 unless costs and slippage make it worse; it exists for the day the fills are ugly.

8.3 **Monthly pause**: the sum of `net_pnl_inr` over `oc_journal` rows in the calendar month ≤
`−monthly_pause_inr` → `paused_until = last day of month`, reason `MONTHLY_PAUSE`. Evaluated at
every close and at 09:00 on every trading day.

8.4 **Absolute ceiling**: `risk_budget_inr ≤ min(BASKFY_CONDOR_RISK_PER_EXPIRY_INR_MAX,
account_inr × BASKFY_CONDOR_RISK_PCT_MAX / 100)` — a 422 on the settings form that names both
numbers.

8.5 **A pause is a refusal, not a warning.** `plan.build` returns `REJECTED_PAUSED` while
`today ≤ paused_until`; the desk process still observes and journals the verdict so the
paper record is unbroken.

## §9 The session state machine (`session.py`)

```
OBSERVING ──(09:59 verdict SKIP)──► SKIPPED
OBSERVING ──(09:59 verdict TRADE, plan built)──► PLANNED
OBSERVING ──(plan REJECTED_*)──► SKIPPED              (reason = the code)
PLANNED ──(no confirm by min(expires_at, 10:15))──► LAPSED
PLANNED ──(confirm)──► CONFIRMED
CONFIRMED ──(all four FILLED)──► OPEN
CONFIRMED ──(ABANDONED_ENTRY)──► CLOSED / NEVER_OPENED
OPEN ──(ExitDecision, all four closed)──► CLOSED / <code>
```

No other edge exists; `transition()` raises on any other pair, and a property test walks random
edge sequences. One session per underlying per date (unique index).

## §10 The journal in R (`journal.py`)

`r_multiple = net_pnl_inr / risk_budget_in_force`; `summarize(rows)` returns count, traded,
skipped by reason, win rate, mean R, expectancy (₹ and R), worst R, max drawdown (₹, on the
running sum), `max_d_over_c` distribution, mean minutes held, and the same split by
`closed_reason` and by `simulated`. Real and simulated rows are **never** pooled in one number.

## §11 The backtest (`backtest.py`) — see `07` for what each tier may claim

11.1 **Tier 1 — the gate on real bars.** Over every monthly expiry from `date_from` [2015-01-01],
the gate of §2 on the index's real one-minute bars → the traded/skipped funnel by reason, by
year. No P&L. Answers: *how often does this book trade, and which filter does the work?*

11.2 **Tier 2 — synthetic P&L, labelled a model.** For each Tier-1 `TRADE` day, strikes from
§4 with delta from Black–Scholes at an IV taken from **India VIX's close of the previous day**
(`index_snapshot_daily` for the VIX index, backfilled by OC9), premiums from the same model,
the minute path of the index from the real bars, exits of §7 applied to model prices, costs of
§5, sizing of §6. Reported with the caveat verbatim: *"Prices are modelled, not observed. Real
expiry-day premiums, skew and slippage differ from a flat-VIX Black–Scholes; treat the P&L as
a shape, not a number."*

11.3 **Tier 3 — observed prices.** The same engine over `oc_chain_snapshot` rows (the forward
dataset from the collector) and, if Maulik buys it, a vendor's minute-level option history
loaded into the same table with `source = VENDOR`. Only this tier may print a number the
real-money gate (`02` §3.3) counts as evidence of profitability; and only after the number of
observed expiries is ≥ `tier3_min_expiries` [12] does the page drop its "insufficient sample"
banner.

11.4 **Both underlyings separately, always.** A backtest row has one `underlying`; there is no
pooled report.
