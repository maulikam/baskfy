# The VBT run — the volume-breakout sleeve, inside Baskfy

This folder commissions the third sleeve. Beside the **weekly momentum book** (the desk's
Friday rebalance) and the **swing book** (`docs/swing/`), Baskfy learns to find, plan and manage
**VBT-1** — the strategy researched in
[`research/volume-breakout/STRATEGY.md`](../../research/volume-breakout/STRATEGY.md): the
Chartink "Ankur's Volume Scan", filtered down to the subset that carries an edge, entered on a
pullback limit, gated by breadth, stopped at 12% and exited on a close below the 21-day EMA.

The research note is the **spec**. This folder translates it into a schema, a nightly job, a
plan the desk confirms, two pages and a module plan with acceptance criteria, exactly the way
`docs/swing/` translated Kullamägi's method and `docs/condor/` translated the expiry-day condor.

**Read order for any agent session:**
`/CLAUDE.md` (the charter — it governs this run unchanged) → `docs/README.md` →
[`research/volume-breakout/STRATEGY.md`](../../research/volume-breakout/STRATEGY.md) → this
file → [`02-scope-and-gating.md`](02-scope-and-gating.md) (what is forbidden) →
[`06-module-plan.md`](06-module-plan.md) (the task list) → the remaining docs as each module
cites them. `docs/swing/` is the sibling: **copy its shape, its gating and its discipline, never
its rules.**

| Doc | What it is |
|---|---|
| [`01-method.md`](01-method.md) | The method, end to end, from STRATEGY §2–3 — what the scan is, why the raw scan is not a strategy, and which six filters make it one |
| [`02-scope-and-gating.md`](02-scope-and-gating.md) | **The law of this run.** Track A (build now) / Track B (dark, flag-off) / Track C (forbidden), and §3's real-money gate |
| [`03-data-model.md`](03-data-model.md) | The `vb_` schema and how it joins `ohlcv_daily`, `instrument`, the desk journal |
| [`04-business-rules.md`](04-business-rules.md) | **The numerical contract.** Every threshold as a named field of `baskfy_core.vbt.config`, with its value. The tests assert this document |
| [`05-ui-spec.md`](05-ui-spec.md) | The web app's `/vbt` hub (read-only) and the desk console's `/vbt` operator page (confirm) |
| [`06-module-plan.md`](06-module-plan.md) | **The task list.** VB0–VB10, every module with a Goal and acceptance criteria that are tests |
| [`STATUS.md`](STATUS.md) | The live status page for this run — updated at the end of every module, loud about what is NOT done |
| [`DECISIONS-VB.md`](DECISIONS-VB.md) | Judgement calls, numbered by module; the pack's own pre-taken ones are PACK.1–PACK.7 |
| [`QUESTIONS.md`](QUESTIONS.md) | The things only Maulik can answer, each with the standing default the run proceeds on |

## The one-paragraph version

Baskfy already owns everything VBT-1 needs except VBT-1. The data plant publishes 3.75 M
adjusted daily bars from 2017 with corporate actions applied; the nightly chain ends in
`publish` and then two post-publish steps that cannot fail the night; `packages/execution` is
the only path to an order and already carries guards, a rate limiter, a journal and a GTT stop;
the desk console's `/analyze → plan → /execute confirm=true` shape is exactly the shape this
strategy needs, because VBT-1 is an end-of-day strategy whose orders are decided at a close and
sent in a morning. What the run adds is the **signal** (the five Chartink lines plus the six
trend filters), the **breadth gate** (the share of the tradable universe above its own 200-DMA),
the **limit entry that works for three sessions and then expires** — the one piece of machinery
the desk does not have today — the **sizing** (ten equal slots, capped at 1% of the name's
20-day turnover), the **exits** (a 12% GTT stop and a close below the 21-day EMA), the
**backtest on the page**, and the two pages that show all of it. Orders still fire only from
the desk console, only on `confirm=true`, only against an unexpired `plan_id`, and only when
`BASKFY_VBT_EXECUTION_ENABLED=true` — which this run never sets.

## The key mapping (memorize this)

| STRATEGY.md concept | Baskfy implementation |
|---|---|
| Chartink's five lines (§1) | `baskfy_core.vbt.signals.chartink_scan`, thresholds in `ScanConfig` |
| The six trend filters A–F (§3) | `baskfy_core.vbt.signals.trend_filters`, thresholds in `TrendConfig`; together `detect_signals` → `vb_signal_daily` |
| "more than 40% of the tradable universe above its 200-DMA" (§3, regime gate) | `baskfy_core.vbt.breadth.breadth_above_dma` → `vb_breadth_daily`; the gate is `BreadthConfig.min_pct_above_dma` |
| The pullback entry: a limit at the signal-day close, working three sessions (§3) | `vb_order` rows in `WORKING`, `entry.valid_sessions`; the expiry sweep is VB7 |
| Equal weight, 10 slots, 3 new a session, 1% of 20-day turnover (§3) | `baskfy_core.vbt.sizing.size_position` with named caps and refusals, `SizingConfig` |
| 12% disaster stop as a GTT the same session (§3, non-negotiable 4) | `OrderGateway`-adjacent `place_gtt_stop` with a VBT `StopBand`; `ExitConfig.stop_pct` |
| Close below the 21-day EMA → sell at the next open (§3) | `baskfy_core.vbt.exits.manage` → a `SELL_AT_OPEN` line in the morning plan |
| 25 bps a side (§4) | `CostConfig.cost_pct_per_side`, used by the backtest only — live costs are the broker's |
| The 761 trades and the 18.2% CAGR (§4) | `baskfy_core.vbt.backtest` reproduces them from the same bars (VB2), and re-runs from the plant's bars into `vb_backtest_run` (VB9) |
| The six thin sessions and the 10% missing-bar tolerance (§1) | `baskfy_core.vbt.calendar.drop_thin_sessions`, `DataConfig` |
| "plan → confirm" | The desk's existing shape: a `VbtPlan` gets a `plan_id`, expires in 30 minutes, `POST /vbt/execute confirm=true` per line |

## What this run is not

It is not a new order path: law 2 stands, `packages/execution` is still the only way to an
order, and the web app gets no route that can reach it. It is not intraday — VBT-1 is decided
at a close and sent at an open, so there is no monitor, no tick bus and no opening range. It is
not auto-execution: **non-negotiable 1's named exception belongs to the swing sleeve alone**,
and this run neither widens it nor adds a second one; no `BASKFY_VBT_AUTO_EXECUTE` flag exists
and none is added. It does not touch the swing book's rules or the weekly book's R1–R4 overlay.
And it is not a promise that the strategy works: STRATEGY §5 says plainly that this is one
history and a favourable one, and every page that shows the number shows those caveats as a
component, not a footer.

## The run's product test

At the end: on a weekday evening the nightly chain writes the day's signals and the breadth
reading; the desk console shows tomorrow's working limit orders with their quantities and their
12% stops, and the morning's sell-at-open lines for every position that closed below its 21-EMA;
one click confirms each; a GTT is armed the same session; a limit that has not filled by the
third session's close is cancelled that evening; the web app shows the book, the gauge and the
backtest with its caveats — and with `BASKFY_VBT_EXECUTION_ENABLED=false`, every one of those
confirms is simulated end to end and **0 orders reach a broker**.
