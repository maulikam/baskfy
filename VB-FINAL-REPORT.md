# VBT-1 — the volume-breakout sleeve. Final report.

**Written 11 Sep 2026, at the end of the VB run.** Eleven modules, VB0 through VB10, all green.
The sleeve detects, plans, prices, sizes, expires, alerts and reports — and it has never placed an
order, because `BASKFY_VBT_EXECUTION_ENABLED` is false and nothing in this run touched it.

## Where it stands

Every number was measured on 11 Sep 2026 by the command beside it, from the repository root
unless said otherwise.

| | | measured by |
|---|---|---|
| Modules | **VB0 → VB10**, all ✅ | `sed -n '/^## Module ledger/,/^States/p' docs/vbt/STATUS.md \| grep -c '^| VB'` |
| Decile suite (api + core + worker) | **6,055 passed, 4 skipped**, 21 min | `cd decile-blueprint && BASKFY_TEST_DATABASE_URL=… uv run pytest services/api/tests packages/core/tests services/worker/tests -p no:randomly` |
| Desk suite | **1,852 passed, 17 skipped**, 72 s | `cd kite-momentum-rebalancer && uv run pytest tests -q -p no:randomly` |
| Web suite | **2,205 passed** | `cd decile-blueprint/apps/web && pnpm exec vitest run` |
| Lint + format + types | clean | `cd decile-blueprint && uv run ruff check . && uv run ruff format --check . && uv run mypy packages/core services/api services/worker` |
| The study, reproduced | **761 / 761 trades**, worst price gap ₹0.0000 | `uv run pytest packages/core/tests/test_vbt_goldens.py` |
| The three books | `full` 18.23% / −27.94% · `gate_off` 18.48% / −48.78% · `raw_scan` 0.76% / −48.10%, in 23 s | `uv run python tools/vbt/backtest.py` |
| `GET /vbt/today` p95 | **140.7 ms** over 2,500 rows (budget 300 ms) | `uv run pytest services/api/tests/test_api_vbt_benchmark.py` |
| Orders reaching a broker | **0** | `python tools/vbt/drill.py --database-url <scratch>` |

Read this with `docs/vbt/STATUS.md` (what is done, module by module, loud about what is not) and
`docs/vbt/DECISIONS-VB.md` (every judgement call, numbered, tagged ⚠ UNREVIEWED).

---

## 1. What was built

A third sleeve beside the weekly momentum book and the swing book, sharing their machinery and
none of their rules.

| | |
|---|---|
| **The rules, pure** | `baskfy_core.vbt` — eleven modules, 43 named thresholds, no literal in a detector. DataFrames in, DataFrames out; law 1 asserted over the source |
| **The data** | Twelve `vb_` tables, migration `0037_vbt`, generated from the models and verified against them by Alembic's own comparison |
| **The night** | `compute_vbt` as the chain's thirteenth step (it cannot fail the night), a 21:10 belt that asks before it works, and the 21:15 evening that writes the plan |
| **The desk** | `/vbt` — three panels, one form per line, no "confirm all". Every confirm goes through a real `OrderGateway` with its own journal |
| **The web** | `/vbt`, `/vbt/book`, `/vbt/backtest` — read-only, and with **no server actions at all** |
| **The alarm** | Four `VBT_*` alerts at 21:30 and 21:40, runbook 8 |
| **The evidence** | The study reproduced to the paisa, and re-runnable from the plant's own bars with drift flagged |

### The one number that matters

**761 of 761 trades reproduced**, from the same bars, on symbol, entry, exit, quantity and reason,
with a worst price gap of ₹0.0000. CAGR 18.23%, drawdown −27.94%, the yearly table to one decimal.
The production core and the research script agree completely, which is what makes every other
number in this report worth reading.

---

## 2. What was decided

Thirty-eight numbered decisions are in `DECISIONS-VB.md`. The six that would be expensive to
re-derive:

1. **VB0.2 — the six-filter reading is the right one.** `grid2.py` still carries an eight-filter
   mask the research note dropped. The six-filter reading reproduces STRATEGY exactly; the
   eight-filter one does not. Settled by measurement, not by reading.
