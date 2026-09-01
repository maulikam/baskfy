# 03 — Parity and gates

The whole sprint is graded the way the merge was: against an answer key. Three keys exist
already — `kite-momentum-rebalancer/data/uploads/*.csv` (the desk corpus), the 236 Python test
files, and `openapi.json`. The fourth is manufactured tonight: **goldens**.

## The golden harness

**Principle:** an agent porting a function should never have to *reason* about whether its Go
matches Python. It runs a test that diffs against what Python produced.

```
tools/parity/
  golden.py            # canonical JSON dumper (Decimal→"12.34", date→"2026-08-30", DataFrame→{columns,rows}, NaN→null, sets→sorted lists)
  dump_L1.py …         # per lane: import the Python function, run it on fixtures/corpus, write go/testdata/golden/L<n>/<module>/<case>.json
go/testdata/golden/L<n>/<module>/<case>.json
  { "fn": "baskfy_core.score.score_frame", "inputs": {...}, "output": {...}, "meta": {"dumped_at": ..., "known_bug": null | "DECISIONS.md §21.9", "tolerance": {"float": 1e-9} } }
go/internal/testkit/golden.go
  LoadGolden(t, "L1/score/case_001.json") → inputs + expected; Diff(expected, got, tol)
```

Rules:

- Dumpers run with the tree's own venv (`decile-blueprint/.venv/bin/python` or
  `kite-momentum-rebalancer/.venv/bin/python`) and **import** Python; they never modify it.
- Inputs come from existing fixtures (`packages/core/tests/fixtures`, `packages/providers/…/fixtures`,
  `tests/fixtures` in the desk, the read-only corpus). No network, no live DB — if a Python
  function needs a DB, dump the *rows* it read as part of `inputs`.
- One case file per (function, fixture). Aim for the cases the Python tests already assert —
  port the *test intent*, not the test file.
- Goldens are committed. Regenerating one is a commit of its own that says why.

## Tolerances

| Kind | Rule |
|---|---|
| Money, prices, quantities, anything that reaches a `numeric` column | **exact** after rounding at the same place Python rounds (`precision.py`) |
| Ranks, buckets (`decile_1…6`), membership, flags, plan lines | **exact** |
| Intermediate factor floats (returns, vol, RSI, z-scores) | `|a-b| ≤ 1e-9 · max(1, |b|)`; NaN matches NaN |
| Backtest metrics (CAGR, Sharpe, drawdown) | 1e-6 relative — the Python computes in float64 through pandas, whose reductions are pairwise; document any case needing more |
| JSON API bodies | same status, same keys, same value after JSON normalisation (numbers compared as decimals, arrays order-sensitive unless the spec says otherwise) |
| Timestamps | equal to the second in IST |

A test that needs a looser tolerance is a `DECISIONS-GO.md` entry, not a silent edit.

## The gates

| Gate | When | Passes when |
|---|---|---|
| **G0** | T+0:45 | `go/main`: `make build lint test` green; `internal/domain` frozen; `laws_test` passing; each lane has its `status/L<n>.md` inventory and ≥1 golden committed on its branch. |
| **G1** | T+2:30 | Per lane: *must* scope compiles, golden tests pass, `make lint` clean, merged into `go/main`, `go/main` still green. **L1 specifically:** the 29 desk columns and 93 screener columns over the corpus are exact. **L4 specifically:** the seven non-negotiables tests pass and `ErrLiveOrdersDisabled` is the only thing a live order path returns. |
| **G2** | T+4:30 | `cmd/api` on :8001 against the dev Postgres/Redis; `apps/web` pointed at it via `NEXT_PUBLIC_API_URL=http://localhost:8001`; Playwright `screens`, `portfolios`, `market`, `nav`, `instrument` specs green; `cmd/worker` runs `baskfy.pipeline.nightly` end-to-end in DRY_RUN against a **copy** of the DB (`pg_dump | psql baskfy_go_shadow`) with `pipeline_run_step` rows matching the Python run's golden. |
| **G3** | T+6:00 | `cmd/desk` on :8421 in DRY_RUN produces, for the same holdings snapshot + scan CSV, a plan whose lines equal the Python desk's plan (`scripts/friday_drill.py` output as golden). `STATUS.md` complete and honest; `DECISIONS-GO.md` has every judgement call; `NEEDS-MAULIK.md` has every blocker. `go/main` merged into `developer`. |
| **G4** | *not tonight* | Two shadow Fridays (Go desk in shadow beside Python, plans diffed, zero deltas); Caddy route-group cutover in order `meta → screens → explore → instruments → portfolios → backtests → admin → alerts → brokers → cb → auth`; worker cutover per schedule; desk cutover last, after Maulik's explicit go; Python retired one service at a time; Alembic → goose baseline. |

## Definition of done, per Go file

A `<stem>.go` is done when: it has a `<stem>_test.go` whose cases are goldens or ported test
intent; `make lint` is clean; every exported identifier has a doc comment that names the Python
origin (`// Score ports baskfy_core.score.score_frame.`); any divergence from Python is a
`DECISIONS-GO.md` entry referenced from the code comment; and `status/L<n>.md` lists it as ✅.

## Cutover rules (strangler mechanics, for L0 and for the next evening)

- Go services bind to **different ports** (`:8001` api, `:8421` desk) and read the **same** DB
  and Redis. Redis key formats and DB rows written by Go must be indistinguishable from
  Python's — that is what lets the two coexist and lets a route move back with one Caddy line.
- Route groups move in the order above; each move is one Caddy `handle_path` block and one line
  in `STATUS.md`; moving back is deleting that block.
- Worker: Go's scheduler takes an entry only when the Celery Beat entry is **disabled** for it
  (never two schedulers on one job); the swap is one config line each side.
- Desk: never before G4; `desk.modelbasket.in` keeps pointing at the Python desk until then.
