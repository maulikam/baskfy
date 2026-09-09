# 02 — Scope and gating: the law of this run

Same shape as `docs/swing/02-scope-and-gating.md`, because the same charter governs. Three
tracks. A module that cannot say which track a surface is on has not understood it.

This run is the first in the repo to build **toward** an F&O order. Non-negotiable 5 puts every
such order behind `INTRADAY_ENABLED` (MIS) and `OPTIONS_ENABLED` (NFO/BFO), both default off,
enforced inside the gateway; `guards.assert_not_overnight_option` adds that an option is MIS or
nothing. Nothing in this run edits those three facts. The run builds the whole book so that it
is correct, drilled and paper-proven **with all of them off**, and then stops.

## Track A — build now, live for the sole user, `DRY_RUN=true`

* The `oc_` schema (`03`), the pure core (`baskfy_core.condor`), the expiry calendar and event
  days, the chain reader, the gate, the structure and sizing, the exit engine, the risk ledger,
  the journal, the backtest tiers `07` allows.
* The desk process `condor_expiry` (behind `BASKFY_CONDOR_MONITOR_ENABLED`, below) that observes
  09:15–09:59 on expiry days, raises the 10:00 verdict and plan, and runs the exit engine to
  14:30 — with `DRY_RUN=true`, which simulates end to end through the gateway, the depth-ladder
  fill simulator and the journal (non-negotiable 1).
* The desk console's `/condor` operator page: today's verdict, the plan, the four legs, the
  margin, the cost test, the **Confirm** button, the live D against ½C and 1.5C, the exit lines.
* The web app's `/condor` hub, **read-only** — the same rule as `/baskets` and `/swing`: every
  mutation is a 405 except the settings form (validated against the ceilings) and event-day
  edits (they change no money).
* The chain snapshot collector (`oc_chain_snapshot`, `07` §3): on expiry days, the strikes the
  strategy could use, once a minute, with depth — the forward dataset the paper record and the
  eventual real backtest need. Behind `BASKFY_CONDOR_CHAIN_COLLECT_ENABLED` for the limiter's
  sake, not for safety.
* Alerts: the 10:00 verdict/plan and the 14:30 flat confirmation through the existing alert
  machinery and the dark Telegram notifier; the monthly journal email.

## Track B — built dark, flag-off, tests assert unreachability

| Flag | Default | What it unlocks | Flip condition |
|---|---|---|---|
| `BASKFY_CONDOR_EXECUTION_ENABLED` | `false` | A confirmed condor line may call `OrderGateway.place` with `DRY_RUN=false`. With it false, `/condor/execute` returns the simulated result and journals `simulated=true` **regardless of `DRY_RUN`** | §3 below — by Maulik's written decision, recorded in NEEDS-MAULIK.md |
| `OPTIONS_ENABLED` (desk, existing) | `false` | NFO/BFO orders pass the gateway's product gate | §3; a `LOCKED_KEY` in the desk's settings boundary, never a form field |
| `INTRADAY_ENABLED` (desk, existing) | `false` | MIS orders pass the gateway's product gate | §3; same boundary |
| `BASKFY_CONDOR_MONITOR_ENABLED` | `false` | The desk process subscribes the index token and the chain on expiry days | After OC6's replay test is green and one expiry morning has been observed by hand |
| `BASKFY_CONDOR_CHAIN_COLLECT_ENABLED` | `false` | The once-a-minute chain snapshot on expiry days | After OC3 proves the snapshot fits the shared limiter with the swing book's own reads (`07` §3) |
| `BASKFY_CONDOR_BANKNIFTY_ENABLED` | `false` | BANKNIFTY becomes a second underlying the planner may consider | Only after OC9's tiers and six paper expiries show BANKNIFTY **independently** better after costs; Maulik's written decision |

