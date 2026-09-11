# 02 — Scope and gating: the law of this run

Same shape as `docs/vbt/02-scope-and-gating.md` and `docs/swing/02-scope-and-gating.md`, because
the same charter governs. Three tracks. **A module that cannot say which track a surface is on has
not understood it.**

## Track A — build now, live for the sole user

* The `tw_` schema (`03`), the state and entry detectors, the breadth reading, the nightly job,
  the sleeve's own cash and book, the ratcheting trailing trigger, the backtest.
* The web app's `/twt` hub, **read-only** — the same rule as `/baskets`, `/swing` and `/vbt`:
  every mutation on it is a 405 except notes and dismissals, which change no money.
* The desk console's `/twt` operator page: tomorrow's buys, the ratchet lines, the book, the
  **Confirm** button — running with `DRY_RUN=true`, which simulates end to end through the
  gateway and the journal (non-negotiable 1).
* Alerts: the evening email (signals, plan preview, gate, naked GTTs, ratchets due) through the
  existing alert machinery.

## Track B — built dark, flag-off, tests assert unreachability

| Flag | Default | What it unlocks | Flip condition |
|---|---|---|---|
| `BASKFY_TWT_EXECUTION_ENABLED` | `false` | A confirmed TWT line may call `OrderGateway.place` / `place_gtt_stop` with `DRY_RUN=false`. With it false the desk's `/twt/execute` returns the simulated result and journals `simulated=true` **regardless of `DRY_RUN`** | §3 below, by Maulik's own hand, recorded in `NEEDS-MAULIK.md` § TWT |
| `BASKFY_TWT_NIGHTLY_ENABLED` | `true` | The nightly `compute_twt` step and the 21:00 IST retry actually detect. Default **true** because detection moves no money and a sleeve with no history is a sleeve with no evidence; set it false to silence the step without removing it | n/a — it is on |

**There is no third flag, and specifically there is no auto-execute flag.** Non-negotiable 1's
named exception is the *swing sleeve's*, by Maulik's own hand (`docs/swing/DECISIONS-SW.md`
SW25/SW26). An agent may not widen it, add a second one, or default any flag to true. **A TWT
order exists only because a human pressed Confirm on an unexpired plan**, and TW10 proves it with
a property test rather than an assurance.

A flag is read once per process at startup by `baskfy_worker.settings` / `baskfy_api.settings` /
the desk's `config.py`, and never from a form. Flags are **system-only** in `.env.example` (the M4
convention).

### The bounded settings (not flags)

| Setting | Where | Ceiling (system-only env) |
|---|---|---|
| `tw_config.sleeve_capital_inr` | `tw_config`, user-editable | none; **seeded at 0** — nothing is planned until Maulik sets it |
| `tw_config.max_open_positions` | `tw_config` | `BASKFY_TWT_MAX_OPEN_POSITIONS_MAX` [15] |
| `tw_config.max_position_pct` | `tw_config` | `BASKFY_TWT_MAX_POSITION_PCT_MAX` [15.00] |
| `tw_config.stop_pct` | `tw_config` | `BASKFY_TWT_STOP_PCT_MAX` [25.00] |
| `tw_config.trail_pct` | `tw_config` | `BASKFY_TWT_TRAIL_PCT_MIN` [18.00] — a **floor**, because tightening this one is the dangerous direction (`01` §5: 15 % halves the CAGR and doubles the drawdown) |

Every other threshold in `04` is a `baskfy_core.twt.config` field and is **not** a setting: a
threshold that can be changed in a form gets changed after a bad week. Changing one is a code
change with a `DECISIONS-TW.md` entry.

**Why `trail_pct` has a floor and not a ceiling.** Every other bounded setting in this repository
is capped above, because the risk is somebody making a position bigger. Here the measured cliff is
in the other direction: the trail is the only exit, and tightening it from 20 % to 15 % took the
research's CAGR from 20.9 % to 9.6 % and its drawdown from −24.7 % to −43 %. The ceiling that
matters is a floor. (DECISIONS-TW **TW0.5**.)

## Track C — forbidden in this run, whatever a module thinks it found

1. **No shorting.** No `SELL` line for a symbol the sleeve does not hold. No MIS, no F&O, no
   `product` other than `CNC`. The strategy is long-only and cash otherwise.
2. **No leverage.** No MTF, no `variety="co"/"bo"`, no margin; exposure ≤ 100 % of the sleeve's
   own cash.
3. **No auto-execution.** A signal is a row in `tw_signal_daily` and a line on a page. An order
   requires `POST /twt/execute` with `confirm=true` and a `plan_id` issued in the last thirty
   minutes. **No flag exists that changes this and none is added.** In particular the ratchet —
   which will want to fire on ten lines a night, for months — is a **plan line a person confirms**,
   never a background job that talks to the broker.
4. **No web-app orders.** `apps/web` gets no route under `/twt` that can reach the gateway. The
   `test_baskets_readonly.py` / `test_desk_readonly.py` / swing / VBT read-only pattern is
   extended to the new routers.
