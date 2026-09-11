# The TW run (TWT) — "3 Week Tight Close", inside Baskfy

This folder commissions the third sleeve: **TWT-1**, the three-weeks-tight position strategy
researched in [`research/tight-close/STRATEGY.md`](../../research/tight-close/STRATEGY.md), built
into Baskfy beside the weekly momentum book and the swing book, under the charter that already
governs this repo. The research note is the spec; this folder translates it into modules with
acceptance criteria, the same way `docs/vbt/` translated the volume-breakout sleeve and
`docs/swing/` translated Kullamägi's setups.

**Read order for any agent session:**
`/CLAUDE.md` (the charter — it governs this run unchanged) → `docs/README.md` → this file →
[`02-scope-and-gating.md`](02-scope-and-gating.md) (what is forbidden) →
[`06-module-plan.md`](06-module-plan.md) (the task list) → the remaining docs as each module
cites them. The one prompt that starts the run is
[`research/tight-close/KICKOFF-PROMPT.md`](../../research/tight-close/KICKOFF-PROMPT.md).

| Doc | What it is |
|---|---|
| [`01-method.md`](01-method.md) | The method, end to end: the scan, the entry, the two stops, the gate, and what the research measured |
| [`02-scope-and-gating.md`](02-scope-and-gating.md) | **The law of this run.** Track A (build now) / Track B (dark, flag-off) / Track C (forbidden), and §3's real-money gate |
| [`03-data-model.md`](03-data-model.md) | The `tw_` schema, migration `0041_twt`, and how it joins `ohlcv_daily`, `instrument`, `trading_day` and the desk journal |
| [`04-business-rules.md`](04-business-rules.md) | **The numerical contract.** Every threshold, formula and rule — the tests in `packages/core/tests/test_twt_*.py` assert this document |
| [`05-ui-spec.md`](05-ui-spec.md) | The web app's `/twt` hub (read-only) and the desk console's `/twt` operator page (confirm) |
| [`06-module-plan.md`](06-module-plan.md) | **The task list.** TW0–TW10, every module with a Goal and acceptance criteria that are tests |
| [`STATUS.md`](STATUS.md) | The live status page for this run — updated at the end of every module, loud about what is NOT done |
| [`DECISIONS-TW.md`](DECISIONS-TW.md) | Judgement calls, numbered by module, each tagged `⚠ UNREVIEWED` until Maulik reads it |
| [`QUESTIONS.md`](QUESTIONS.md) | The handful only Maulik can answer, each with the default the run builds against |
| [`FIRST-LIVE-MORNING.md`](FIRST-LIVE-MORNING.md) | **Written by TW10.** The exact sequence for the first real session, and the daily routine after it |

## The one-paragraph version

A stock that has run 30 % above its low of three months ago, whose **last three weekly closes sit
within 3 % of each other**, is in a tight consolidation after a move — O'Neil's "three weeks
tight", in Chartink form, with a ₹30 floor and a 10,000-share volume floor. It is a **state**, not
an event: about fifty names hold it on any day and the median stay is five sessions. The event is
**the first day the closes are tight after at least five sessions out**, and that event is worth
+2.3 % after twenty sessions and +7.3 % after sixty, in both halves of the history, with no
context filter improving on it. What it needs is **room**: a 10 % stop turns it into a loser, and
a 20 % disaster stop plus a **20 % trailing stop off the highest high since entry** turns it into
a position book — ten slots, about eighteen trades a year, held a hundred sessions on average,
**20.9 % CAGR at a −24.7 % maximum drawdown** over 2017-10 → 2026-09, with a 40 % breadth gate
keeping it out of 2018-style markets. The trailing stop **is** the strategy (137 of 164 exits),
and re-setting it every session it ratchets is the one mechanism this sleeve adds to Baskfy.

## What is new here, and what is already built

Baskfy already owns everything this method needs except the method: 3.75 M adjusted daily bars
from 2017, a nightly pipeline that publishes them, a Kite provider behind a 3 req/s limiter, an
`OrderGateway` that is the only path to an order, a GTT stop path with its own guard, a journal,
and a desk console whose `/analyze → plan → /execute confirm=true` shape is exactly the shape a
position trader needs each morning. Two sleeves already sit on that shape — the swing book
(`docs/swing/`) and VBT-1 (`docs/vbt/`) — and this one copies their skeleton exactly.

**The one genuinely new mechanism is the ratchet.** Every other Baskfy stop either sits where it
was armed or is raised by a rule somebody presses once. TWT-1's stop is raised **after every
close on which the name made a new high since entry**, for months at a time, on ten lines at once.
The swing book's `RAISE_GTT_STOP` line already knows how to do it — cancel the resting trigger,
arm the new one, and treat "cancelled but not re-armed" as a **naked position** that the 15:15
sweep must find. TW6 reuses that path verbatim; TW4 computes tomorrow's trigger the night before
so the morning plan is arithmetic that has already been done.

## The key mapping (memorize this)

| TWT-1's concept | Baskfy implementation |
|---|---|
| The scan's five lines, point-in-time | `baskfy_core.twt.signals.tight_state` over `ohlcv_daily` — `close_raw` for the ₹30 floor, adjusted `close` for the weekly comparison |
| "Three weekly closes within 3 %" | `weekly_closes` — today's close plus the last close of each of the two preceding weeks the calendar holds (`04` §3.2) |
| "≥ 1.3 × the low of three months ago" | `month_low_back` — the low of the calendar month three months before this session's month (`04` §3.3) |
| The entry event | `entry_events` — the state is true today and was false on each of the previous five sessions (`04` §3.4) |
| Tradability | `min_turnover_inr` [₹5 crore] on the 20-session average turnover (`04` §3.5; the research used ₹2 crore — **DECISIONS-TW TW0.3**) |
| The regime gate | `baskfy_core.twt.breadth` — share of the traded universe above its own 200-DMA, `> 40 %` (`04` §4). The same measurement VBT-1 uses, over the same universe |
| Entry | Next session's **open, market** — `TwtLineKind.BUY_AT_OPEN` (`04` §5) |
| Sizing | Ten equal slots, 1 % of turnover, ₹10,000 floor, half size for the first ten live entries (`04` §6) |
| Disaster stop | 20 % below the fill, armed as a GTT the **same session** (non-negotiable 4; `04` §7.1) |
| Trailing stop | 20 % below the highest high since entry, re-computed after every close, raised through `RAISE_GTT_STOP` (`04` §7.2) |
| The book | `tw_position` — the sleeve's own source of truth; it never sells what it did not buy |
| "Plan → confirm" | The desk's existing shape: a `TwtPlan` gets a `plan_id`, expires in 30 minutes, `POST /twt/execute confirm=true` per line |

## What this run is not

It is not a new order path (law 2 stands; the web app gets no order route; the desk console is the
only place a TWT line becomes an order). It is not shorting, not leverage, not intraday, not F&O.
**It is not auto-execution:** non-negotiable 1's one named exception belongs to the *swing* sleeve
by Maulik's own hand, and this run neither widens it nor adds a second — no `BASKFY_TWT_AUTO_*`
flag exists and none is created. It is not a promise that the method works: `05`'s pages carry
STRATEGY §5's caveats verbatim, and 164 trades in nine years with ten of them carrying half the
profit is a wide confidence interval, not a fact.

And it is not a deploy. The box and `tools/deploy/` are Maulik's; this run ends at a green tree,
the runbook and a report.
