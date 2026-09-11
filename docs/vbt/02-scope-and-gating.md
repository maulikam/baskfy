# 02 — Scope and gating: the law of this run

Same shape as `docs/swing/02-scope-and-gating.md` and `docs/smallcase/02-scope-and-gating.md`,
because the same charter governs. Three tracks. **A module that cannot say which track a surface
is on has not understood it.**

## Track A — build now, live for the sole user

* The `vb_` schema (`03`), the signal and breadth detectors, the nightly job, the sleeve's cash
  and book, the working-order machinery, the backtest.
* The web app's `/vbt` hub, **read-only** — the same rule as `/baskets` and `/swing`: every
  mutation on it is a 405 except notes and dismissals (they change no money).
* The desk console's `/vbt` operator page: tonight's working orders, tomorrow's sell-at-open
  lines, the book, the **Confirm** button — running with `DRY_RUN=true`, which simulates end to
  end through the gateway and the journal (non-negotiable 1).
* Alerts: the evening email (signals, plan preview, gate, naked GTTs, expiring limits) through
  the existing alert machinery.

## Track B — built dark, flag-off, tests assert unreachability

| Flag | Default | What it unlocks | Flip condition |
|---|---|---|---|
| `BASKFY_VBT_EXECUTION_ENABLED` | `false` | A confirmed VBT line may call `OrderGateway.place` / `place_gtt_stop` with `DRY_RUN=false`. With it false the desk's `/vbt/execute` returns the simulated result and journals `simulated=true` **regardless of `DRY_RUN`** | §3 below, by Maulik's hand, recorded in `NEEDS-MAULIK.md` § VBT |
| `BASKFY_VBT_NIGHTLY_ENABLED` | `true` | The nightly `compute_vbt` step and the 21:00 IST retry actually detect. Default **true** because detection moves no money and a sleeve with no history is a sleeve with no evidence; set it false to silence the step without removing it | n/a — it is on |

**There is no third flag, and specifically there is no auto-execute flag.** Non-negotiable 1's
named exception is the *swing sleeve's*, by Maulik's own hand (`docs/swing/DECISIONS-SW.md`
SW25/SW26). An agent may not widen it, add a second one, or default any flag to true. A VBT
order exists only because a human pressed Confirm on an unexpired plan.

A flag is read once per process at startup by `baskfy_worker.settings` / `baskfy_api.settings` /
the desk's `config.py` and never from a form. Flags are **system-only** in `.env.example` (M4
convention).

### The bounded settings (not flags)

| Setting | Where | Ceiling (system-only env) |
|---|---|---|
| `vb_config.sleeve_capital_inr` | `vb_config`, user-editable | none; **seeded at 0** — nothing is planned until Maulik sets it |
| `vb_config.max_open_positions` | `vb_config` | `BASKFY_VBT_MAX_OPEN_POSITIONS_MAX` [15] |
| `vb_config.max_position_pct` | `vb_config` | `BASKFY_VBT_MAX_POSITION_PCT_MAX` [15.0] |
| `vb_config.stop_pct` | `vb_config` | `BASKFY_VBT_STOP_PCT_MAX` [15.0] |

