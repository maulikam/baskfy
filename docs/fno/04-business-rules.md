# 04 — Business rules: the numerical contract of the FO run

Tests assert this file, never current behaviour (house rule 2). Every number here is a field of
`baskfy_core.fno.config` with the default shown; the settings form may move a field only inside
its bounds and below its env ceiling (`02`). Money is `Decimal`, rounded at write time; greeks
and IV are floats (options OP1.8, carried).

## §1 — Calendar, entry day and exits

| Field | Default | Bounds | Rule |
|---|---|---|---|
| `f1_underlyings` | `NIFTY, BANKNIFTY` | subset of the index underlyings in the NFO master | anything else is a 422 |
| `f1_entry_sessions_before` | `15` | 10–20 | entry day = the exchange session that many sessions before the **monthly** expiry, counted on `trading_day`. Monthly = the last expiry of the calendar month for that underlying in the master, never a weekday rule |
| `f1_entry_window` | `09:20–10:30` | inside 09:15–15:00 | the plan is raised at 09:20; `expires_at = min(issued + 30 min, 10:30)`. A missed window is a `LAPSED` plan, not a retry on a later day |
| `f1_loss_close_mult` | `1.5` | 1.0–2.0 | **Maulik, M.1.** Close the whole structure, shorts first, when the cost to close ≥ (1 + mult) × entry credit. Checked by the monitor on live mids every 60 s and at each close. It never widens past the wings' max loss. Tested: +0.022R, worst −0.73R (`RESEARCH.md` §B4) |
| `f1_profit_take_pct` | `50` | 30–80 | exit when the cost to close ≤ (1 − pct) × entry credit. Checked at each close (on the bhavcopy settle, for the mark) and by the monitor on live mid-quotes during the session; the exit plan fires on the live check |
| `fo_hard_exit_before_expiry` | `1` (index); stock sleeves `1` | 1–5 | flat by 15:00 on session `E − n`. **Never zero**: no position is ever held into its expiry day (`02` §2.2) |
| `f1_max_open_per_underlying` | `1` | 1 | one F1 structure per underlying; the next cycle's entry waits for the previous exit |

A position whose hard-exit date passes while the desk is down is exited at the next open, and the
journal marks it `LATE_EXIT`. That is a rule violation for the paper checklist (`02` §3.3).

## §2 — Structure, and the covered-overnight guard

**Strikes.** On the entry decision: `F` = the monthly future's live mid; `T` = calendar days to
expiry ÷ 365; `σ` = the ATM implied vol (Black-76 on `F`, mean of CE and PE, the options pack's
solver); `sd = F·σ·√T`.

| Leg | Strike | Qty |
|---|---|---|
| long call (wing) | the smallest listed strike ≥ `F + (k + w)·sd` | +q |
| short call | the smallest listed strike ≥ `F + k·sd` | −q |
| short put | the largest listed strike ≤ `F − k·sd` | −q |
| long put (wing) | the largest listed strike ≤ `F − (k + w)·sd` | +q |

`k = f1_short_sigma = 1.0` (0.75–1.5), `w = f1_wing_sigma = 0.5` (0.25–1.0). Wing strike ≠ short
strike on each side, or the plan is `REJECTED_STRUCTURE`. Credit
`C = Σ short mids − Σ long mids > 0`. Max loss per unit = `max(call width, put width) − C`.

**Liquidity** (refusals by name): each short leg has OI ≥ `f1_min_short_oi_lots` (500 lots) and a
live spread ≤ `f1_max_spread_pct` (5 %) of its mid; each wing has a two-sided quote.

**Entry sequence** (`fo_leg.entry_seq`): long put → long call → short put → short call. **Exit
sequence**: short call → short put → long call → long put. **The guard**
`assert_overnight_option_is_covered(plan, step, positions)` runs in the gateway before every NRML
option order. It computes the book *after* this order from the broker's positions plus the
plan's already-filled legs, and refuses unless, for each (underlying, expiry, type), the short
quantity ≤ the long quantity at a strike further from the money. It is property-tested over every
prefix and every partial fill of both sequences. A refused step abandons the entry, and the legs
already filled are longs only, so they are closed at once (`ABANDONED_PARTIAL`).

## §3 — Sizing and costs

`lots = floor(min(risk_budget_inr, BASKFY_FNO_RISK_PER_TRADE_INR_MAX) ÷ (max_loss_per_unit × lot_size))`,
capped at `fo_max_lots` (2; ceiling 10). `risk_budget_inr = sleeve_capital × risk_per_trade_pct`
(1.0 %). **F1's capital is ₹10,00,000** (Maulik, M.1), seeded by FO2, so ₹10,000 per structure,
and paper sizes exactly as live would. A sleeve at ₹0 (F2) runs one lot on paper, and live refuses
`NO_SLEEVE_CAPITAL`. Zero lots is `REJECTED_SIZE`, never rounded up to one. The broker's
`basket_order_margins` for the plan's legs must be ≤ free margin, or `REJECTED_MARGIN`. **Margin never sizes** (Track C §6).

Costs are `docs/options/04` §6's rates (OP0.1, verified 22 Sep 2026) on all eight orders, with no
exercise STT because nothing is held to expiry. The plan shows the round trip in ₹ and as a share
of the credit. `f1_max_cost_share` = 25 %: above it, `REJECTED_COST`. In the research the cost was
0.015R, about a third of the gross edge.

## §4 — The data layer

* `fo_contract_daily`: exactly `FO_BHAVCOPY_SCHEMA`, from the file of that date. Idempotent upsert
  on `(trade_date, symbol, expiry, strike, option_type)`. The nightly task runs at **18:30 IST**
  and retries hourly to 23:30. A day with no file by then is `MISSING` on the status page, never
  interpolated.
