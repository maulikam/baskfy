# 04 — Business rules: the numerical contract

Every number here is a field of `baskfy_core.options.config` (`OptionsConfig`, groups `calendar`,
`chain`, `condor_monthly`, `condor_weekly`, `directional`, `expiry_setups`, `costs`, `sizing`,
`execution`, `risk`; the default in brackets), and every rule is a function in
`baskfy_core.options`. Tests in `packages/core/tests/test_options_*.py` assert **this document**.
When code and document disagree the document wins, unless the document is wrong — then a
`DECISIONS-OP` entry edits both. **Every default is ⚠ UNREVIEWED** (the reasoning and rejected alternatives: PACK.5, PACK.7, PACK.10, PACK.13).

Units: `*_pct` are percent (0.75 = 0.75 %); points are index points; money is ₹ and `Decimal`; times
IST. The pure core takes `now` as an argument (law 1). "Bar" means a one-minute bar unless it says
5-minute; 5-minute bars are aligned to 09:15 (09:15–09:19, 09:20–09:24, …) and built from the
one-minute bars, never fetched separately. A 5-minute bar's **close time** is the start of its last minute (the
10:15–10:19 bar closes at 10:19) and it is complete once that minute is stored (OP4.2).

## §1 Calendar and event days (`calendar.py`)

1.1 **Expiries come from the master.** `expiries(rows)` = distinct `expiry` of NIFTY CE/PE rows in
`op_contract`; `monthly_expiry(year, month)` = the **last** of them in the calendar month;
`kind(expiry)` = `MONTHLY` if it is that, else `WEEKLY`. No weekday arithmetic anywhere; a test with a
fixture master whose Tuesday moved to Monday (holiday) must move the date, and a weekday-rule
implementation must fail it.

1.2 **Day roles.** For a date `d` that is an NSE trading day (`trading_day`):
`is_event_day(d)` ⇔ `d ∈ op_event_day`. `role(d, sleeve)`:

