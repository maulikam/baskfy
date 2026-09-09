# The condor run (OC) — one hedged expiry-day iron condor, inside Baskfy

This folder commissions the next autonomous run: making Baskfy **plan, gate, size, confirm and
manage one range-filtered, fully hedged NIFTY monthly-expiry iron condor** — an intraday,
defined-risk, option-selling trade — through Kite, under the charter that already governs this
repo. The strategy was settled by Maulik on 9 Sep 2026 and is written down once, in Baskfy's
terms, in [`01-method.md`](01-method.md); this folder translates it into modules with acceptance
criteria, the same way `docs/swing/` translated Kullamägi's setups and `docs/01–08` translated
the merge.

**Read order for any agent session:**
`/CLAUDE.md` (the charter — it governs this run unchanged) → `docs/README.md` → this file →
[`02-scope-and-gating.md`](02-scope-and-gating.md) (what is forbidden) →
[`07-data-reality.md`](07-data-reality.md) (what a backtest can and cannot claim here) →
[`06-module-plan.md`](06-module-plan.md) (the task list) → the remaining docs as each module
cites them. The single prompt that starts the run is in [`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md).

| Doc | What it is |
|---|---|
| [`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md) | The one prompt Maulik pastes into a fresh CLI session |
| [`01-method.md`](01-method.md) | The strategy, end to end, in plain words: when to trade, what to sell, when to leave, how much |
| [`02-scope-and-gating.md`](02-scope-and-gating.md) | **The law of this run.** Track A (build now) / Track B (dark, flag-off) / Track C (forbidden), and the real-money gate |
| [`03-data-model.md`](03-data-model.md) | The `oc_` schema and how it joins `instrument`, the desk journal and the Kite quote path |
| [`04-business-rules.md`](04-business-rules.md) | **The numerical contract.** Every threshold, formula and rule — the tests in `packages/core/tests/test_condor_*.py` assert this document |
| [`05-ui-spec.md`](05-ui-spec.md) | The desk console's `/condor` operator page (confirm) and the web app's `/condor` hub (read-only) |
| [`06-module-plan.md`](06-module-plan.md) | **The task list.** OC0–OC12, every module with a Goal and acceptance criteria |
| [`07-data-reality.md`](07-data-reality.md) | **Read before OC9.** Where expiry-day option prices can and cannot come from, and what each backtest tier is allowed to claim |
| [`STATUS.md`](STATUS.md) | The live status page for this run — updated at the end of every module |
| [`DECISIONS-OC.md`](DECISIONS-OC.md) | Judgement calls, numbered by module; the pack's own pre-taken ones are PACK.1–PACK.8 |
| [`QUESTIONS.md`](QUESTIONS.md) | The questions only Maulik can answer, with the pack's standing default for each so the run never waits |

## The one-paragraph version

Baskfy already owns the hard parts of an options desk except the options: a Kite provider with a
token bridge and a shared rate limiter, an execution gateway that is **the only path to an
order** and that already refuses NFO without `OPTIONS_ENABLED`, refuses MIS without
`INTRADAY_ENABLED`, and refuses *any* option under a product that can carry past the close
(`baskfy_execution.guards.assert_not_overnight_option` — this system never holds an option
overnight), a `TickBus` and a desk process model that the swing book's opening-range monitor already
runs on (deployed to the box 3 Sep 2026), a plan → `plan_id` → `confirm=true` shape with a thirty-minute expiry,
a journal, and a desk console. It also owns, frozen under `frozen/strangle/` since M6, a 4,347-line
paper-only options lab with an `IronCondorPlanner`, a Black–Scholes/IV/delta module, a cost model
with STT-on-exercise, a depth-ladder fill simulator and an NSE expiry calendar. The condor run adds
the **expiry-day gate** (gap, first-45-minute range, opening-range containment, efficiency ratio,
event days), the **chain reader** (one `quote()` call for the strikes that matter, with depth), the
**strike picker** (delta band *and* outside the opening range), the **structure** (wings first,
credit floor as a fraction of width, a cost test against the profit target), the **sizing** (lots
from a rupee risk budget, never from margin headroom), the **exit engine** (½C profit, 1.5C stop,
short-strike touch, 14:30 hard exit, shorts before wings, no rolling), the **risk ledger** (per
expiry, per day, per month, absolute), a **journal in ₹ and in R**, the **backtest tiers** that
`07` allows, and the **pages** that show all of it. Orders still fire only from the desk, only on
confirm, only in `DRY_RUN=false` after the gates in `02` are met — and for this run that means
*three* product flags and one execution flag, each of which stays false unless Maulik writes
otherwise.

## The key mapping (memorize this)

