# Baskfy — working agreement

> **Part of [Baskfy](../CLAUDE.md).** The root `CLAUDE.md` governs — it carries the two laws,
> the desk's seven non-negotiables and the screener's nine house rules. This file remains
> authoritative for this tree's internals.

India-equities momentum screener. `docs/` is the **source of truth**. If your instinct conflicts
with it, say so out loud rather than deviating quietly.

## House rules

1. Read `docs/02-tech-stack-adr.md` before proposing any dependency. Nothing outside the locked
   stack without saying why first.
2. Tests assert the **spec**, never current behaviour. If a test would only lock in what the code
   happens to do today, it is not worth writing.
3. No `# type: ignore`, no `any`, no silently swallowed exceptions.
   `packages/core/tests/test_no_escape_hatches.py` enforces this by scanning the source.
4. Every module ends with: tests passing, `make lint` clean, and a `docs/` update if behaviour
   diverged from the spec.
5. **No look-ahead, ever.** Anything referencing a past date uses point-in-time index membership
   and point-in-time factor rows. Asserted by tests, not by discipline.
6. **Adjusted by default.** `close` is adjusted; `close_raw` is the exchange print. Factors read
   `close`; display uses `close_raw` where the user expects a real price.
7. **Idempotent ingestion and seeding.** Re-running any day's job produces identical rows.
8. **Round at write time.** Storage precision is the contract (`docs/13` §4), so the API, the UI
   and the CSV export can never disagree.
9. Money and prices are `numeric`, never `float`. Disclaimers are components, not footers.

## Stack

Locked by `docs/02-tech-stack-adr.md`; installed versions are in `pyproject.toml` and
`apps/web/package.json`. House rule 1 applies — read the ADR before proposing a dependency.

## Layout

`packages/core` is deliberately I/O-free — DataFrames in, DataFrames out. Anything that
touches a database, a network or a disk belongs in `services/` or `packages/providers`.

## Where things live

Only the entries a `grep` will not tell you — design intent, deviation notes, and the
decision log. For anything else, search the tree.

| Looking for | It is in |
|---|---|
| **The screener query builder (pure)** | `packages/core/src/baskfy_core/screener.py` |
| Screener deviations from `docs/06` | `docs/06a-screener-implementation-notes.md` |
| **Argon2id, OTP, opaque tokens** | `services/api/src/baskfy_api/security.py` |
| Auth deviations from `docs/07`/`docs/11` | `docs/12a-auth-implementation-notes.md` |
| **The entitlement service (one place, server truth)** | `services/api/src/baskfy_api/entitlements.py` |
| **Every Prompt 13 decision taken under ambiguity** | `docs/DECISIONS.md` §13 |
| **The rank-buffer rebalance rule (pure)** | `packages/core/src/baskfy_core/rebalance.py` |
| **Every Prompt 14 decision taken under ambiguity** | `docs/DECISIONS.md` §14 |
| **The backtest engine (pure, point-in-time)** | `packages/core/src/baskfy_core/backtest.py` |
| **Every Prompt 15 decision taken under ambiguity** | `docs/DECISIONS.md` §15 |
| **The docs/11 budget table, and what each was measured on** | `benchmarks/AS-MEASURED.md` |
| **Every Prompt 16 decision taken under ambiguity** | `docs/DECISIONS.md` §16 |
| **The five runbooks (start here at 3am)** | `docs/runbooks/` |
| **Every Prompt 17 decision taken under ambiguity** | `docs/DECISIONS.md` §17 |
| **The per-package coverage gate (core >= 90%, api >= 80%)** | `tools/coverage_gate.py` |
| **Every Prompt 19 decision taken under ambiguity** | `docs/DECISIONS.md` §19 |
| **The public route table (footer, sitemap, static-CSP predicate)** | `apps/web/src/lib/marketing/routes.ts` |
| **The four legal drafts, and the review checklist** | `apps/web/src/content/legal/` |
| **Every Prompt 18 decision taken under ambiguity** | `docs/DECISIONS.md` §18 |
| **The public-API compliance gate (docs/11 §Compliance, as a constant)** | `packages/core/src/baskfy_core/public_api.py` |
| **The screen diff behind every alert email (pure)** | `packages/core/src/baskfy_core/screen_diff.py` |
| **Every Prompt 20 decision taken under ambiguity** | `docs/DECISIONS.md` §20 |
| API deviations from `docs/07` | `docs/07a-api-implementation-notes.md` |
| **Design tokens (Tailwind v4 has no config file)** | `apps/web/src/app/globals.css` |
| Web deviations from `docs/08` | `docs/08a-web-implementation-notes.md` |
| Screens-UI deviations from `docs/08`/`docs/01` §2 | `docs/09a-screens-ui-implementation-notes.md` |
| Factsheet deviations from `docs/01` §5 | `docs/10a-instrument-factsheet-notes.md` |
| Market-surface deviations from `docs/01` §6-7 | `docs/11a-market-surfaces-notes.md` |
| **The factor engine (pure)** | `packages/core/src/baskfy_core/factors.py` |
| **Market-health breadth (pure query)** | `packages/core/src/baskfy_core/breadth.py` |

## Commands

`make help` lists every target with a one-line description. Two that matter and are
easy to get wrong: `make test` skips the database-backed tests, `make test-db` runs
only those and needs `BASKFY_TEST_DATABASE_URL` exported.

## Open items carried forward

The full list lives in [`docs/OPEN-ITEMS.md`](docs/OPEN-ITEMS.md) — read it before
claiming any part of the pipeline works. It is the honest record of what has never run.