Every other threshold in `04` is a `baskfy_core.vbt.config` field and is **not** a setting
(PACK.5's reasoning, inherited): a threshold that can be changed in a form gets changed after a
bad week. Changing one is a code change with a `DECISIONS-VB.md` entry.

## Track C — forbidden in this run, whatever a module thinks it found

1. **No shorting.** No `SELL` line for a symbol the sleeve does not hold. No MIS, no F&O, no
   `product` other than `CNC`. The strategy is long-only and cash otherwise (STRATEGY §5).
2. **No leverage.** No MTF, no `variety="co"/"bo"`, no margin; exposure ≤ 100% of the sleeve's
   own cash.
3. **No auto-execution.** A signal is a row in `vb_signal_daily` and a line on a page. An order
   requires `POST /vbt/execute` with `confirm=true` and a `plan_id` issued in the last
   30 minutes. **No flag exists that changes this and none is added.**
4. **No web-app orders.** `apps/web` gets no route under `/vbt` that can reach the gateway. The
   `test_baskets_readonly.py` / `test_desk_readonly.py` / swing read-only pattern is extended.
5. **No touching the desk's weekly book and no touching the swing book.** The VBT sleeve never
   sells a holding it did not buy (`vb_position` is the source of truth for what it owns); it
   never reads or writes the R1–R4 overlay; it never writes a `sw_` row; the Friday rebalance
   and every swing rule are unchanged. `06`'s VB10 proves all three.
6. **No multi-tenant.** `BASKFY_SOLE_USER_ID` / `BASKFY_SOLE_BROKER_ACCOUNT_ID` only. Every
   `vb_` row carries `user_id`, as P4.1 requires, so nothing needs a migration the day D3 is
   answered — but no second user exists in this run.
7. **No new data provider, no scraping.** Bars from `ohlcv_daily`; quotes, when a module needs
   one, through the existing Kite provider and its limiter (3 req/s). No news source.
8. **No changes to the shared numbers.** The desk's `StopBand`, `GTT_LIMIT_FRACTION`,
   `MIN_TRADE_VALUE` and the swing sleeve's own constants are read, never edited. A VBT-specific
   value is a VBT-specific keyword, additive, defaulting to what the other books already use —
   the pattern `SWING_GTT_LIMIT_FRACTION` established.
9. **No deploys.** The box, the nightly window guard and `tools/deploy/` are Maulik's. This run
   ends at a green tree and a report.

## §3 — The real-money gate

`BASKFY_VBT_EXECUTION_ENABLED=true` may be set only when **all** of the following are true, each
with its evidence in `VB-FINAL-REPORT.md`.

> ### The paper gate was withdrawn on 11 Sep 2026, by Maulik
>
> Condition 1 was **twenty `DRY_RUN` sessions**, and it is struck. His words: *"i dont want to
> have dry run at all go live"* (DECISIONS-VB **VB11.4**, `NEEDS-MAULIK.md` V4). The same call he
> made for the swing book at STANDING-ANSWERS A11, made earlier here.
>
> `DRY_RUN_SESSIONS_REQUIRED` is now **0**. `vb_session` still counts the sessions and the pages
> still show the count, because "this sleeve has rehearsed three sessions" is worth knowing after
> it stops being a condition — it is information now, not a gate.
>
> **What this changes, stated once and not argued.** The sleeve's one genuinely new mechanism is a
> limit that expires after three *sessions*, and a session count was the only thing that could
> exercise it before real money. It will now first run a full three-session window with money in
> it. That is a fact about the schedule, not an objection: the four remaining conditions below are
> unchanged, and they are the ones that carry evidence.

The conditions, as they now stand:

1. ~~**Twenty `DRY_RUN` sessions.**~~ **Withdrawn 11 Sep 2026** — see the box above.
2. **VB10 green.** With the flag false, a property test proves no VBT code path reaches
   `OrderGateway.place`; with `DRY_RUN=true` the full drill (`tools/vbt/drill.py`) produces a
   simulated fill and a simulated GTT for every confirmed line and **0 orders reach a broker**.
3. **The backtest on the page.** `tools/vbt/backtest.py` has been run over the plant's own
   backfilled bars and its CAGR, drawdown, trade count, win rate, profit factor and the
   gate-on/gate-off comparison are on `/vbt/backtest`, beside the research numbers, with
   STRATEGY §5's caveats verbatim and the drift flag of `06`'s VB9.
4. **Maulik's written risk decision**, as an entry in `DECISIONS-VB.md`: the sleeve's capital,
   the slot count, whether the equal-weight sizing stands, and the stop percentage. Until he
   writes it, `vb_config.sleeve_capital_inr` is **0** and nothing is planned. **The capital is
   answered — ₹25 lakh, equal weight, ten slots (11 Sep 2026, DECISIONS-VB VB11.1)** — but the
   decision in prose that this condition asks for is not yet written, and the capital is the
   number, not the decision. Note that ₹25 lakh makes the 1%-of-turnover cap bind for the first
   time; no backtest in this repository was produced at that size.
5. **Half risk for the first live sessions.** For the first `first_live_sessions` [5] LIVE
   sessions the plan is sized at `risk_multiplier_first_live` [0.5] × the slot size, applied at
   plan time so the line shown is the line sent (`04` §5.4). The countdown is the evening job's,
   once per LIVE session.

**What is not delegated.** An agent never places an order itself, never confirms a line on
Maulik's behalf, never widens the sleeve or a ceiling, and never sets
`BASKFY_VBT_EXECUTION_ENABLED`. The swing run's §3.5 delegation (5 Sep 2026) is about the *swing*
flag and does not extend here; if Maulik wants the same delegation for this sleeve he writes one
line in `NEEDS-MAULIK.md` § VBT, and an agent records it in `DECISIONS-VB.md` before acting on it.