5. **No touching the weekly book, the R1–R4 overlay, the swing book or VBT-1.** The TWT sleeve
   never sells a holding it did not buy (`tw_position` is the source of truth for what it owns);
   it never writes an `sw_` or `vb_` row; the Friday rebalance, every swing rule and every VBT
   rule are unchanged. TW10 proves all four.
6. **No multi-tenant.** `BASKFY_SOLE_USER_ID` / `BASKFY_SOLE_BROKER_ACCOUNT_ID` only. Every `tw_`
   row carries `user_id`, as P4.1 requires, so nothing needs a migration the day D3 is answered —
   but no second user exists in this run.
7. **No new data provider, no scraping.** Bars from `ohlcv_daily`; quotes, when a module needs
   one, through the existing Kite provider and its limiter (≈ 3 req/s). No news source.
8. **No changes to the shared numbers.** The desk's `StopBand`, `GTT_LIMIT_FRACTION`,
   `MIN_TRADE_VALUE`, the swing sleeve's constants and `baskfy_core.vbt.config` are **read, never
   edited**. A TWT-specific value is a TWT-specific keyword, additive, defaulting to what the
   other books already use — the pattern `SWING_GTT_LIMIT_FRACTION` established and
   `VBT_GTT_LIMIT_FRACTION` repeated.
9. **No deploys.** The box, the nightly window guard and `tools/deploy/` are Maulik's. This run
   ends at a green tree, `FIRST-LIVE-MORNING.md` and a report.

---

## §3 — The real-money gate

> ### There is no paper phase. Maulik decided this on 11 Sep 2026, before the run started.
>
> His words, carried into the kickoff prompt: **no paper phase — the sleeve goes live, ₹25 lakh,
> as soon as the tree is green and he has flipped the flag himself.** The same call he made for
> VBT-1 that morning (DECISIONS-VB VB11.4) and for the swing book on 2 Sep (STANDING-ANSWERS A11).
>
> **`DRY_RUN_SESSIONS_REQUIRED` is 0 and there is no session-count condition in this section.**
> `tw_session` still counts the sessions the machinery has run and the pages still show the count,
> because "this sleeve has rehearsed three sessions" is worth knowing after it stops being a
> condition. It is **information**, not a gate.
>
> **What this changes, stated once and not argued.** This sleeve's one new mechanism is a stop
> that ratchets after every close, and a session count was the only thing that could exercise it
> before real money. It will now first ratchet with money behind it. That is a fact about the
> schedule, not an objection: the conditions below are the ones that carry evidence, and TW7's
> fill-day rule and TW10's sweep exist precisely because the first ratchet is also the first
> chance to leave a line naked.

`BASKFY_TWT_EXECUTION_ENABLED=true` may be set only when **all** of the following are true, each
with its evidence in `TW-FINAL-REPORT.md`:

1. **TW10 green.** With the flag false, a property test proves no TWT code path reaches
   `OrderGateway.place`; with `DRY_RUN=true` the full drill (`tools/twt/drill.py`) produces a
   simulated fill, a simulated GTT and a simulated ratchet for every confirmed line, and
   **0 orders reach a broker**.
2. **A green tree.** Both suites pass — `decile-blueprint`'s and the desk's — and the weekly book,
   R1–R4, the swing book and VBT-1 still run. This is the condition Maulik named on 11 Sep, and it
   is checkable rather than felt: `make test` and `make lint` in both trees.
3. **The backtest on the page.** `tools/twt/backtest.py` has been run over the plant's own
   backfilled bars and its CAGR, drawdown, trade count, win rate, profit factor and the
   gate-on/gate-off comparison are on `/twt/backtest`, beside the research numbers, with `01` §8's
   caveats **verbatim** and the drift flag of `06`'s TW9.
4. **The written runbook.** `docs/twt/FIRST-LIVE-MORNING.md` exists and is the sequence Maulik
   will actually follow on the first morning, down to the Kite login before 09:00 and how to stop
   the sleeve in one command. This is a condition, not a nicety: the first morning of a sleeve
   whose exit is a ratchet is the morning most likely to end with an unprotected line.
5. **Maulik's own flag flip, by his own hand.** The run never sets this flag. The swing run's 5 Sep delegation
   (`docs/swing/02` §3.5) is about the *swing* flag and does not extend here; **VBT-1's §3 says the
   same about its own**. If Maulik wants the same delegation for this sleeve he writes one line in
   `NEEDS-MAULIK.md` § TWT, and an agent records it in `DECISIONS-TW.md` before acting on it.
6. **Half size for the first ten live entries.** The first `first_live_entries` [10] entries taken
   with the flag true are sized at `risk_multiplier_first_live` [0.5] × the slot, applied **at plan
   time** so the line shown is the line sent (`04` §6.4). The counter lives in `tw_config` and
   counts **entries, not sessions** — a book that enters about eighteen times a year would
   otherwise exhaust a five-session allowance in a week that happened to have no signals. It is
   decremented once per filled entry by the session that filled it, never by a request.

**What is not delegated, in any circumstance.** An agent never places an order itself, never
confirms a line on Maulik's behalf, never widens the sleeve or a ceiling, never sets
`tw_config.sleeve_capital_inr`, and never sets `BASKFY_TWT_EXECUTION_ENABLED`. Those are the root
`CLAUDE.md` safety rails, and this section does not touch them.
