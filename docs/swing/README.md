# The swing run (SW) — Kullamägi's setups, inside Baskfy

This folder commissions the next autonomous run: making Baskfy **find and manage momentum swing
trades** the way Kristjan Kullamägi (Qullamaggie) trades them — the flag breakout, the episodic
pivot, and (for the record only) the parabolic short — on NSE, through Kite, under the charter
that already governs this repo. The method itself is written down once, in Baskfy's terms, in
[`01-method.md`](01-method.md); this folder translates it into modules with acceptance criteria,
the same way `docs/01–08` translated the merge and `docs/smallcase/` translated smallcase.

**Read order for any agent session:**
`/CLAUDE.md` (the charter — it governs this run unchanged) → `docs/README.md` → this file →
[`02-scope-and-gating.md`](02-scope-and-gating.md) (what is forbidden) →
[`06-module-plan.md`](06-module-plan.md) (the task list) → the remaining docs as each module
cites them. The single prompt that starts the run is in [`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md).

| Doc | What it is |
|---|---|
| [`KICKOFF-PROMPT.md`](KICKOFF-PROMPT.md) | The one prompt Maulik pastes into a fresh CLI session |
| [`01-method.md`](01-method.md) | The method, end to end, and what does and does not transfer to NSE |
| [`02-scope-and-gating.md`](02-scope-and-gating.md) | **The law of this run.** Track A (build now) / Track B (dark, flag-off) / Track C (forbidden) |
| [`03-data-model.md`](03-data-model.md) | The `sw_` schema and how it joins `ohlcv_daily`, `factor_daily`, `instrument`, the desk journal |
| [`04-business-rules.md`](04-business-rules.md) | **The numerical contract.** Every threshold, formula and rule — the tests in `packages/core/tests/test_swing_*.py` assert this document |
| [`05-ui-spec.md`](05-ui-spec.md) | The web app's `/swing` hub (read-only) and the desk console's `/swing` operator page (confirm) |
| [`06-module-plan.md`](06-module-plan.md) | **The task list.** SW0–SW12, every module with a Goal and acceptance criteria |
| [`07-primary-source-corrections.md`](07-primary-source-corrections.md) | **Read before SW9.5.** Five rules corrected against his own words (stop ≤ 1 ADR, 10>20 index filter, 1-3 entries a day, 5-10/15-20 positions, 15-20% drawdown containment), with the patch that lands them |
| [`STATUS.md`](STATUS.md) | The live status page for this run — updated at the end of every module |
| [`DECISIONS-SW.md`](DECISIONS-SW.md) | Judgement calls, numbered by module; the pack's own pre-taken ones are PACK.1–PACK.6 |

## What already exists (built with the pack, 2 Sep 2026)

The pure core is **already written and green** so the run starts from working arithmetic
rather than from a blank file:

```
decile-blueprint/packages/core/src/baskfy_core/swing/
    __init__.py  config.py  indicators.py  setups.py  sizing.py  stops.py
    market.py  opening_range.py  plan.py  journal.py
decile-blueprint/packages/core/tests/
    swing_fixtures.py  test_swing_setups.py  test_swing_sizing.py  test_swing_stops.py
    test_swing_market.py  test_swing_opening_range.py  test_swing_plan_and_journal.py
    test_swing_purity.py
```

103 tests, `ruff check` + `ruff format --check` clean, `mypy --strict` clean, and
`test_no_escape_hatches.py` still green. Law 1 is asserted by `test_swing_purity.py`: the package
imports no database, network, disk or clock. **SW1 begins by re-running exactly that and recording
the counts in STATUS.** Nothing outside `packages/core` has been touched: no migration, no worker
task, no router, no page, no desk code. Those are the modules.

## The one-paragraph version

Baskfy already owns everything the method needs except the method: 3.5M adjusted daily bars from
2017 with corporate actions applied, a nightly pipeline that publishes `factor_daily` (returns,
MAs, turnover, 52-week highs) and `market_health_daily` (breadth), a Kite provider with a token
bridge, an execution gateway that is the only path to an order, a GTT stop path, a journal, and a
desk console whose `/analyze → plan → /execute confirm=true` shape is exactly the shape a
discretionary swing trader needs. The swing run adds the **detectors** (which names are flags,
which gapped on a catalyst out of a neglected base, which went parabolic), the **watchlist with
levels** (pivot, stop reference), the **live opening-range monitor** for the first ninety minutes
(5-minute ORH break → a trigger the desk shows and Maulik confirms), the **sizing** (risk per
trade, not conviction), the **exits** (partial on day 3–5, breakeven, 10/20-DMA trail), the
**market gate and progressive exposure** (breadth plus his own recent results), a **journal in R**,
an **EOD backtest** over the 2017→ history, and the **pages** that show all of it. Orders still
fire only from the desk, only on confirm, only in `DRY_RUN=false` after the gates in `02` are met.

## The key mapping (memorize this)

| Kullamägi's concept | Baskfy implementation |
|---|---|
| Universe scan (top gainers 1/3/6M, ADR ≥ 3.5–4%, $ volume) | `factor_daily.ret_1m/3m/6m` + new `adr_pct`, `turnover_avg` in `baskfy_core.swing.indicators`; `liquid_expr` is the predicate |
| Flag / continuation setup | `detect_flags` → `sw_setup_daily` rows with `setup='FLAG'`, status `SETTING_UP` or `BREAKOUT_TODAY`, `pivot_high`, `stop_ref` |
| Episodic pivot | `detect_eps` on the gap day (EOD) + `live_gap` at 09:16 (pre-open quotes) → `setup='EP'` |
| Parabolic short | `detect_parabolic` → `setup='PARABOLIC_SHORT'`, **detect-only**; never a plan line (Track C) |
| Opening-range high break (1/5/60-min) | `opening_range` + `evaluate_trigger` fed by the desk's `TickBus` / Kite minute candles, 09:15–10:45 IST |
| Stop at low of day / ORL | `stops.initial_stop`; armed as a GTT through `OrderGateway.place_gtt_stop` the same session (non-negotiable 4) with a swing `StopBand` (PACK.3) |
| Sell ⅓–½ into strength day 3–5, trail 10/20 SMA, breakeven | `stops.manage` after every close → next morning's `SELL_AT_OPEN` / `RAISE_GTT_STOP` lines |
| Risk 0.25–1%/trade, 20–25% cap, never average down, never widen | `sizing.size_position` (named caps and refusals); ceilings are **system-only** env (`BASKFY_SWING_*`), the chosen value is a bounded setting |
| Market gate: breadth, index vs 10/20 MA, own results → progressive exposure | `market.breadth_snapshot` + `market_gate` + `exposure_tier`; the tier ladder is the exposure overlay of this book, separate from the desk's R1–R4 |
| The daily routine (premarket gap scan, first hour, EOD review, weekend scan) | Beat entries `swing-premarket` 08:50, the monitor 09:15–10:45, `swing-eod` after publish, `swing-weekend` Sat; all IST |
| Journal, win rate, R-multiples | `journal.summarize` over `sw_position` closes; the exposure ladder reads it |
| "Plan → confirm" | The desk's existing plan/confirm shape: a `SwingPlan` gets a `plan_id`, expires in 30 min, `POST /swing/execute confirm=true` per line |

## What this run is not

It is not a new order path (law 2 stands; the web app gets no order route; the desk console is
the only place a swing line becomes an order). It is not shorting (NSE cash equities cannot be
shorted for delivery; MIS/F&O sit behind `INTRADAY_ENABLED`/`OPTIONS_ENABLED` which stay off).
It is not leverage: no MTF, no margin, exposure is a fraction of a sleeve's cash. It is not
auto-execution: the monitor **raises** triggers; Maulik **confirms** them. And it is not a
promise that the method works on NSE — that is what the backtest (SW9), the journal (SW8) and
the shadow gate in `02` §3 exist to find out. The run's product test: **at the end, Maulik opens
the web app on a weekend and sees the flags forming with their pivots; on a weekday morning the
desk shows him which pivots broke their 5-minute opening range, sized to 0.5% risk, with the
stop the method prescribes; one click confirms; the GTT is armed; every evening the book is
managed by the rules; and the journal tells him, in R, whether he should be pressing or sitting.**

## The Go rewrite

`docs/go-rewrite/` (decided 30 Aug 2026) rewrites the Python backend in Go behind a strangler.
This run builds in **Python**, on the live path, because the Go lanes have not started
(`docs/go-rewrite/STATUS.md`: pre-G0). `baskfy_core.swing` is pure and typed on purpose so that
L1 (core/signals) can port it from goldens dumped by `tools/parity/golden.py` when its turn comes;
SW12 adds the swing goldens and a line in `docs/go-rewrite/REQUESTS.md`. Do not build any swing
piece in Go during this run.
