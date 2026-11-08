# Baskfy — Build Blueprint

A complete, implementation-ready specification for building **Baskfy**, an India-equities
momentum screener modelled on **momoindiascreener.in**, plus the exact, ordered set of prompts to
feed Claude Code so it builds the system one module at a time.

Prepared: 19 Aug 2026.

## How to use this bundle

1. Read `docs/01-product-teardown.md` — what the reference product actually does, page by page,
   filter by filter, including the factor formulas that were reverse-engineered and
   **numerically verified** against live data.
2. Read `docs/02-tech-stack-adr.md` — the locked technology decisions. Do not re-litigate these
   mid-build; the prompts assume them.
3. Read `docs/13-csv-export-schema.md` — a real 271-row export from the reference product,
   decoded. It pins the exact window lengths, storage precision and internal schema, and it is
   the acceptance test for the factor engine.
4. Skim `docs/03` … `docs/12` — these are the reference specs the prompts point Claude Code at.
5. Open **`PROMPTS.md`** and run the prompts **in order**, one per Claude Code session (or one
   per `/clear`). Each prompt is self-contained, states its inputs, its deliverables and its
   acceptance criteria.

## Contents

| File | What it is |
|---|---|
| `PROMPTS.md` | **The main deliverable.** 22 sequential build prompts (0–21), module by module. |
| `docs/01-product-teardown.md` | Full reverse-engineering of momoindiascreener.in |
| `docs/02-tech-stack-adr.md` | Locked stack + rationale + rejected alternatives |
| `docs/03-architecture.md` | Services, repo layout, data flow, deployment topology |
| `docs/04-data-model.md` | Postgres schema, DDL, indexes, partitioning |
| `docs/05-factor-formulas.md` | Exact math for every factor, with verified worked examples |
| `docs/06-screener-semantics.md` | Filter pipeline order, decile logic, multi-factor ranking |
| `docs/07-api-spec.md` | REST contract for the FastAPI service |
| `docs/08-ui-spec.md` | Every screen, component, state and interaction |
| `docs/09-data-pipeline.md` | Kite + NSE ingestion, adjustments, scheduling, QA gates |
| `docs/10-backtest-spec.md` | Point-in-time backtest engine design |
| `docs/11-nonfunctional.md` | Performance, security, compliance, legal |
| `docs/12-parity-matrix.md` | Feature-by-feature parity checklist to track completion |
| `docs/13-csv-export-schema.md` | The reference product's 93-column CSV export, decoded — and what it proves |
| `fixtures/reference-screen-export-2026-08-18.csv` | 271-row answer key from the real product; the decisive acceptance test |
| `docs/14-brand-and-naming.md` | The name, the trademark checklist, and the product vocabulary |

## The one-line summary of the product

> Pick a universe (an NSE index) → rank every stock in it by a momentum factor (or a blend of
> up to three) → apply a long list of quality/liquidity/risk filters → show a sortable,
> exportable table → let the user save that configuration as a reusable "screen", re-run it on
> any historical date, and backtest it.
