# AGENTS.md — Baskfy

Entry point for any coding agent working in this repository. **`CLAUDE.md` at this root is the
authoritative working agreement** — it carries the two laws, the desk's seven non-negotiables,
the screener's nine house rules, the safety rails, and the autonomy charter. This file is the
standard-named adapter so that agents which look for `AGENTS.md` find the same regime; it
summarizes and points, it does not replace. **Where this file and `CLAUDE.md` disagree,
`CLAUDE.md` wins — and this file should be fixed.**

**Read order for any agent session:** this file → `CLAUDE.md` → `docs/README.md` →
`MERGE-PROMPTS.md`, and the docs they cite as you reach them. `RUN-AND-TEST.md` is the
from-scratch guide to running and testing everything. Each sub-tree's own `CLAUDE.md`
(`decile-blueprint/CLAUDE.md`, `kite-momentum-rebalancer/CLAUDE.md`) remains authoritative for
that tree's internals.

## What this repository is

**Baskfy** merges two products into one: `decile-blueprint/` (the momentum screener / data
plant — Python 3.12 uv workspace + TypeScript/Next.js, PostgreSQL 16 + TimescaleDB; has never
placed an order and never will) and `kite-momentum-rebalancer/` (the live momentum execution
desk — FastAPI + Kite Connect on NSE cash equities; the only thing here that can place an
order). They share `packages/core` — factors, scoring, basket construction. `frozen/strangle/`
is a frozen options lab: no refactors, no deletions, no lint fixes. `gates/` holds
feature-gate briefs; `docs/` holds the merge record (00–08, `DECISIONS-MERGE.md`).

## Hard rules (from CLAUDE.md — read it for the full set)

These are the ones that are catastrophic to miss. They are not advisory.

1. **Never place a live order. Ever.** `DRY_RUN=true` is the default in every environment an
   agent creates; never run execution tests against live credentials. Orders fire only from
   `POST /execute` with `confirm=true` and a `plan_id` from `/analyze` — and never from an agent.
2. **`kite-momentum-rebalancer/data/portfolio.db` is unrebuildable evidence.** Before anything
   that could touch it: verified backup (`python -m scripts.backup` says `ok`) plus a dated
   copy outside the repo. Migrations run against a copy first. The file is archived forever.
3. **`kite-momentum-rebalancer/data/uploads/*.csv` is the read-only regression corpus** — the
   answer key the merge is graded against. Never modify it.
4. **The two laws:** `packages/core` touches nothing (no DB, network, disk, or clock — I/O
   lives in `services/` or `packages/providers`); `packages/execution` is the only path to an
   order (guards → risk → rate limit → journal → broker; nothing calls `kc.place_order`
   directly).
5. **Never print, log, or commit secrets.** `.env` files and `data/` stay untracked.
6. **Never weaken a test to make something pass.** Tests assert the spec, not current
   behaviour. No `# type: ignore`, no `any`, no swallowed exceptions (enforced by
   `packages/core/tests/test_no_escape_hatches.py`).
7. **The desk must be able to rebalance on any Friday.** Don't stop for the day with
   `kite-momentum-rebalancer`'s suite red.
8. **No look-ahead** in anything referencing a past date (point-in-time membership and factor
   rows). One known violation exists (`apply_adjustments`, docs/DECISIONS.md §21.9, M10's to
   fix) — do not add a second.
9. Network calls go through the existing rate-limited providers only (Kite ~3 req/s
   historical; NSE with its cookie/header discipline). No scraping around them.
10. The public API stays shut (D9); Track B flags stay false; the web app never gains an
    execute route. Paid multi-tenant launch waits on counsel (C3) — human-track, not yours.

## Commands

Run each suite from its own directory — `pytest` at the repo root walks into both trees and
fails confusingly.

```bash
# decile-blueprint/ (uv + pnpm; `make help` lists everything)
make up            # postgres + redis + mailpit
make test          # python + TS suites          make lint   # ruff + mypy strict + TS
uv run pytest -p no:randomly
pnpm -r run test

# kite-momentum-rebalancer/
.venv/bin/python -m pytest

# repo root
tools/ci-local.sh          # CI's steps, locally; honest about skips
tools/check-namespace.sh   # no `decile` namespace tokens outside the ALLOWED vocabulary
```

Dependencies: read `decile-blueprint/docs/02-tech-stack-adr.md` before proposing anything
outside the locked stack. Money and prices are `numeric`, never `float`. `close` is adjusted,
`close_raw` is the exchange print. Ingestion and seeding are idempotent; round at write time.

## Working style

- **Autonomy:** run long; questions to Maulik are the exception. When a stop-and-ask would
  happen: decide, record it in `docs/DECISIONS-MERGE.md` tagged `⚠ UNREVIEWED`, continue.
  Precedence when rules conflict and what still stops work (credentials, unrebuildable data
  without backup, live orders, launch gates, red-gate parity): see `CLAUDE.md`, "Autonomy
  charter". Things only Maulik's hands can supply go in `NEEDS-MAULIK.md`.
- **Commits:** one per module, message `M<N>: green — <one line of what changed>`, written in
  complete sentences, matching the repos' habit.
- **Status:** `docs/00-merge-status.md` is the live status page — updated at the end of every
  module, loud about what is NOT done. Judgement calls go in `docs/DECISIONS-MERGE.md`.
- **Naming (D1):** namespaces `baskfy_core` / `baskfy_api` / `baskfy_worker` /
  `baskfy_providers` / `baskfy_execution`, TS `@baskfy/*`, env prefix `BASKFY_`, database
  `baskfy`. `decile` survives only as domain vocabulary (D1 bucket, decile drift) per the
  `ALLOWED` list in `tools/check-namespace.sh`.
