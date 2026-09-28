# 02 — Scope and gating: the law of the FO run

Same shape as `docs/options/02`, `docs/swing/02` and `docs/twt/02`, because the same charter
governs. There are three tracks. **A module that cannot say which track a surface is on has not
understood it.**

This pack does something the options pack forbade: it **holds derivatives across the close**.
Maulik chose that in session on 23 Sep 2026 ("Defined-risk overnight"; README), and this file is
where the permission is bounded. **v1 builds two paper sleeves: F1 (NIFTY and BANKNIFTY monthly
iron condors) and F2 (stock-futures breakout, long only, built by Maulik's choice against the
research, M.1), plus a data layer and an information page** (`01`). The
rules below are written for any overnight derivative, so a sleeve commissioned later inherits them
rather than re-arguing them. Everything the options pack's `02` says about O1–O3 stays true for
O1–O3. Nothing here widens them.

## §1 — What changes in the gateway, and exactly how far

Three facts in force today (non-negotiable 5; `docs/options/DECISIONS-OP` OP2.1):

| Fact | Where it lives | What the FO run does to it |
|---|---|---|
| A derivative venue admits **MIS only**, and MIS needs `INTRADAY_ENABLED` | `guards.product_exchange_refusal` | **Adds one branch.** `NRML` on `NFO` is admitted only when `OPTIONS_ENABLED` **and** `BASKFY_FNO_CARRY_ENABLED` are both true **and** the order carries a `fo_plan` reference (below). Every other product/venue pair is refused exactly as today |
| `assert_not_overnight_option` refuses any option under a carry product | `guards.py`, before any network call | **Replaced for FO orders only** by `assert_overnight_option_is_covered`. An NRML option order passes only if it is a leg of a registered `fo_plan` whose structure is defined-risk *at every prefix of its entry sequence* (`04` §2). Orders without a `fo_plan` reference, including every O1–O3 order, meet the old guard unchanged |
| A GTT on a derivative venue is refused whatever the switches | `guards.py` (OP2.1) | **Adds one branch, for F2.** A GTT on `NFO`/`NRML` for a **stock future** is admitted when the same two flags are on and it carries an F2 `fo_plan` reference, because non-negotiable 4 requires the stop (M.1). A GTT on an option stays refused (§2.4) |

The multi-tenant clause (the law of `packages/execution`) applies unchanged. Every FO order carries
`user_id` + `broker_account_id`.

## §2 — The four rules that make overnight safe enough to build

1. **Defined risk at every instant, broker-side.** A short option is never open without a long
   option of the same underlying, type and expiry, further from the money, in at least the same
   quantity. **Longs enter before shorts; shorts exit before longs.** If a protecting leg fills
   only partly, the entry is abandoned before any short is sent (the O1/O3 rule, carried). A
   property test proves `short_qty ≤ long_qty` per side at every step of every sequence. **There
   is no naked short option, ever, in any sleeve.**
2. **No stock derivative is held into expiry day.** Stock options and futures settle physically
   (`07` §3). Every stock-derivative position is flat by `fo_hard_exit_before_expiry` sessions
   before expiry (default `E−1`, 15:00; `04` §1). Index positions may hold to expiry day but exit
   before 15:00 on it.
3. **Every future has a resting stop the same session.** This is non-negotiable 4 read for
   futures. It applies to shorts as well as buys, because a short future is the riskier side. The
   stop is broker-side (a GTT, admitted by §1), so a desk that is down overnight does not remove
   it. It is the tighter of the sleeve's own stop and `stop_from_vol()` (Maulik, M.1).
4. **An option structure's stop is its structure, plus a close order.** Its maximum loss is fixed
   at entry and shown on the plan in ₹ and R. It is never placed as a GTT, because a GTT on the
   long leg would un-hedge the short; that is why §1 keeps option GTTs refused. In addition the
   desk closes the whole structure, shorts first, when its loss reaches `f1_loss_close_mult`
   (1.5) × the entry credit. **This is Maulik's reading of non-negotiable 4 for derivatives**
   (in session, 23 Sep 2026; `DECISIONS-FO` M.1). Agents do not reopen it.

## Track A — build now, live for the sole user, paper only

