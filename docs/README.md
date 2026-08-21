# Baskfy — merge documentation

Two products, one thesis. This folder is the working record of how `decile-blueprint` and
`kite-momentum-rebalancer` become a single system, and what has to be true at each step.

Written 21 Aug 2026, from a full read of both trees. Nothing here has been built yet.
Docs 07–08 added the same day: an outside-world verification pass (registry, AWS, SEBI/Zerodha
rules as of today) and the AWS deployment design.

| Doc | What it is |
|---|---|
| [`01-decile-blueprint.md`](01-decile-blueprint.md) | Product A understood — the screener/data plant |
| [`02-momentum-desk.md`](02-momentum-desk.md) | Product B understood — the live execution desk |
| [`03-merge-analysis.md`](03-merge-analysis.md) | **The seam.** Where they join, where they collide, what is redundant |
| [`04-target-architecture.md`](04-target-architecture.md) | What the merged system looks like |
| [`05-merge-plan.md`](05-merge-plan.md) | **The task list.** Seven phases, every task with an acceptance criterion |
| [`06-decisions-required.md`](06-decisions-required.md) | The nine things only you can decide, and what each one blocks |
| [`07-feasibility-study.md`](07-feasibility-study.md) | **The outside world checked.** Name cleared, stack verdicts, and the 2025–26 SEBI/Zerodha/data-licensing rules the plan must obey |
| [`08-aws-architecture.md`](08-aws-architecture.md) | The AWS design — Mumbai, two phases: a $50/mo box through P3, the managed topology from P4 |

## The one-paragraph version

`kite-momentum-rebalancer` is a working, deployed, order-placing momentum desk whose brain
(`app/scoring.py`) reads a 30-column CSV that a human downloads by hand, every week, from
**momoindiascreener.in** — a competitor's website. `decile-blueprint` is a from-scratch,
spec-driven, numerically-verified clone of momoindiascreener.in that produces **exactly that
CSV** (93 columns; the desk's 30 are a strict subset). The two products are already joined —
by a manual download from a third party. The merge is to replace that download with an
internal query, and then to notice that what you are left with is a complete
screen → rank → construct → size → execute → monitor → rebalance loop that nothing in the
Indian market currently sells end-to-end.

## The one thing that decides whether this works

Decile has never seen real market data. Its own `CLAUDE.md` says so in capitals: no backfill
has run, the decisive 271-row parity test **skips**, the NSE URL shapes are unverified, and the
seeded calendar is ~9 lunar holidays a year short — which makes its factor windows
22/67/127/191/256 where the reference product's are 22/64/121/185/247.

The desk hands you the oracle for free. `data/uploads/` holds real scan CSVs from the reference
product, and a live portfolio that was scored, ordered and stopped off them. So Phase 1's
acceptance test writes itself: run Decile's pipeline for the same date, feed both files into the
desk's own `scoring.py`, and diff the top 25. That is not a new rule — it is the rule the desk's
`CLAUDE.md` already imposes on anyone touching scoring.

**Until that diff is clean, nothing else in this plan should be started.**