2. **VB2.1 — `limit × 1` is not a no-op.** A bar price converted from a float carries about fifty
   significant digits; Decimal rounds a product to twenty-eight. That pushed a limit a hair above
   the low that touched it and cost 589 of the 761 trades. Yesterday's close *is* today's low
   often enough to matter — it is what a pullback to the previous close looks like.
3. **VB7.1 — the three-session window is the parameter with a cliff.** Two sessions returns 11.4%
   a year where three returns 18.2%. The window is a config field and the property test runs at
   1, 2, 3, 5 and 10, so nothing in the suite compares against a literal three.
4. **VB8.1 — `04` §7.3 carried an invented 91% fill rate from VB0 until VB8 measured it.** It is
   **83.8%**: 761 fills out of the 908 orders the engine actually offered a fill test. Over all
   5,954 working orders it is 12.8%, but most of those never got a slot, so that figure measures
   the slot count rather than the market.
5. **VB9.2 — the gate costs return and buys drawdown.** Measured over the full history: `full`
   18.23% at −27.94%, `gate_off` 18.48% at −48.78%. The gate is worth **minus a quarter of a CAGR
   point and 21 points of drawdown**. Anyone reading the ablation's CAGR column alone will
   conclude the gate is useless.
6. **VB10.1 — the drill found a bug 43 tests had missed.** The desk wrote `vb_position.order_id`,
   a column that has never existed. Every test of that path used an in-memory store that accepts
   any key. A fake that accepts anything tests the caller, not the contract.

---

## 3. What is NOT done

Nothing here is a surprise; all of it is in `STATUS.md` under its module.

**Nothing has ever run outside a test.**
* No order has been placed, simulated or otherwise, outside a test or the drill.
* No page has been opened in a browser. The web pages pass in jsdom against mocked reads; the
  desk page renders in the test suite's shape and nobody has looked at it.
* The nightly chain has never run with `compute_vbt` in it against real bars.
* No alert has been delivered. No sink is configured on any box this run touched.
* Runbook 8 says `**Verified against:** NOT YET`, like the seven before it.

**One thing I could not explain.**
* A golden failed once in five full sweeps and never again — three clean runs of the module and a
  clean 6,055-test sweep since. The output was truncated before the assertion was captured, so I
  do not know which number moved. It is recorded in `STATUS.md` under VB10 rather than left out,
  because this suite is the run's central evidence and a golden that is not deterministic is not
  a golden.

**Measurements that are missing rather than bad.**
* The full-history backtest has never run **against the plant**. The engine does all three books
  in 23 seconds from the research export; the `ohlcv_daily` read for 4,186 names over nine years
  is unmeasured, because no database this run could reach holds that history. VB9's "under 30
  minutes" is therefore *made likely*, not met. One command settles it — `NEEDS-MAULIK.md` V6.
* The DRY_RUN session counter stands at **zero of twenty**.

**Spec gaps, recorded rather than quietly dropped.**
* `05` §2's per-row **Dismiss** note does not exist: `03` has no table to put a note in, and the
  hub is stronger for having no writes at all (VB8.4).
* `AlertName.VBT_EVENING`, the nightly digest email, does not exist. The four condition alerts do.
* An open position's 21-EMA and its distance come back null: the read layer does not recompute
  indicators and `vb_position` does not store them.
* The monthly table of `04` §11 is not stored. The yearly table and the equity curve are.

**Known holes in the data, which are the plant's and not this run's.**
* 262 missing instrument-days and the six thin sessions (`NEEDS-MAULIK.md` V2).
* Corporate actions before 2024 are as the source adjusted them.

---

## 4. What needs Maulik

In `NEEDS-MAULIK.md` under **VBT**, and nothing there can be done by an agent:

| | |
|---|---|
| **V1** | The risk decision, in writing. Blocks the real-money gate and nothing before it |
| **V2** | The 262 missing instrument-days and the six thin sessions |
| **V3** | `BASKFY_VBT_EXECUTION_ENABLED`. **The swing sleeve's delegation does not extend here** — no agent touches this flag without a line from you first |
| **V4** | Twenty DRY_RUN sessions, or the shorter gate you gave the swing book |
| **V5** | Deploys. This run deployed nothing; the box does not know this sleeve exists |
| **V6** | One timing: how long `make vbt-backtest` takes against the real plant |