* The `fo_` schema (`03`), the F&O bhavcopy ingest (nightly, into `fo_contract_daily`), and the NFO
  master widened from NIFTY to every F&O underlying (`op_contract` gains the rows; FO2).
* The pure core `baskfy_core.fno`: the continuous futures series, IV and realised volatility per
  underlying, F1's calendar, structure, sizing and exits, and the re-test families of `RESEARCH.md`.
* The **nightly scan** that runs after the bhavcopy lands. F1's state for tomorrow (entry day or
  not, the proposed condor) goes into `fo_scan`, and the Stock F&O information rows into
  `fo_underlying_daily`. It sits behind `BASKFY_FNO_SCAN_ENABLED` (operational, not safety).
* The **quarterly re-test** of every `RESEARCH.md` family, and the 15:00 spread sample (`01` §2).
* The web app's **Options** tab gains a **Stock F&O** section and `/options/fno` (read-only; `05`).
  Every mutation is a 405 except the settings form, which changes no money.
* The desk's `fno_monitor` process (behind `BASKFY_FNO_MONITOR_ENABLED`). It raises the morning
  plan from the scan, re-prices it on live quotes, and runs the exit engine: the 50 % profit take and
  the E−1 exit. The desk page `/fno` carries the **Confirm** button. Every
  confirm is simulated end to end through the real gateway's dry-run branch and journalled
  `simulated=true` (non-negotiable 1).
* The **paper carry**: a simulated position is carried across closes and marked each night at the
  bhavcopy settle. It is not a claim on the account.

## Track B — built dark, flag-off, tests assert unreachability

| Flag | Default | What it unlocks | Flip condition |
|---|---|---|---|
| `BASKFY_FNO_<SLEEVE>_EXECUTION_ENABLED` (one per sleeve in `01`) | `false` | A confirmed line of that sleeve may reach `OrderGateway.place` with `DRY_RUN=false`. With it false the execute route returns the simulated result, **regardless of `DRY_RUN`** | §3, by Maulik's hand |
| `BASKFY_FNO_CARRY_ENABLED` | `false` | The gateway admits `NRML` on `NFO` for `fo_plan` orders only (options re-proved covered), and an F2 future's GTT stop (§1) | §3; a `LOCKED_KEY`, never a form field |
| `OPTIONS_ENABLED` (existing) | `false` | Derivative venues pass the product gate | §3, and `docs/options/02`'s side-door test must be green first |
| `BASKFY_FNO_SCAN_ENABLED` | `false` | The nightly scans | After FO2's bhavcopy ingest is green; operational |
| `BASKFY_FNO_MONITOR_ENABLED` | `false` | The desk process that raises plans and runs exits | After FO7's replay test; operational, moves no money |

A real FO order needs **four** flags for its sleeve: `DRY_RUN=false`, `OPTIONS_ENABLED`,
`BASKFY_FNO_CARRY_ENABLED` and `BASKFY_FNO_<SLEEVE>_EXECUTION_ENABLED`. **`INTRADAY_ENABLED` is
not one of them, and must not become one**: an FO order is NRML, never MIS. `fno_gates(sleeve)`
is the one function that ANDs them (`options_gates()` and `swing_gates()` are the precedents).

**There is no auto-execute flag for an entry, and none may be added.** The one name matching
`BASKFY_FNO_*AUTO*` is **`BASKFY_FNO_F3_AUTO_EXIT`** (M.5, Maulik's answer in session, 28 Sep
2026: "Auto-exit under a new flag"), and FO11's scan admits that name alone: it lets the monitor
send an F3 **exit** it raised itself, never an entry or an add, and it defaults false in code,
compose and the desk's env. The swing SW25/SW26 and TWT TW17 exceptions belong to those books
alone, and root `CLAUDE.md` forbids an agent to add another. **Exits under a confirm are not
entries**: one confirm covers the plan's rule-driven exits (options PACK.2, carried).

### Ceilings (system-only env; a setting may sit below them, never above)

