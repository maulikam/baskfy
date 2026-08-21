# benchmarks/ — measuring docs/11's performance budgets

Prompt 16. Everything here exists to answer one question honestly: **what are the numbers, and
what were they measured against?**

```
make up                                     # postgres + redis
export BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test
export BASKFY_REDIS_URL=redis://localhost:6380/0

make bench                                  # every server-side budget -> AS-MEASURED.md
make web-build && make bundle               # the client-JS budget
make plans                                  # EXPLAIN ANALYZE every hot query
make loadtest URL=http://localhost:8000     # 50 concurrent screen runs against a live server
```

## What is in here

| File | What it is |
|---|---|
| `budgets.py` | docs/11 §"Performance budgets" transcribed as data, and the recorder every benchmark writes through. **The only place a budget number is written down on the Python side.** |
| `report.py` | Renders `AS-MEASURED.md`. `--check` fails on a missed *or* an unmeasured budget. |
| `load_screens.py` | The concurrent load driver. Used by `services/api/tests/test_load.py` over an ASGI transport and by `make loadtest` over real HTTP. |
| `AS-MEASURED.md` | Generated. The "as measured" column Prompt 16 asks to be added to docs/11. |
| `results/` | One JSON file per measurement, written by the benchmarks. Not committed. |

The benchmarks themselves live next to the code they measure:

| Budget | Measured by |
|---|---|
| Screen run, warm / cold | `services/api/tests/test_api_benchmark.py` |
| Dashboard, factsheet TTFB, CSV export | `services/api/tests/test_benchmarks.py` |
| 50 concurrent screen runs | `services/api/tests/test_load.py` |
| Backtest (15y, monthly, 20 names) | `packages/core/tests/test_backtest.py` |
| `compute_factors` | `packages/core/tests/test_factor_benchmark.py` |
| Factsheet LCP | `apps/web/e2e/performance.spec.ts` |
| Screens-route JS | `apps/web/scripts/bundle-budget.mjs` |

## Why the table is not in docs/11

Prompt 16's first acceptance criterion asks for the numbers "written into docs/11 as an
'as measured' column". The overnight working agreement this was built under forbids editing
anything under `docs/` except appending to `docs/DECISIONS.md`, so the column is rendered here
instead. **Paste it in.** `docs/DECISIONS.md` §16.1.

## What is not measured, and why

* **Nightly pipeline end-to-end (< 45 min).** Nine of docs/03's ten steps are network fetches
  against Kite and NSE, and the suite is network-blocked (`network_guard.py`). The tenth —
  `compute_factors` — is measured, on a synthetic panel, with no database on either side of it.
  A real number needs a staging run against real providers. Until then this row reads
  **not measured**, which is the truth.
* **Anything at production scale.** The seeded database is docs/13's single-date, 271-row export.
  Where a budget is stated for a bigger input (the CSV export's 4,000 rows), the benchmark builds
  a synthetic universe and says so in the "Measured against" column. Where it is not (the
  dashboard, the factsheet), the number is for 271 instruments and 117 indices and should be read
  as a floor.
* **Production latency.** Every server-side benchmark runs in-process over an ASGI transport: no
  socket, no uvicorn worker pool, no reverse proxy, one event loop. `make loadtest` against a
  running server is the closer measurement and is the one to take before believing any of this in
  a deployment.

## The load test

`services/api/tests/test_load.py` asserts Prompt 16's second acceptance criterion — 50 concurrent
screen runs, p95 < 400 ms, no errors — in CI, in-process.

Against a real server:

```
make up && make migrate && make seed
make api &
make loadtest URL=http://localhost:8000
```

It exits non-zero if the p95 misses or anything errors, so it can be wired into a smoke test after
a deploy.