---

## 5. The first DRY_RUN morning, exactly

Nothing below can place an order. Every step is safe to stop after.

**The evening before (any weekday after the chain has published).**

```
# 1. The sleeve needs its schema and its money.
cd decile-blueprint && make migrate                 # 0037_vbt onwards
#    Then set the sleeve's capital — it is seeded at 0 and plans nothing until you do:
#    PATCH /api/v1/vbt/config  {"sleeve_capital_inr": "1000000.00"}
#    or on the desk's settings surface. 0 is deliberate: a sleeve with no money is a sleeve
#    that cannot trade by accident.

# 2. Detect the last published session, and read the funnel.
make vbt DATE=<the last published trading day>
#    Expect: universe -> with a bar -> scan hits -> signals, and a vb_breadth_daily row.
#    0 signals is a normal night. 0 signals AND no breadth row means it did not run.

# 3. Build the evening plan. It places nothing.
make vbt-plan DATE=<the same day>
#    Expect: entries=N sells=0 cancels=0 arms=0, gate=OPEN|SHUT, every line PROPOSED.
```

**The morning.**

```
# 4. Rebuild the plan before the open — last night's cannot be confirmed at 09:20,
#    because a desk plan expires in thirty minutes.
make vbt-plan DATE=<the signal session> SOURCE=MORNING

# 5. Open the desk at /vbt. Check the status bar first:
#      DRY_RUN true, execution flag false, "N of 20" DRY_RUN sessions.
#    If the flag is not false, stop and do not continue.

# 6. Confirm one line. One click, one line — there is no "confirm all".
#    Expect the outcome SIMULATED and the row labelled simulated in the book.
#    A GTT is armed in the same request as the fill it belongs to.
```

**The check.**

```
# 7. Read the book at /vbt/book: the position, its stop, GTT armed (not "naked"),
#    and the working limits with "N of 3 sessions".
# 8. Read the journal: kite-momentum-rebalancer/data/vbt_orders_journal.jsonl
#    Every line should say dry_run. If any line says an order id from Zerodha, stop everything.
```

**Before any of that, once, to see the whole thing move:**

```
python tools/vbt/drill.py --database-url postgresql+asyncpg://.../a_scratch_database
```

It creates its own schema, plans a session, confirms a line through the real gateway, runs the
expiry sweep, prints the counters and drops the schema. It should end with `calls: 0` and
`0 orders reached a broker.`

**What to watch over the twenty sessions.** The fill-rate line on `/vbt/book`. The study modelled
83.8%; if this book comes in far below that, the limits are not being reached and the strategy's
returns will not follow the study whatever else is right. That line is the earliest honest warning
available.

---

## 6. The safety posture, in one paragraph

`BASKFY_VBT_EXECUTION_ENABLED` is false and no agent has ever set it. There is **no auto-execute
flag for this sleeve and none was added** — non-negotiable #1's named exception belongs to the
swing sleeve alone, and a source scan asserts the absence across both trees, with a test proving
the scan would catch a real one. The web app has no execute route and no server actions at all.
Every order-shaped action needs `confirm=true`, a `plan_id` issued in the last thirty minutes, and
a line still `PROPOSED`. Track C held throughout: no shorting, no MIS or F&O, no margin, no
auto-execution, no web-app orders, and no change to the swing rules or the weekly book — the swing
package is pinned by SHA-256 and the desk's suite is green at 1,852 passed.

---

**Maulik — this is ready for your review.** Start with `docs/vbt/STATUS.md` §"What is NOT done" at
each module, then `NEEDS-MAULIK.md` § VBT, then the six decisions in §2 above. The one thing I
would most like a second opinion on is **VB8.1**: I replaced a number that had been in the
contract document since VB0 with a measured one, and the denominator I chose for it is a judgement
call that changes what the page's early-warning line means.