| Env | Default | Bounds |
|---|---|---|
| `BASKFY_FNO_RISK_PER_TRADE_INR_MAX` | `25000` | any FO plan's max loss in ₹ |
| `BASKFY_FNO_RISK_PCT_MAX` | `1.0` | `risk_per_trade_pct` of any sleeve |
| `BASKFY_FNO_MAX_OPEN_POSITIONS_MAX` | `10` | open FO positions across all sleeves |
| `BASKFY_FNO_MAX_PER_UNDERLYING_MAX` | `1` | open FO positions on one underlying, across sleeves |
| `BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX` | `75000` | `fo_book_config.monthly_pause_inr` |

## Track C — forbidden in this run, whatever a module thinks it found

1. **No naked short option, at any instant, in any sleeve** (§2.1).
2. **No stock derivative held into expiry day.** No exercise, no assignment and no delivery,
   ever. Index positions exit before 15:00 on expiry day (§2.2).
3. **No auto-executed entry.** An entry requires `POST /fno/execute` with `confirm=true` and a
   `plan_id` issued in the last 30 minutes.
4. **No web-app orders.** `apps/web` and `services/api` get no route that can reach the gateway.
   `test_fno_readonly.py` checks both sides of the wire.
5. **No rolling, no averaging down, no adding to a loser, no converting a position**, and no
   re-entry on the same underlying and sleeve before the next scan. **One exception, F2's
   calendar roll**: at 15:00 on E−1, the held future is sold and the next month bought as one
   plan under the original confirm, same quantity, stop carried (`01` §1b). It is mechanical, it
   is not a new entry, and it is journalled as a roll with its own costs.
6. **No sizing from margin.** Lots come from the risk budget. Margin is a ceiling the plan must fit
   under (`basket_order_margins`).
7. **No touching the other books.** The weekly rebalancer, swing, TWT, VBT and the O-sleeves are
   not read for positions or cash. The FO book never closes anything it did not open. **The SGB is
   untouchable**, and so is pledging it: pledging is Maulik's act at the broker, never code
   (QUESTIONS Q6).
8. **No new data provider, no scraping.** The F&O bhavcopy comes only through `NSEProvider`
   (archive first, limiter, cookie discipline). Quotes and depth come from Kite `quote()`, masters
   from `instruments("NFO")`, margins from `basket_order_margins`.
9. **No trading in the F&O ban period** (a stock over 95 % of its market-wide position limit).
   The scan refuses it by name, and an open position in a banned stock may only be reduced.
10. **No touching `frozen/strangle/`.**
11. **No multi-tenant.** Sole user, and every `fo_` row carries `user_id` (P4.1).

## §3 — The real-money gate, per sleeve

`BASKFY_FNO_<SLEEVE>_EXECUTION_ENABLED=true` (with `OPTIONS_ENABLED`,
`BASKFY_FNO_CARRY_ENABLED` and `DRY_RUN=false`) may be set only when **all** of these hold for that
sleeve, each with its evidence in `FO-FINAL-REPORT.md` or a dated addendum:

1. **FO11 green**, including the options pack's `OPTIONS_ENABLED` side-door test and this pack's
   covered-overnight guard tests (§1).
2. **Capital and the non-negotiable-4 reading answered in writing.** For F1 both are done (M.1:
   ₹25 lakh, M.2; structure stop + close order). F2 still has capital ₹0.
3. **The paper period, through the deployed desk,** with `DRY_RUN=true` and zero rule violations
   (no uncovered short at any step, no position into expiry day, no future without its resting
   stop, no journal gap). The length is per sleeve, in `04` §9.
4. **Tier 2E (`07` §4) with positive expectancy after costs in at least three of the five sample
   years, including 2022.** Otherwise Maulik writes that he is proceeding without it. **Neither
   sleeve meets this today** (M.1): F1 with its loss close is −0.010R in 2022, and F2 is positive
   only in 2023. Both therefore need his written waiver, or better forward evidence, before a flag.
5. **Tier 3 over the paper period** with its measured slippage. If the measured slippage is worse
   than the Tier 2E assumption, the Tier 2E run is repeated at the measured number, and item 4 must
   still hold.
6. **Broker readiness, by his hand:** F&O segment active (OP0.4 found it is), the margin in place,
   the static IP, and the algo-ID requirements of the day (OP0.8).
7. **The flags are flipped by his hand.** Nothing is pre-delegated. An agent never places an
   order, never confirms a plan on his behalf, and never widens a budget, a limit or a ceiling.