A flag is read once per process at startup by `baskfy_worker.settings` / the desk's `config.py`
and never from a form. Flags are **system-only** in `.env.example` (M4 convention). A real
condor order requires **four** of them at once — `DRY_RUN=false`, `OPTIONS_ENABLED`,
`INTRADAY_ENABLED`, `BASKFY_CONDOR_EXECUTION_ENABLED` — and `condor_gates()` is the one function
that ANDs them (the swing book's `swing_gates()` is the precedent). OC10 asserts that with any one
of the four false, no code path reaches a broker.

### Ceilings (system-only env; a setting may sit below them, never above)

| Env | Default | Bounds |
|---|---|---|
| `BASKFY_CONDOR_RISK_PER_EXPIRY_INR_MAX` | `50000` | `oc_config.risk_budget_inr` |
| `BASKFY_CONDOR_RISK_PCT_MAX` | `0.5` | `risk_budget_inr / account_inr × 100` |
| `BASKFY_CONDOR_MAX_LOTS_MAX` | `10` | lots on any plan |
| `BASKFY_CONDOR_DAILY_LOSS_INR_MAX` | `30000` | `oc_config.daily_loss_limit_inr` |
| `BASKFY_CONDOR_MONTHLY_LOSS_INR_MAX` | `75000` | `oc_config.monthly_pause_inr` |
| `BASKFY_CONDOR_HARD_EXIT_LATEST` | `14:30` | `oc_config.hard_exit_time` may be earlier, never later |

## Track C — forbidden in this run, whatever a module thinks it found

1. **No naked leg, ever.** Every plan has four legs; a short is never sent while its wing is
   unfilled (`structure.entry_sequence`); a wing is never closed while its short is open
   (`exit_sequence`). A partial fill on a wing cancels the plan before any short is sent. The
   strangle lab under `frozen/` is not thawed, imported or edited (D4, safety rails).
2. **No overnight.** Product is `MIS` and nothing else; the gateway's guard already refuses
   NRML/CNC on an option and this run adds no bypass. `hard_exit_time` ≤ 14:30.
3. **No re-entry, no rolling, no averaging, no conversion.** One `oc_session` per expiry per
   underlying; its state machine has no edge back to `PLANNED` once it has left it.
4. **No weekly expiries.** `calendar.is_trading_expiry` is true only for the month's last
   expiry of the underlying as the instrument master states it; the desk refuses every other day.
5. **No auto-execution of an entry.** A plan is a row and a page. An order requires
   `POST /condor/execute` with `confirm=true` and a `plan_id` issued in the last 30 minutes —
   and in this book, issued **today, before 10:15**. (What the confirm covers for *exits* is
   PACK.5 in `DECISIONS-OC.md`: the rule-driven closes, the 14:30 flat included, are part of
   the plan that was confirmed — the option book's analogue of the GTT stop non-negotiable 4
   arms without a second click.)
6. **No web-app orders.** `apps/web` gets no route under `/condor` that can reach the gateway.
   The existing `test_*_readonly.py` pattern is extended to the new router.
7. **No touching the weekly book or the swing book.** The condor book never reads their
   positions, cash or overlays, and never sells or closes anything it did not open. Its margin
   pool is a number in `oc_config`, not a claim on the account's holdings.
8. **No multi-tenant.** `BASKFY_SOLE_USER_ID` / `BASKFY_SOLE_BROKER_ACCOUNT_ID` only; every `oc_`
   row carries `user_id` (P4.1) so nothing needs a migration on the day D3 is answered.
9. **No new data provider, no scraping.** Option quotes and depth from the Kite provider's
   `quote()`; index minute bars from `historical_data`; expiries and lot sizes from
   `instruments("NFO")`; margins from `basket_order_margins` — all through the existing rate
   limiter, shared with everything else that reads Kite. Historical option prices from a vendor
   are a **NEEDS-MAULIK** item (`07` §4), never something the run acquires on its own.
10. **No sizing from margin.** Lots come from the risk budget (`04` §6). Margin is a *ceiling*
    the plan must fit under, never the quantity's source.

## §3 — The real-money gate

`BASKFY_CONDOR_EXECUTION_ENABLED=true` (with `OPTIONS_ENABLED`, `INTRADAY_ENABLED` and
`DRY_RUN=false`) may be set only when **all** of the following are true, each with its evidence
in `OC-FINAL-REPORT.md`:

1. **OC10 green.** With the execution flag false a property test proves no condor code path
   reaches `OrderGateway.place`; with the product flags false the gateway itself refuses every
   leg (an integration test, not a unit test); with `DRY_RUN=true` the full drill
   (`tools/condor/drill.py`) produces four simulated entry fills in sequence, a simulated exit in
   sequence, and **0 orders reach a broker**.
2. **Six paper expiries on the live chain.** Six consecutive monthly-expiry Tuesdays on the
   deployed box with a Kite login before 09:00 and `DRY_RUN=true`: the observation, the 10:00
   verdict, the plan (or the reasoned skip), a confirm, fills simulated against the real depth
   ladder, the exit engine to a close, the 14:30 flat, the journal row with `simulated=true`,
   the chain snapshots for the day. A skipped day counts as a paper expiry — the filter is the
   strategy — but at least **three of the six must have carried a position**, or the count
   continues.
3. **The backtest tiers on the page.** Tier 1 (the gate over real index minute bars, 2015→) and
   Tier 2 (the synthetic-priced P&L, labelled as a model) on `/condor/journal` with `07`'s
   caveats verbatim; Tier 3 (vendor option prices) if and only if Maulik has bought the data.
4. **Maulik's written risk decision** in `DECISIONS-OC.md`: the margin pool, the execution
   buffer, the starting risk budget (the method says ₹10,000–₹15,000 and one or two lots), the
   per-expiry / daily / monthly limits, the absolute ceiling, and NIFTY only.
5. **Broker readiness, by his hand:** the F&O segment active on the account, the margin pool
   in place, `basket_order_margins` answering for a real four-leg basket, and the desk's static
   IP unchanged. Recorded in NEEDS-MAULIK.md under **Condor**.
6. **The flags are flipped by his hand.** The swing run's §3.5 was later delegated (SW23) after
   it produced a loop; this pack does **not** pre-delegate. If Maulik chooses to delegate the
   flip, he writes that in NEEDS-MAULIK.md under Condor and the agent records it in
   `DECISIONS-OC.md` before acting. An agent still never places an order itself, never confirms
   a plan on his behalf, and never widens a budget, a limit or a ceiling.