* `fo_underlying_daily.held_expiry` = the nearest expiry strictly after the *previous* session.
  `ret = ln(settle_t ÷ settle_{t−1})` of that contract. A session with `|ret| > ln(1.4)`, or below
  `ln(0.7)`, sets `ca_flag` and is excluded from the series and from signals for 5 sessions
  (`RESEARCH.md` method).
* `iv_atm`: the nearest monthly with ≥ 8 sessions left, the strike nearest the future's settle,
  Black-76 on the settle, `r = 0`, mean of CE and PE. It is null if either leg did not trade.
  `rv20 = stdev(ret, 20) × √252`.
* `basis_ann = (F ÷ S − 1) × 365 ÷ calendar days`, UDiFF days only, null before 8 Jul 2024.

## §5 — The Stock F&O information page

Columns: symbol · lot size · days to the near monthly · futures settle · basis (a.y.) · 5-session
futures OI change · IV · RV20 · IV ÷ RV20 · 1-year IV percentile · ban. Sorted by futures turnover
by default. **No column is named or coloured as a signal** (`01` §3). There is no green or red on
IV ÷ RV. A banner links `RESEARCH.md`'s verdict in one sentence: "None of these numbers predicted
a profitable trade after costs in 2022–2026."

## §6 — The quarterly re-test

In January, April, July and October, the re-test task runs every family of `RESEARCH.md` with
the parameters in its tables, as pure functions of `baskfy_core.fno.research` over
`fo_contract_daily` (Tier 2E). The slippage is the latest measured median per underlying where
FO3 has ≥ 20 sessions of it, and the research default otherwise. Each run writes
`fo_backtest_run` with the tier, the caveat text, n, net and gross R, per-year rows, and the
slippage source. The page shows the latest run per family beside the original.

## §7 — Loss limits

F1, per underlying: pause after **3** consecutive trades closed by the loss close or worse
(≤ −0.6R; the loss close makes −0.73R the tested worst). F2: pause new entries for the rest of
the calendar month once the month's closed F2 trades reach **−6R**. Book: pause when the
month's realised FO loss reaches `fo_book_config.monthly_pause_inr` (≤ the ₹75,000 ceiling). A
pause stops new entries only. Open structures run to their own exits.

## §8 — States (`fo_scan.state`, `fo_plan.state`)

Scan: `NOT_ENTRY_DAY` (with the next entry date) · `CANDIDATE` · `SKIPPED_EVENT` · `PAUSED` ·
`OPEN_POSITION` · `NO_DATA`; for F2 also `NO_SIGNAL`, `BLOCKED_BAN`, `BLOCKED_REGIME` (NIFTY below
its 50-session average), `BLOCKED_CAPACITY`. Plan: `ISSUED` → `CONFIRMED` → `FILLING` → `OPEN` →
`EXITING` → `CLOSED`, or `LAPSED`, `REJECTED_*`, `ABANDONED_PARTIAL`; a `ROLL` plan goes
`ISSUED` → `FILLING` → `OPEN` under the original confirm. Every non-happy state carries its reason
in words.

## §9 — Paper periods (`02` §3.3)

**F1: 6 consecutive monthly cycles per underlying** (12 structures in all), with ≥ 4 actually
opened. **F2: 60 consecutive trading sessions with ≥ 15 positions closed**, including ≥ 3 rolls.
Zero rule violations: no uncovered short at any step, no position into expiry day, no
`LATE_EXIT`, no journal gap. A cycle the desk missed for want of a Kite login counts as a
violation, not a skip, because an overnight position needs the desk on every day it is open.

## §10 — F2, stock-futures breakout long (paper; Maulik, M.1)

| Field | Default | Bounds | Rule |
|---|---|---|---|
| `f2_universe_turnover_pct` | `60` | 20–100 | F&O stocks in this top share by 20-session median futures turnover |
| `f2_breakout_sessions` | `20` | 10–60 | close > the max of the prior n continuous closes |
| `f2_trend_sessions` | `50` | 20–200 | close > its n-session average; NIFTY's continuous future > its own n-session average |
| `f2_stop_atr` | `3.0` | 1.5–5.0 | initial stop = entry − mult × ATR14 (at the signal close) |
| `f2_trail` | `true` | — | each close: stop = max(stop, highest close since entry − mult × ATR14 at entry). Never lowered |
| `f2_max_sessions` | `40` | 10–60 | time exit at 15:00 on that session |
| `f2_roll_before_expiry` | `1` | 1–3 | roll at 15:00 on E−n (`02` Track C §5's exception) |
| `f2_max_open` | `5` | 1–10 | open F2 positions; one per stock; ≤ 2 per NSE industry |

**The GTT** triggers at `max(stop, stop_from_vol(entry, ann_vol))`: for a long, the higher price is
the tighter stop. It is placed within the same session as the fill and modified
each evening after the trail moves. A GTT that fails to place is an alert and a `NAKED_FUTURE`
violation on the paper checklist; the monitor then exits the position at the next check.

**Sizing.** `lots = floor(min(capital × risk %, ₹25,000 ceiling) ÷ (entry − stop) ÷ lot_size)`.
One lot's 3-ATR risk is often ₹30,000–₹80,000, so most names size to zero under the ceiling and are
`REJECTED_SIZE`. That is the ceiling working, not a bug. Paper, with capital ₹0, runs one lot and
records what the live size would have been.

**Costs** per round trip, and again per roll: futures STT 0.05 % on the sale, exchange 0.00173 %
per side, stamp 0.002 % on the buy, ₹20 per order, GST 18 % on brokerage and exchange charges,
and the measured slippage (FO3), or 0.03 % a side until it is measured.