| The method's concept | Baskfy implementation |
|---|---|
| Monthly-expiry day only, both indices on the last Tuesday | `baskfy_core.condor.calendar` — expiry dates derived from the Kite instrument master (`expiry` column), never computed from a weekday rule; `oc_session` rows exist only on those dates |
| Observe 09:15–09:59, enter ~10:00, one trade, no re-entry | The desk process `app/strategies/condor_expiry.py` on the `TickBus`, 09:15 → 14:30; `oc_session.state` is a one-way machine `OBSERVING → GATED/SKIPPED → PLANNED → CONFIRMED → OPEN → CLOSED` |
| Skip on gap > 0.75 %, 45-min range > 0.8 %, price outside the 09:15–09:44 range at 09:59, trending, event day | `baskfy_core.condor.gate.evaluate_day` over index 1-minute bars (Kite `historical_data(interval="minute")` for the index token, plus ticks) → a `DayVerdict` with every reason; event days from `oc_event_day` |
| Efficiency ratio ER ≤ 0.30 | `gate.efficiency_ratio` — Kaufman's ER over the 09:15–09:59 one-minute closes, exactly as `04` §2 defines it |
| Short call/put at 0.20–0.25 delta **and** outside the opening range; wings 150 / 300 points; credit ≥ 25–30 % of width | `baskfy_core.condor.chain` (quotes → `OptionQuote` rows, delta from expiry-day BS with the hours left) + `structure.build_condor` → an `CondorPlan` with four `Leg`s, or a `PlanRejected` with a code |
| Enter wings before shorts; exit shorts before wings | `structure.entry_sequence` / `exit_sequence` — the order the desk sends the legs in, asserted by tests; a short is never sent while its wing is unfilled |
| Costs ≤ 20 % of the profit target | `costs.expected_round_trip` (ported from the frozen lab's `options_costs`: brokerage, STT on sell, exchange txn, SEBI, stamp, GST, clearing) vs `0.5 × C × lots × lot_size` |
| Lots from a ₹25,000 risk budget over `(width − credit) × lot_size + reserve` | `sizing.lots_for_budget` — floor division, refuses to 0 and says why; never reads margin headroom as capacity |
| ELM 2 % on expiry-day short index options; broker basket margin before entry | `kc.basket_order_margins` through the Kite provider (a new read, rate-limited) → `oc_plan.margin_required_inr`; a plan whose margin exceeds the pool is `REJECTED_MARGIN` |
| ½C profit, 1.5C stop, short-strike touch, 14:30 hard exit, no rolling | `exits.evaluate` every tick; `ExitDecision` with a code; the desk turns it into the closing legs in sequence |
| Loss limits: per expiry, daily emergency, monthly pause, absolute ceiling | `risk.Ledger` over `oc_journal`; a breached limit sets `oc_config.paused_until` and the plan builder refuses |
| Backtest → 6 paper expiries → live at ₹10–15k risk → ₹25k → BANKNIFTY only if independently better | `02` §3 (the real-money gate) and `07` (what each backtest tier may claim); BANKNIFTY is Track B behind `BASKFY_CONDOR_BANKNIFTY_ENABLED` |

## What already exists, and what this run reuses without touching

The **frozen lab** (`frozen/strangle/kite-momentum-rebalancer/app/strategies/`) is read-only under
D4 and the safety rails, and it stays that way in this run. It is nonetheless the best reference
the run has: `options.py` (`IronCondorPlanner`, `black_scholes_price`, `implied_volatility`,
`option_delta`, `_liquid`, `_pick`, `evaluate_exit`), `options_costs.py` (`CostRates`,
`option_costs`, `exercise_stt`), `strangle/fills_paper.py` (`simulate_fill` against a depth
ladder), `strangle/calendar_nse.py`, `strangle/sizing.py` (`max_lots_by_tail`, `query_margin`)
and `strangle/rules.py` (`day_vetoes`, `entry_cost_gate`). **PACK.1** settles how they are used:
the run **ports the arithmetic into a new pure package `baskfy_core.condor`** with its own tests
that assert `04`, citing the frozen file it was derived from in each module's docstring; it does
not `git mv`, import from, or edit anything under `frozen/`. Thawing is a written reversal of D4,
which is Maulik's, not the run's.

Also reused, unchanged: the gateway's three product gates and the overnight-option guard; the
desk's `TickBus`, `BaseStrategy` and `swing_clock.py` (the desk is the clock); the swing run's
`PgSwingStore` pattern for a desk-side store over Postgres; `tools/swing/drill.py` as the template
for `tools/condor/drill.py`; the alert machinery and the dark Telegram notifier; the
`test_*_readonly.py` pattern for the web hub.

## What this run is not

It is not the strangle lab thawed — no naked legs, ever (Track C). It is not multi-index — NIFTY
only in Track A; BANKNIFTY is built dark and stays dark until its own numbers say otherwise. It is
not weekly expiries — monthly only; the desk refuses a Tuesday that is not the month's last
expiry. It is not an overnight book — the gateway already refuses NRML/CNC on an option, and the
hard exit at 14:30 is fifty minutes before the broker's own MIS square-off. It is not
auto-execution — the monitor **raises** a plan at 10:00 and Maulik **confirms** it; the exit
engine raises exit lines the same way, and OC7 decides (PACK.5) how the mandatory 14:30 exit is
guaranteed without a click. It is not a promise the strategy is profitable: SEBI's FY25–26 study
says most individual derivatives traders lose, the attached research reached "most defensible
candidate, not proven", and `07` says exactly which tier of evidence this repo can produce. The
run's product test: **on the last Tuesday of a month, at 10:00, the desk shows Maulik either a
reasoned no-trade or one four-leg plan with its credit, its width, its lots from the risk budget,
its margin from the broker and its cost test; one click confirms; wings fill before shorts; the
page shows D against ½C and 1.5C all day; at 14:30 the position is flat whatever else happened;
and the journal tells him, in ₹ and R over the last six expiries, whether he should be sizing up,
sitting still, or stopping.**

## The Go rewrite

`docs/go-rewrite/` (30 Aug 2026) is still pre-G0. This run builds in **Python** on the live
path, exactly as the swing run did; `baskfy_core.condor` is pure and typed so L1 can port it
from goldens later. OC12 adds the condor goldens and a line in `docs/go-rewrite/REQUESTS.md`. Do
not build any condor piece in Go during this run.