| Sleeve | Trades on `d` iff |
|---|---|
| O1-M | `d` is a `MONTHLY` expiry ∧ ¬event |
| O1-W | `d` is a `WEEKLY` expiry (so not the month's last) ∧ ¬event — PACK.7: expiry day only |
| O2 | ¬event (any trading day) |
| O3 | `d` is any expiry (weekly or monthly) ∧ ¬event |

Everything downstream — scans, collector, plan builder, desk process — asks `role()`.

1.3 **Event days** seeded (`source=SEED`) from the RBI's published MPC schedule for the financial
year and the Union Budget date, each with its source URL in the seed file's comment; election-result
days when announced. If a date cannot be verified from its primary source, it is not seeded and
QUESTIONS Q4 says so. An expiry that is an event day is skipped, never shifted.

1.4 **Lot size and tick size** are read per contract from `op_contract`, shown on every plan; the
sizing refuses when missing or 0 (`REJECTED_NO_LOT_SIZE`). Literal lot sizes appear only in test
fixtures.

1.5 **`expiry_for_o2(d)`** = the smallest expiry `> d` (strictly): the nearest weekly that does not
expire today. **`expiry_for_o1_o3(d)`** = `d` itself.

## §2 Chain, greeks, liquidity (`chain.py`, `greeks.py`)

2.1 **Which contracts are read.** `atm(spot, step)` = the strike nearest `spot` (ties → the lower
strike); `step` = the modal difference between adjacent listed strikes near ATM for that expiry
[50 for NIFTY at write time; read, not assumed]. The collector and scans read `snapshot_strikes`
[15] strikes either side of ATM, CE and PE, for the two nearest expiries — one `quote()` call.

2.2 **The forward.** Per expiry per minute, `F = K_atm + (C_mid − P_mid) × e^{rT}` from the ATM pair
(put–call parity), falling back to the next-nearest pair if either ATM mid is missing; `rate`
[0.065]. The forward absorbs dividends and the rate, which the index spot does not.

2.3 **Time and IV.** `T = minutes from now to 15:30 on expiry ÷ (365 × 24 × 60)` — calendar time,
consistent for 0-DTE and 7-DTE (PACK.8). IV = the Black-76 volatility that reprices `mid`, by a
bracketed solver on [0.01, 5.0] to 1e-6; **refused** (`iv = None`, no greeks) when `mid < min_premium`
[₹0.50], when `mid` is below intrinsic on `F` plus one tick, or when the solver does not converge.
Delta, gamma, theta (per calendar day) and vega (per vol point) are Black-76 on `F`. A contract
with `iv = None` can never be a leg whose selection depends on delta.

2.4 **Liquid** (`is_liquid(q, qty)`): `spread_pct = (ask − bid) / mid × 100 ≤ max_spread_pct` [3.0]
**and** the side the order takes has `≥ qty` within `depth_levels` [3] levels **and**
`oi ≥ min_oi_lots` [200] × lot size (Kite's OI unit is recorded by OP0 and the conversion follows
it). An illiquid contract is never a leg, except that an O1 **wing** needs only the depth test
(condor `04` §4.3).

2.5 **Stale.** A quote older than `stale_quote_seconds` [15] is stale; the index is stale after
`stale_index_seconds` [30] without a tick. A scan computed from a snapshot older than 2 minutes is
`stale=true` and the web tab says so.

## §3 O1 — hedged premium selling (`condor.py`)

3.1 **Authoritative text: `docs/condor/04` §2 (gate), §3.2–§3.4 (quote, liquidity), §4 (structure,
limit prices, entry/exit sequences, C from fills), §7 (exits), §9 (state machine)** — ported into
`options.condor` with every field in two config groups, `condor_monthly` (O1-M) and `condor_weekly`
(O1-W). Where condor `04` §3.3 computes delta with BS on spot, this pack's §2.3 replaces it.

3.2 **Defaults per variant.**

| Field | O1-M | O1-W | Source |
|---|---|---|---|
| `gap_max_pct` / `range_max_pct` / `er_max` | 0.75 / 0.80 / 0.30 | 0.75 / 0.80 / 0.30 | condor §2 |
| observation / opening range | 09:15–09:59 / 09:15–09:44 | same | condor §2 |
| `delta_min` / `delta_max` / `delta_target` | 0.20 / 0.25 / 0.22 | same | condor §4.1 |
| `wing_width_points` | 150 | 150 | condor §4.3 |
| `credit_floor_frac` | 0.25 | 0.25 | condor §4.4 |
| `profit_take_frac` / `stop_frac` | 0.50 / 1.50 | same | condor §7 |
| entry window | plan at 10:00, lapse 10:15 | same | condor §9 |
| `hard_exit_time` | 14:30 | 14:30 | condor §7.4 |
| `cost_share_max` | 0.20 | 0.20 | condor §5.3 |
| `risk_per_trade_pct` / `max_lots` | 1.0 / 3 | 0.5 / 2 | this pack, `01` §2 |
| `reserve_per_lot_inr` | 1000 | 1000 | condor §5.2 |

The two groups start equal except risk; they are separate objects so a Tier-3 finding can move one
without the other. A test asserts they are distinct instances.

3.3 **Slot.** O1 needs today's expiry-day slot (§8.6); if O3 holds it at 10:00 the plan is
`REJECTED_SLOT_TAKEN`.

## §4 O2 — directional buying (`directional.py`)

4.1 **Day filters** (evaluated at 09:30; every reason reported):

| Rule | Definition | Skip when |
|---|---|---|
| Trend | `ema20` = 20-day EMA of NIFTY 50 closes from `index_snapshot_daily` through the previous session; `trend = UP` if `prev_close > ema20`, `DOWN` if `<` | equal → `TREND_FLAT` |
| Gap | `|open_0915 / prev_close − 1| × 100` | `> gap_max_pct` [1.0] → `GAP_TOO_BIG` |
| Range | opening range = high/low of the 09:15–09:29 bars (15); `or_pct = (or_high − or_low) / prev_close × 100` | `> or_max_pct` [0.90] → `RANGE_TOO_WIDE` |
| VIX | India VIX previous close | `> vix_max` [22] → `VIX_TOO_HIGH` |
| Event | §1.2 | always |
| Completeness | fewer than 15 opening-range bars | `INCOMPLETE_OBSERVATION` |

4.2 **Trigger.** In the entry window [09:30, 13:30] (the trigger bar's close time), the first
completed 5-minute bar whose close is `> or_high × (1 + buffer_pct/100)` when `trend = UP`, or
`< or_low × (1 − buffer_pct/100)` when `trend = DOWN` [`buffer_pct` 0.05]. Counter-trend breaks are
recorded in `op_scan.numbers` and never traded. **One trigger per day**; after it, the scan is
`TRIGGERED` whatever happens to the plan.

4.3 **Contract.** Expiry `expiry_for_o2(d)`. Strike: `trend = UP` → CE at `atm − itm_steps × step`;
`DOWN` → PE at `atm + itm_steps × step` [`itm_steps` 1]. Required: `|delta| ∈ [0.50, 0.75]`
(`REJECTED_DELTA`), liquid for the quantity (`REJECTED_ILLIQUID`).

4.4 **Entry.** One LIMIT buy at `ask + 1 tick`; the execution rules of §8 apply; a buy not filled
after the second wait is cancelled and the day is `MISSED` — no chasing.

4.5 **Exits** (mark = the option's **bid**; `E` = the entry fill):

| Rule | Condition | Code |
|---|---|---|
| Stop | `bid ≤ E × (1 − stop_frac)` [0.30] | `STOP` |
| Invalidation | a completed 5-minute index close back inside the opening range (`< or_high` for a call, `> or_low` for a put) | `INVALIDATED` |
| Target | `bid ≥ E × (1 + target_frac)` [0.60] | `TARGET` |
| Time stop | at `entry + time_stop_minutes` [45], `bid < E × (1 + time_stop_min_gain)` [0.10] | `TIME_STOP` |
| Hard exit | `now ≥ hard_exit_time` [15:00] | `HARD_EXIT` |
| Manual | the Close button | `MANUAL` |

Precedence on one tick: `HARD_EXIT > STOP > INVALIDATED > TARGET > TIME_STOP`. One decision per
session (test).

4.6 **Risk per lot** = `E_planned × stop_frac × lot_size + reserve_per_lot_inr` [300], where
`E_planned = ask + 1 tick`. The plan also shows the **gap-through** worst case, `E × lot_size`.
Additionally `lots × E × lot_size ≤ premium_cap_pct` [10] % of sleeve capital (`REJECTED_PREMIUM_CAP`)
when capital > 0.

**As built (OP7, `baskfy_core.options.plan_o2`).** The plan builder walks §4.1 → §4.2 → §4.3 → §4.6
over the **trigger minute's** snapshot, so a plan equals the scan's candidate (§10, tested). Its
answers beyond this section's codes: `NO_TRIGGER` (the window closed with no with-trend break — a
*skipped session*, so the paper period counts the day), `NO_TRIGGER_YET`, `BEFORE_ENTRY_WINDOW`,
`RANGE_NOT_SETTLED`, `NO_DECISION_SNAPSHOT`, `NO_EXPIRY` (all "ask again next minute", nothing
written) and `ENTRY_WINDOW_CLOSED` — a trigger at or after 13:30 is never planned, because
`expires_at = min(issued + 30 min, 13:30)` could not then outlive its issue (`DECISIONS-OP` OP7.2,
OP7.5). O2 asks the broker's margin calculator nothing: §7.4 is O1 and O3 (OP7.3). At the ceilings
of `02`, §4.6's premium cap and §6.4's cost test cannot bind for this sleeve — OP7.6 has the
arithmetic, and it is a finding, not a change.

## §5 O3 — expiry-day setups (`expiry_setups.py`)

Both setups trade the **expiring** contract (`expiry_for_o1_o3`), structure `DEBIT_SPREAD`:
long `K_long = atm(spot)`, short `K_short = K_long + width` (call spread, bullish) or
`K_long − width` (put spread, bearish) [`width_points` 100]. `debit = long.ask − short.bid`
(conservative side). Required: `debit ≤ max_debit_frac × width` [0.55] (`REJECTED_DEBIT`), both legs
liquid for the quantity, the day's slot free (§8.6).

5.1 **O3-A — range break.** Morning range = high/low of 09:15–10:14 (60 bars); required
`(high − low) / prev_close × 100 ≤ range_max_pct` [1.20] (else `RANGE_TOO_WIDE`). Trigger: the first
completed 5-minute bar closing in [10:19, 13:00] whose close is beyond the range by `buffer_pct`
[0.05] **and** `er ≥ er_min` [0.40], where `er` is Kaufman's ER (condor `04` §2.4's formula) over the
one-minute closes from 09:15 to the trigger bar's close. Direction = the side broken.

5.2 **O3-B — gap hold.** `gap = open_0915 − prev_close`; required `gap_min_pct ≤ |gap| / prev_close ×
100 ≤ gap_max_pct` [0.50, 1.50]. Hold: from 09:15 to the 09:44 bar inclusive, no bar trades through
`half_gap = prev_close + gap / 2` (no low ≤ `half_gap` for an up-gap; no high ≥ it for a down-gap).
Plan at 09:45; direction = the gap's. Entry window closes 10:00.

5.3 **Exits** (`V` = `long.bid − short.ask`, the conservative close value; `D` = the entry debit from
fills):

| Rule | Condition | Code |
|---|---|---|
| Target | `V ≥ target_frac_of_width × width` [0.80] | `TARGET` |
| Stop | `V ≤ stop_frac_of_debit × D` [0.50] | `STOP` |
| Invalidation | O3-A: a 5-minute close back inside the morning range; O3-B: any bar through `half_gap` | `INVALIDATED` |
| Hard exit | `now ≥ hard_exit_time` [14:45] | `HARD_EXIT` |
| Manual | Close button | `MANUAL` |

Precedence: `HARD_EXIT > STOP > INVALIDATED > TARGET`.

5.4 **Sequences.** Entry `[LONG, SHORT]`: the short is sent only after the long is fully filled; a
partial/cancelled long abandons the entry and closes what filled. Exit `[SHORT, LONG]`: the long is
sold only after the short is fully bought back. There is no state with short qty > long qty (the
condor's never-naked property, same test harness).

5.5 **Risk per lot** = `debit × lot_size + reserve_per_lot_inr` [500]. One O3 trade per day across
both setups; if both would fire, O3-B (earlier) holds.

## §6 Costs (`costs.py`, `CostRates`) — every rate a dated field, verified by OP0/OP6

| Rate | Default | Applies to |
|---|---|---|
| `brokerage_per_order_inr` | 20 | every executed order (Zerodha flat F&O) |
| `stt_sell_premium_pct` | **0.15** (from 1 Apr 2026; verified OP0) | sell side, on premium × qty |
| `stt_exercise_intrinsic_pct` | **0.15** (from 1 Apr 2026; verified OP0) | an ITM long held to expiry, on settlement intrinsic × qty |
| `exchange_txn_pct` | **0.03553** (NSE, from 1 Mar 2026; verified OP0) | premium turnover, both sides |
| `sebi_per_crore_inr` | 10 | turnover |
| `ipft_per_crore_inr` | **0.01** (from 1 Mar 2026; verified OP0) | premium turnover (NSE investor-protection fund) |
| `stamp_buy_pct` | 0.003 | buy side, premium |
| `gst_pct` | 18 | on brokerage + exchange txn + SEBI (+ IPFT) |
| `auto_squareoff_inr` | 50 (+GST) per order (verified OP0) | charged if the broker squares off an MIS position (Zerodha F&O auto square-off 15:26); unreachable behind every hard exit ≤ 15:00 |

**Verified by OP0 (22 Sep 2026) — sources in `DECISIONS-OP.md` OP0.1.** The pack's author was right:
the Finance Bill 2026 (Memorandum, "Increase in tax rates of Securities Transaction Tax", Clause 143)
raised STT on the sale of an option from 0.1 % to **0.15 % of premium** and on an exercised option from
0.125 % to **0.15 % of intrinsic**, for transactions on or after **1 Apr 2026**; Zerodha's charges page
shows the same. NSE circular NSE/FA/73061 (27 Feb 2026) rolled the IPFT contribution back and raised
the transaction charge by the same amount from 1 Mar 2026 — Zerodha now shows NSE options at
**0.03553 %** of premium and IPFT at **₹0.01 per crore** (+GST). The condor pack's 0.1 % / 0.125 % /
0.03503 % are the stale half. `CostRates`' docstring carries each URL and the date read;
`OPTIONS_COST_RATES_REVIEWED_ON` warns after 90 days (condor OC5/OC11).

6.1 `charges(fills)` = the statutory sum and brokerage over the actual (or planned) orders:
O1 eight orders, O2 two, O3 four.

6.2 **Slippage.** Plans price at the conservative side (buy at ask, sell at bid) plus
`limit_improve_ticks` [1]. Paper fills walk the depth ladder (§8.4). Tier 2 (model prices, `07`)
adds `synthetic_half_spread_pct` [1.5] % of premium, minimum ₹0.10, per crossing.

6.3 **The expiry-day STT trap** (`exercise_stt`): a long ITM option not closed by 15:30 on expiry
pays `stt_exercise_intrinsic_pct` on `intrinsic × qty` — for a deep-ITM long this can exceed the
trade's whole profit. It applies to O3's long leg and to an O1 wing that ends ITM. Every hard exit
makes it unreachable; a test asserts that, and a second test asserts that if `hard_exit_time` were
set past 15:30 the journal would carry the charge.

6.4 **Cost test**: `cost_share = expected_round_trip / expected_gain_inr` with expected gain =
O1 `(1 − profit_take_frac) × credit_inr`; O2 `target_frac × E × qty`; O3
`(target_frac_of_width × width − debit) × qty`. Required `cost_share ≤ cost_share_max`
[O1 0.20, O2 0.15, O3 0.20], else `REJECTED_COST`.

## §7 Sizing (`sizing.py`)

7.1 `risk_budget_inr = min(sleeve_capital_inr × risk_per_trade_pct / 100,
BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX)`.

7.2 `lots = floor(risk_budget_inr / risk_per_lot_inr)`, then `min(lots, max_lots,
BASKFY_OPTIONS_MAX_LOTS_MAX)`; `0` → `REJECTED_BUDGET` with the arithmetic in the message.

7.3 **Capital ₹0.** In `PAPER` mode, `lots = 1`, `sizing_mode = PAPER_ONE_LOT`, and `R` for the
journal = `risk_per_lot_inr` (PACK.6). In `LIVE` mode it is `REJECTED_NO_SLEEVE_CAPITAL`.

7.4 **Margin is a ceiling.** O1 and O3 take the broker's basket-margin estimate for the plan's legs
at the plan's lots (hedged, and for O1 also the transient figure between wing and short fills —
condor §6.3); `REJECTED_MARGIN` if above `margin_pool_inr − margin in use by open op_ positions`. With
`margin_pool_inr = 0` in `PAPER` mode the figure is shown and the check is a warning
`MARGIN_POOL_UNSET`; in `LIVE` mode it is a rejection.

7.5 **First-live multiplier**: while a sleeve has fewer than `first_live_trades` [5] journal rows with
`simulated=false`, its `risk_budget_inr` × `first_live_risk_multiplier` [0.5], tagged `half_size`.

## §8 Execution mechanics, shared (`execution.py`; the desk sends, the core decides)

8.1 Every order is `product=MIS`, `exchange=NFO`, `order_type=LIMIT`, through
`OrderGateway.place` with `client_id` minted by `packages/execution` (`plan_id:symbol`, exits
`plan_id:symbol:CLOSE`). Never `MARKET` on entry.

8.2 **Limit, wait, reprice, cancel**: entry at the quoted side + `limit_improve_ticks` [1];
unfilled after `fill_wait_seconds` [20] → repriced once by one more tick; unfilled after the second
wait → cancelled. Exits the same, except the **third** attempt on a closing leg that reduces risk
(a short buy-back, or O2's sell) is marketable at the touch (`exit_final_marketable` [true]):
flat by the hard exit outranks slippage.

8.3 **Sequences**: O1 condor `04` §4.6–4.7; O3 §5.4; O2 single leg. A protecting leg is never
closed while the leg it protects is open.

8.4 **Paper fills** walk the snapshot's depth ladder from the side taken, level by level, up to the
limit price, adding `latency_ticks` [1] of adverse price per level consumed beyond the first; what
the ladder cannot fill at the limit is a partial (`sim_method=DEPTH_LADDER`). Ported from the frozen
lab's `fills_paper.simulate_fill` by re-implementation (PACK.2).

8.5 **Stale marks**: profit-taking / target rules are not evaluated on a stale quote; stops,
invalidations and hard exits are, on the last known mark. No index tick for `stale_index_seconds`
after 14:00 (O1, O3) or at any time with a position open for more than 60 s (O2) → `HARD_EXIT`
with reason `FEED_LOST`.

8.6 **The expiry-day slot**: one O1-or-O3 position per expiry day; the first plan **confirmed** holds
it (`op_session.slot_holder`); a lapsed or rejected plan frees it. O2 is outside the slot.

8.7 **The confirm covers the exits** (PACK.2): the rule-driven closes of §3–§5 and every hard exit
fire under the entry's confirm; the Confirm button says so verbatim (`05` §3).

## §9 The risk ledger (`risk.py`)

9.1 **Per sleeve, in R** (R = the risk budget in force, or `risk_per_lot_inr` in paper-one-lot):
today's realised + marked net ≤ `−daily_loss_r` [2] → close the sleeve's position, sleeve paused for
the day; the ISO week's realised ≤ `−weekly_loss_r` [4] → paused to the week's last trading day; the
calendar month's ≤ `−monthly_loss_r` [8] → paused to month-end. Reason `DAILY_R` / `WEEKLY_R` /
`MONTHLY_R`.

9.2 **Per trade**: a marked loss beyond `risk_budget × budget_breach_frac` [1.0] closes the position
(`STOP`) whatever the sleeve's own rule says (a gap through a thin print).

9.3 **Book, in ₹**, once any sleeve capital > 0: `daily_loss_limit_inr` (0 = derived as 1.5 % of Σ
sleeve capital) and `monthly_pause_inr` (0 = derived as 5 %), each capped by its env ceiling. A
daily breach closes every open `op_position` and pauses the book for the day; a monthly breach pauses
it to month-end.

9.4 **A pause is a refusal.** `plan.build` returns `REJECTED_PAUSED`; scans keep running and show
`PAUSED`; sessions still journal their verdicts.

## §10 Scan states (`scan.py`) — what the web tab shows each minute

| Sleeve | States (one-way within a day) |
|---|---|
| O1-M / O1-W | `NOT_TODAY` (with next date) → `OBSERVING` (live gate numbers vs thresholds) → `WOULD_TRADE` (candidate condor priced) \| `WOULD_SKIP` (all reasons) → `SLOT_TAKEN` \| `PLANNED` → `DONE`; `PAUSED` overrides |
| O2 | `NOT_TODAY` (event) → `BUILDING_RANGE` → `DAY_SKIPPED` (reasons) \| `ARMED` (direction, trigger level, distance in points and %, candidate contract priced) → `TRIGGERED` \| `WINDOW_CLOSED`; `PAUSED` |
| O3 | `NOT_TODAY` → `BUILDING_RANGE` (O3-B's gap watch and O3-A's range shown side by side) → `ARMED` (each setup's trigger levels, both-direction candidate spreads priced) → `TRIGGERED` \| `SLOT_TAKEN` \| `WINDOW_CLOSED`; `PAUSED` |

A scan's candidate uses **exactly** the functions the plan builder uses (§3–§7) over the minute's
snapshot; a test asserts that a plan built from the same inputs equals the scan's candidate.

**As built (OP4, `baskfy_core.options.scan`).** O3-A and O3-B are separate rows (their `op_sleeve`
codes), and a setup whose conditions fail is `DAY_SKIPPED` with its reasons, as O2's is (OP4.9). A
decided sleeve prices its candidate from the snapshot of its decision minute — O1 `plan_time`, O3-B
`o3b_plan_time`, O2/O3-A the trigger minute — so a verdict does not move with later premiums
(OP4.4). A window is judged once its last minute is stored, or `stale_scan_seconds` after (OP4.3).
Refusal codes beyond §3–§7: `REJECTED_NO_SHORT_PUT`, `REJECTED_NO_CONTRACT`, `REJECTED_NO_CHAIN`;
skip reasons beyond the tables: `TREND_UNKNOWN`, `VIX_UNKNOWN`, `NO_PREV_CLOSE`, `O3B_HOLDS`,
`SETUP_DISABLED` (OP4.6, OP4.8, OP4.9).

## §11 Session state machine (`session.py`)

Condor `04` §9's machine, per sleeve: `OBSERVING → SKIPPED | PLANNED`; `PLANNED → LAPSED |
CONFIRMED`; `CONFIRMED → OPEN | CLOSED/NEVER_OPENED`; `OPEN → CLOSED/<code>`. No other edge;
`transition()` raises; property-tested. One session per sleeve per date.

## §12 Journal in R (`journal.py`)

`r_multiple = net_pnl_inr / R`. `summarize(rows)` per sleeve: count, traded, skipped by reason, win
rate, mean R, expectancy (₹, R), worst R, max drawdown (R and ₹), MAE/MFE distribution, mean minutes
held, by `closed_reason`, by expiry kind (O1, O3), by weekday (O2). Sleeves, `simulated`, and
`sizing_mode` are never pooled in one number.

## §13 Backtest (`backtest.py`) — `07` says what each tier may claim

13.1 **Tier 1** per sleeve over `op_index_minute` 2015→ (or Kite's earliest): the day filters and
triggers only → funnel by reason and year, trigger-time distribution, and for O2/O3 the index's
move after the trigger (MFE/MAE in points to the hard exit). No option P&L.

13.2 **Tier 2** (modelled, labelled): Black-76 at the previous day's India VIX close as flat IV,
the forward = spot, §6.2's synthetic spread, §4–§5's exits on model prices along the real minute
path. The caveat of condor `07` §4, verbatim, plus for O2: *"A bought option's model price ignores
intraday IV changes; real premiums often fall after the open and on a move's reversal. Expect Tier 2
to flatter O2."*

13.3 **Tier 3** over `op_chain_snapshot` (collector, or `VENDOR`), fills by §8.4, costs by §6. Sample
banners until `tier3_min_sessions` [O1-M 12, O1-W 20, O2 60, O3 20] observed sessions.

13.4 One sleeve per backtest row; never pooled.

## §14 Every field and its default (`config.py`) — machine-checked

Regenerated from `baskfy_core.options.config.OptionsConfig` and asserted both ways by
`packages/core/tests/test_options_docs_parity.py` (OP1.2): a field added, renamed or re-valued
without editing this table is red, and a row here with no field behind it is red. Times are IST
`HH:MM`; `*_pct` percent, `*_frac` fraction, `*_inr` rupees. The sections above are the rules;
this table is the inventory. The env ceilings of `02` are not fields (they are
`OptionsCeilings`, read from system-only env by OP2).

| Field | Default |
|---|---|
| `calendar.underlying` | `NIFTY` |
| `calendar.allowed_underlyings` | `NIFTY` |
| `calendar.market_open` | `09:15` |
| `calendar.market_close` | `15:30` |
| `chain.snapshot_strikes` | `15` |
| `chain.rate` | `0.065` |
| `chain.iv_lower` | `0.01` |
| `chain.iv_upper` | `5.0` |
| `chain.iv_tolerance` | `1e-06` |
| `chain.min_premium` | `0.50` |
| `chain.max_spread_pct` | `3.0` |
| `chain.depth_levels` | `3` |
| `chain.min_oi_lots` | `200` |
| `chain.oi_unit` | `UNITS` |
| `chain.stale_quote_seconds` | `15` |
| `chain.stale_index_seconds` | `30` |
| `chain.stale_scan_seconds` | `120` |
| `chain.greeks_model` | `black76-parity-v1` |
| `condor_monthly.variant` | `MONTHLY` |
| `condor_monthly.gap_max_pct` | `0.75` |
| `condor_monthly.range_max_pct` | `0.80` |
| `condor_monthly.er_max` | `0.30` |
| `condor_monthly.observation_start` | `09:15` |
| `condor_monthly.observation_end` | `09:59` |
| `condor_monthly.opening_range_end` | `09:44` |
| `condor_monthly.min_bars` | `45` |
| `condor_monthly.delta_min` | `0.20` |
| `condor_monthly.delta_max` | `0.25` |
| `condor_monthly.delta_target` | `0.22` |
| `condor_monthly.strike_buffer_points` | `0` |
| `condor_monthly.wing_width_points` | `150` |
| `condor_monthly.credit_floor_frac` | `0.25` |
| `condor_monthly.credit_target_frac` | `0.30` |
| `condor_monthly.profit_take_frac` | `0.50` |
| `condor_monthly.stop_frac` | `1.50` |
| `condor_monthly.plan_time` | `10:00` |
| `condor_monthly.entry_window_end` | `10:15` |
| `condor_monthly.hard_exit_time` | `14:30` |
| `condor_monthly.cost_share_max` | `0.20` |
| `condor_monthly.risk_per_trade_pct` | `1.0` |
| `condor_monthly.max_lots` | `3` |
| `condor_monthly.reserve_per_lot_inr` | `1000` |
| `condor_monthly.tier3_min_sessions` | `12` |
| `condor_weekly.variant` | `WEEKLY` |
| `condor_weekly.gap_max_pct` | `0.75` |
| `condor_weekly.range_max_pct` | `0.80` |
| `condor_weekly.er_max` | `0.30` |
| `condor_weekly.observation_start` | `09:15` |
| `condor_weekly.observation_end` | `09:59` |
| `condor_weekly.opening_range_end` | `09:44` |
| `condor_weekly.min_bars` | `45` |
| `condor_weekly.delta_min` | `0.20` |
| `condor_weekly.delta_max` | `0.25` |
| `condor_weekly.delta_target` | `0.22` |
| `condor_weekly.strike_buffer_points` | `0` |
| `condor_weekly.wing_width_points` | `150` |
| `condor_weekly.credit_floor_frac` | `0.25` |
| `condor_weekly.credit_target_frac` | `0.30` |
| `condor_weekly.profit_take_frac` | `0.50` |
| `condor_weekly.stop_frac` | `1.50` |
| `condor_weekly.plan_time` | `10:00` |
| `condor_weekly.entry_window_end` | `10:15` |
| `condor_weekly.hard_exit_time` | `14:30` |
| `condor_weekly.cost_share_max` | `0.20` |
| `condor_weekly.risk_per_trade_pct` | `0.5` |
| `condor_weekly.max_lots` | `2` |
| `condor_weekly.reserve_per_lot_inr` | `1000` |
| `condor_weekly.tier3_min_sessions` | `20` |
| `directional.trend_ema_days` | `20` |
| `directional.gap_max_pct` | `1.0` |
| `directional.opening_range_start` | `09:15` |
| `directional.opening_range_end` | `09:29` |
| `directional.opening_range_bars` | `15` |
| `directional.or_max_pct` | `0.90` |
| `directional.vix_max` | `22` |
| `directional.bar_minutes` | `5` |
| `directional.entry_window_start` | `09:30` |
| `directional.entry_window_end` | `13:30` |
| `directional.buffer_pct` | `0.05` |
| `directional.itm_steps` | `1` |
| `directional.delta_min` | `0.50` |
| `directional.delta_max` | `0.75` |
| `directional.stop_frac` | `0.30` |
| `directional.target_frac` | `0.60` |
| `directional.time_stop_minutes` | `45` |
| `directional.time_stop_min_gain` | `0.10` |
| `directional.hard_exit_time` | `15:00` |
| `directional.reserve_per_lot_inr` | `300` |
| `directional.premium_cap_pct` | `10` |
| `directional.cost_share_max` | `0.15` |
| `directional.risk_per_trade_pct` | `0.5` |
| `directional.max_lots` | `2` |
| `directional.tier3_min_sessions` | `60` |
| `expiry_setups.o3a_enabled` | `true` |
| `expiry_setups.o3b_enabled` | `true` |
| `expiry_setups.width_points` | `100` |
| `expiry_setups.max_debit_frac` | `0.55` |
| `expiry_setups.o3a_range_start` | `09:15` |
| `expiry_setups.o3a_range_end` | `10:14` |
| `expiry_setups.o3a_range_bars` | `60` |
| `expiry_setups.o3a_range_max_pct` | `1.20` |
| `expiry_setups.o3a_window_start` | `10:19` |
| `expiry_setups.o3a_window_end` | `13:00` |
| `expiry_setups.o3a_buffer_pct` | `0.05` |
| `expiry_setups.o3a_er_min` | `0.40` |
| `expiry_setups.o3b_gap_min_pct` | `0.50` |
| `expiry_setups.o3b_gap_max_pct` | `1.50` |
| `expiry_setups.o3b_hold_end` | `09:44` |
| `expiry_setups.o3b_plan_time` | `09:45` |
| `expiry_setups.o3b_window_end` | `10:00` |
| `expiry_setups.bar_minutes` | `5` |
| `expiry_setups.target_frac_of_width` | `0.80` |
| `expiry_setups.stop_frac_of_debit` | `0.50` |
| `expiry_setups.hard_exit_time` | `14:45` |
| `expiry_setups.reserve_per_lot_inr` | `500` |
| `expiry_setups.cost_share_max` | `0.20` |
| `expiry_setups.risk_per_trade_pct` | `0.5` |
| `expiry_setups.max_lots` | `2` |
| `expiry_setups.tier3_min_sessions` | `20` |
| `costs.brokerage_per_order_inr` | `20` |
| `costs.stt_sell_premium_pct` | `0.15` |
| `costs.stt_exercise_intrinsic_pct` | `0.15` |
| `costs.exchange_txn_pct` | `0.03553` |
| `costs.sebi_per_crore_inr` | `10` |
| `costs.ipft_per_crore_inr` | `0.01` |
| `costs.stamp_buy_pct` | `0.003` |
| `costs.gst_pct` | `18` |
| `costs.auto_squareoff_inr` | `50` |
| `costs.synthetic_half_spread_pct` | `1.5` |
| `costs.synthetic_half_spread_min_inr` | `0.10` |
| `costs.reviewed_on` | `2026-09-22` |
| `costs.review_after_days` | `90` |
| `sizing.first_live_trades` | `5` |
| `sizing.first_live_risk_multiplier` | `0.5` |
| `execution.limit_improve_ticks` | `1` |
| `execution.fill_wait_seconds` | `20` |
| `execution.exit_final_marketable` | `true` |
| `execution.latency_ticks` | `1` |
| `execution.plan_ttl_minutes` | `30` |
| `execution.feed_watch_from` | `14:00` |
| `execution.feed_grace_seconds` | `60` |
| `risk.daily_loss_r` | `2` |
| `risk.weekly_loss_r` | `4` |
| `risk.monthly_loss_r` | `8` |
| `risk.budget_breach_frac` | `1.0` |
| `risk.book_daily_loss_pct` | `1.5` |
| `risk.book_monthly_loss_pct` | `5` |
