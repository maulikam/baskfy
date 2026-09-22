# 02 — Scope and gating: the law of this run

Same shape as `docs/condor/02`, `docs/swing/02`, `docs/vbt/02`, `docs/twt/02`, because the same
charter governs. Three tracks. **A module that cannot say which track a surface is on has not
understood it.**

Non-negotiable 5 puts every F&O order behind `OPTIONS_ENABLED` and every MIS order behind
`INTRADAY_ENABLED`, both default off, enforced inside the gateway (`ProductGates`,
`guards.product_exchange_refusal` — an allow-list since AF 0.7); `guards.
assert_not_overnight_option` refuses any option under a carry product. **Nothing in this run edits
those three facts.** The run builds all three sleeves so that they are correct, drilled and
paper-proven with every money flag off, and then stops.

## Track A — build now, live for the sole user, paper only

* The `op_` schema (`03`); the pure core `baskfy_core.options` (calendar, greeks, costs, sizing,
  session, risk, journal, backtest engine, and the three sleeves' signal/structure/exit modules).
* The NFO contract master and `op_expiry` (nightly), `op_event_day`, the index minute bars
  (NIFTY 50 and India VIX), the backfill for Tier 1.
* The **chain collector** — one `quote()` call a minute for the strikes within reach on the two
  nearest expiries, with depth and OI, 09:15–15:30 every trading day, into `op_chain_snapshot`.
  Behind `BASKFY_OPTIONS_COLLECT_ENABLED` for the limiter's sake, not for safety.
* The **scans** — each sleeve's today's-candidates computation each minute into `op_scan` —
  behind `BASKFY_OPTIONS_SCAN_ENABLED` (limiter, not safety).
* The web app's **Options** tab (`/options`), **read-only**: every mutation is a 405 except event-day
  add/remove and the settings form, which change no money (the `/swing`, `/vbt`, `/twt` rule).
* The desk process `options_monitor` (behind `BASKFY_OPTIONS_MONITOR_ENABLED`) that raises each
  sleeve's plan and runs the exit engine; the desk's `/nifty-options` operator page with the
  **Confirm** button — every confirm simulated end to end through the real gateway's dry-run
  branch, the depth-ladder fill simulator and the journal (non-negotiable 1).
* Alerts: per-sleeve plan/verdict, the flat-by-close confirmation, the weekly paper summary —
  through the existing alert machinery and the dark Telegram notifier.

## Track B — built dark, flag-off, tests assert unreachability

| Flag | Default | What it unlocks | Flip condition |
|---|---|---|---|
| `BASKFY_OPTIONS_O1M_EXECUTION_ENABLED` | `false` | A confirmed O1-M line may reach `OrderGateway.place` with `DRY_RUN=false`. With it false, `/nifty-options/execute` returns the simulated result and journals `simulated=true` **regardless of `DRY_RUN`** | §3, by Maulik's hand |
| `BASKFY_OPTIONS_O1W_EXECUTION_ENABLED` | `false` | Same, O1-W | §3 |
| `BASKFY_OPTIONS_O2_EXECUTION_ENABLED` | `false` | Same, O2 | §3 |
| `BASKFY_OPTIONS_O3_EXECUTION_ENABLED` | `false` | Same, O3 (both setups) | §3 |
| `OPTIONS_ENABLED` (desk, existing) | `false` | Derivative venues pass the gateway's product gate | §3; a `LOCKED_KEY`, never a form field |
| `INTRADAY_ENABLED` (desk, existing) | `false` | MIS passes the product gate | §3; same boundary |
| `BASKFY_OPTIONS_MONITOR_ENABLED` | `false` | The desk process subscribes the index and legs and raises plans | After OP9's replay test is green; operational, moves no money |
| `BASKFY_OPTIONS_COLLECT_ENABLED` | `false` | The once-a-minute chain snapshot | After OP3's limiter measurement; operational (PACK.11) |
| `BASKFY_OPTIONS_SCAN_ENABLED` | `false` | The once-a-minute scans | After OP3's limiter measurement; operational (PACK.11) |

A real options order needs **four** flags at once for its sleeve — `DRY_RUN=false`,
`OPTIONS_ENABLED`, `INTRADAY_ENABLED`, `BASKFY_OPTIONS_<SLEEVE>_EXECUTION_ENABLED` — and
`options_gates(sleeve)` is the one function that ANDs them (`swing_gates()` is the precedent).
Flags are read once per process at startup, system-only in `.env.example` (M4 convention), and
never from a form.

**There is no auto-execute flag, and none may be added.** No name matching
`BASKFY_OPTIONS_*AUTO*` is read anywhere (OP13 scans for it). The swing sleeve's SW25/SW26 and
TWT's TW17 exceptions are Maulik's for those books only; CLAUDE.md forbids an agent to add a
second one.

**`OPTIONS_ENABLED` opens more than this run's door** — record this before anyone flips it. The
desk still carries the frozen lab's deferred hooks: `app/main.py`'s `/options`, `/options/run`,
`/options/data` (404 today *because* the gate is off), `app/analytics/ops.py`'s
`_options_operations()`, `app/analytics/autorun.py`'s options loop, and a partial
`app/strategies/strangle/` (`calendar_nse.py`, `clock.py`, `config.py`, `instruments.py`) still in
the live tree. OP0 inventories exactly what `OPTIONS_ENABLED=true` would wake today, and OP13 adds
a test that with it true those hooks stay dark unless `frozen/` is thawed (a decision that is D4's,
not this run's). Until that test is green, §3 cannot be met.

**OP0's inventory (22 Sep 2026, `STATUS.md` OP0 §5) found two more things.** (1) `/ops` is *not*
dark: `_options_operations()` guards on an `ImportError` from `strategies.strangle.instruments`, which
is live, so the flag alone puts five strangle controls on the page (they would fail at spawn —
`DECISIONS-OP` OP0.5). (2) The gateway's product gate admits **any** product on a derivative venue once
`options_enabled` is true — NFO MIS without `INTRADAY_ENABLED`, and NRML futures — so the flag alone
widens the weekly desk's gateway (OP0.6). OP2 tightens (2); OP13's test covers (1).

### Ceilings (system-only env; a setting may sit below them, never above)

| Env | Default | Bounds |
|---|---|---|
| `BASKFY_OPTIONS_RISK_PER_TRADE_INR_MAX` | `25000` | any sleeve's per-trade risk in ₹ |
| `BASKFY_OPTIONS_RISK_PCT_MAX` | `1.0` | `risk_per_trade_pct` of any sleeve |
| `BASKFY_OPTIONS_MAX_LOTS_MAX` | `10` | lots on any plan |
| `BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX` | `30000` | `op_book_config.daily_loss_limit_inr` |
| `BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX` | `75000` | `op_book_config.monthly_pause_inr` |
| `BASKFY_OPTIONS_HARD_EXIT_LATEST` | `15:00` | every sleeve's `hard_exit_time` may be earlier, never later |

## Track C — forbidden in this run, whatever a module thinks it found

1. **No overnight option.** `MIS` only; no path constructs another product for an option; no hard
   exit later than 15:00; the gateway's guard is not bypassed or wrapped.
2. **No naked short option, at any instant.** Wings before shorts, shorts closed before wings
   (O1); long before short, short closed before long (O3). A partial fill on a protecting leg
   abandons the entry before any short is sent. A property test proves short qty ≤ long qty per
   side at every step.
3. **No auto-execution of an entry, for any sleeve.** An entry requires
   `POST /nifty-options/execute` with `confirm=true` and a `plan_id` issued in the last 30 minutes
   (and within the sleeve's entry window). Exits under the confirm (PACK.2) are not entries.
4. **No web-app orders.** `apps/web` and `services/api` get no route that can reach the gateway;
   `test_options_readonly.py` on both sides of the wire.
5. **No stock options, no BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX or any other underlying** in v1.
   The code is underlying-agnostic; the config accepts `NIFTY` only and the settings API refuses
   anything else. (Supersedes condor PACK.2's dark BANKNIFTY — README.)
6. **No futures legs, no calendar or diagonal spreads, no ratio spreads, no rolling, no averaging,
   no re-entry after an exit on the same sleeve the same day, no converting a position.**
7. **No touching `frozen/strangle/`** (D4): not imported, moved, edited or linted. Its arithmetic
   is ported by re-implementation (PACK.2 carries condor PACK.1).
8. **No touching the other books.** The weekly rebalancer, swing, TWT and VBT books are not read
   for positions, cash or overlays; the options book never closes anything it did not open; its
   capital is a number in `op_sleeve_config`, never a claim on the account's holdings.
9. **No new data provider, no scraping.** Option quotes, depth and OI from the Kite provider's
   `quote()`; index minute bars from `historical_data`; expiries, strikes and lot sizes from
   `instruments("NFO")`; margins from `basket_order_margins`; the NSE F&O bhavcopy (EOD, `07` §2)
   only through the existing NSE provider's cookie/header discipline. A vendor purchase is
   NEEDS-MAULIK.
10. **No sizing from margin.** Lots come from the risk budget; margin is a ceiling the plan must fit
    under.
11. **No multi-tenant.** Sole user; every `op_` row carries `user_id` (P4.1).

## §3 — The real-money gate, per sleeve

`BASKFY_OPTIONS_<SLEEVE>_EXECUTION_ENABLED=true` (with `OPTIONS_ENABLED`, `INTRADAY_ENABLED`,
`DRY_RUN=false`) may be set only when **all** of these hold for that sleeve, each with its evidence
in `OP-FINAL-REPORT.md` or a later dated addendum:

1. **OP13 green**, including the `OPTIONS_ENABLED` side-door test above.
2. **The paper period on the live chain**, through the deployed desk, `DRY_RUN=true`, zero rule
   violations (no stale-quote entry, no orphan leg, no late exit, no journal gap):

   | Sleeve | Sessions | Of which carried a position |
   |---|---|---|
   | O1-M | 6 consecutive monthly expiries | ≥ 3 |
   | O1-W | 12 consecutive weekly expiries (non-monthly) | ≥ 6 |
   | O2 | 60 consecutive trading sessions | ≥ 25 |
   | O3 | 20 consecutive expiry days (weekly or monthly) | ≥ 8 |

   A skipped day counts as a session; a day the desk was down or had no Kite login does not.
3. **The tiers on the page** (`07` §4): Tier 1 and Tier 2 with their caveats; Tier 3 over the
   paper period's observed chains, with its sample size. Tier 3 expectancy after every cost must be
   positive for the sleeve — or Maulik writes that he is proceeding without it.
4. **Maulik's written capital and risk decision** for that sleeve in `DECISIONS-OP.md`: sleeve
   capital, risk %, max lots, loss limits.
5. **Broker readiness, by his hand** (NEEDS-MAULIK § Options): F&O segment active, the margin in
   place, `basket_order_margins` answering for a real basket, the desk's static IP unchanged, and
   the retail-algo requirements of the day (order tagging / registration as the broker then
   requires — OP0 records what Zerodha requires; this pack does not assert it).
6. **The flags are flipped by his hand.** This pack does not pre-delegate (condor PACK.8 carried).
   An agent never places an order, never confirms a plan on his behalf, and never widens a budget,
   a limit or a ceiling.
