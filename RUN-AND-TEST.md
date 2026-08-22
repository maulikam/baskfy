# Baskfy — run it, and test it

One document, from a fresh checkout to a running, testable system. Everything below has been
executed on this machine; where something has not, it says so.

Two products live here and they are not yet one process:

| | `decile-blueprint/` | `kite-momentum-rebalancer/` |
|---|---|---|
| what it is | the screener — bars, factors, scans, the Next.js app | the desk — plans, orders, stops |
| language | Python 3.12 (uv workspace) + TypeScript | Python 3.12 |
| database | PostgreSQL 16 + TimescaleDB | **the same Postgres**, `desk` schema (M19) |
| runs as | api + worker + beat + web | one FastAPI process with Jinja pages |
| can place an order | **no, and never** | yes, through `packages/execution` |

They share `packages/core` — the factor engine, the score, the basket construction and the
exposure overlay all live there and are imported by both.

---

## 1. From nothing to running

### Prerequisites

`docker`, `uv`, `pnpm`, and Python 3.12. Nothing else.

### The screener

```bash
cd decile-blueprint
cp .env.example .env               # the defaults work against the compose stack
make up                            # postgres + redis + mailpit, waits for health
make migrate                       # alembic to head — includes 0011, the 20-DMA breadth column
make seed                          # reference data + fixtures
make api                           # http://127.0.0.1:8000  (OpenAPI at /docs)
make worker                        # in another shell
make beat                          # and another — the schedule in docs/09, IST
make web                           # http://127.0.0.1:3000
```

`make doctor` reports what is up and what is not.

### The desk

```bash
cd kite-momentum-rebalancer
cp .env.example .env
# THE ONE LINE THAT MATTERS:
#   DRY_RUN=true    orders are simulated and journalled, nothing reaches Zerodha
#   DRY_RUN=false   orders are real
grep DRY_RUN .env

python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh                           # http://127.0.0.1:8420
```

The desk needs a Kite session for anything involving holdings, cash or prices. Log in from its
home page. **Kite tokens expire around 06:00 IST the next morning and cannot be refreshed** — that
is structural, not a bug (`NEEDS-MAULIK.md` item 3).

If the box has already logged in today, the laptop can borrow that token instead of logging in
again:

```bash
cd decile-blueprint
make token-sync TARGET=momentum-desk
```

It reads the token over the SSH connection `deploy/sync.sh` already uses, writes it into **both**
local encrypted stores — the desk's and the pipeline's, which are different files — and verifies it
with one `profile()` call. **The token is never printed.**

**Re-run it after *any* Kite login, not just the first of the day.** Minting a new token
invalidates the previous one, so a second login on the box silently kills the copy on the laptop. The
Kite app's Redirect URL stays `desk.modelbasket.in/callback`.

---

## 2. The nightly chain

```bash
cd decile-blueprint
make pipeline DATE=2026-08-21          # one date, end to end
```

Fire it for one date and watch it. What "published" looks like:

* `ohlcv_daily` has rows for the date (`make explain SYMBOL=CUPID DATE=2026-08-21 FACTOR=sharpe_12m`
  audits one factor cell all the way back to its inputs);
* `factor_daily` has a row per instrument;
* `market_health_daily` has twelve rows, one per universe, each carrying `pct_above_20dma`;
* the API's screen endpoints return the new date;
* `/ops` on the desk shows the run.

The Beat schedule that does this unattended is in `services/worker/src/baskfy_worker/celery_app.py`.
It also carries the desk's own two jobs (M19 §1) at 18:30 and 18:50 IST.

---

## 3. The Friday drill — the whole loop, zero orders

This is the acceptance test for the merged system, and it runs on a Sunday.

```bash
cd decile-blueprint
make friday-drill DATE=2026-08-18
```

It refuses to run unless `DRY_RUN=true`. Not a warning — a refusal, before anything is built,
because the difference between a drill and a real session is one environment variable somebody will
eventually have set for a real session and forgotten.

What it does:

1. **generate the scan** from the merged engine (no CSV, no website);
2. **build the plan** — `/analyze`, with live prices, the pledged-share flags, the funding check;
3. **review it** — buys, sells, pledged;
4. **execute it** — `/execute` with `confirm=true`, under `DRY_RUN`;
5. **preview the stops** — `/stops`;
6. **count the orders that reached a broker.** That number must be zero.

A real run, 22 Aug 2026:

```
Friday drill — 2026-08-18   DRY_RUN=True   STUB BOOK
  1. analyze            ok    plan 9cf46614a665, 13 orders
     scan               generated  0db6fe59674293e7
     WARNING            41 of 271 symbols carry an unadjusted corporate action: ...
     breadth            68.6347 from pipeline
  2. review             ok    3 buys, 10 sells, 4 pledged
  3. execute            ok    dry_run=True, 13 results {'DRY_RUN': 13}
  4. stops preview      ok    200
  5. orders that reached a broker: 0
DRILL GREEN   (STUB BOOK)
```

**"STUB BOOK" means there was no Kite session**, so the drill substituted the desk's last recorded
snapshot as the book. It prints that in the header and again in the verdict, because a green run
against a stub proves the machinery works and proves nothing about the real portfolio. With a live
token it says `LIVE BOOK` and the same six steps run against the real one.

The drill also refuses to call itself green if **nothing was even simulated** — an earlier version
reported GREEN over fifteen `RISK_BLOCKED` results, which is exactly the reassuring-but-empty
verdict a drill exists to prevent.

---

## 3a. The desk's surfaces on the web app (M22, M26)

Seven read-only pages, all in Next.js on `:3000`, grouped in the sidebar under **Desk**:

| | |
|---|---|
| `/baskets` | what the strategy wants to hold today — names, weights, scores, the six score components, and the stop each position would carry |
| `/baskets/plan` | the desk's most recent rebalance plan: every order, planned and filled quantity, the price each was actually done at |
| `/performance` | what the portfolio is worth, and how that compares to buying the benchmark |
| `/holdings` | every position held, its cost, its value, what it has made — pledged and untouchable instruments marked |
| `/tradebook` | every trade taken, what it made or lost, and why it was closed |
| `/regime` | how defensive the strategy is being, in the sentences the desk wrote when it decided |
| `/reconcile` | whether the last plan did what it planned to, order by order |

The five M26 pages are the desk console's own pages **rewritten rather than ported**: `R1` reads
"Risk-on — fully invested", `exit_reason='rank'` reads "fell out of the ranking",
`RISK_BLOCKED` reads "blocked by a risk limit". The codes are shown beside the words.

**Two of the console's pages are deliberately not here in full.** The desk's `/stops` and
`/reconcile` read *live broker state*, and `/stops` creates and deletes triggers at the broker.
These serve what the database knows; live confirmation stays in the desk console, and each page
says so. That is the D3 question `CLAUDE.md` forbids building against.

Both are built from the merged backend: live bars → `MomentumScan` → `baskfy_core.score` → the
basket engine, and the `desk` schema for the plan. The API serves them at `/api/v1/baskets` and
`/api/v1/baskets/plan`.

**Strictly read-only, and enforced rather than intended.** There is no execute control on either
page and no route behind one — `POST`, `PUT` and `DELETE` all return **405**. Two tests hold it
that way for the basket pages — `services/api/tests/test_baskets_readonly.py` and
`apps/web/src/lib/basket/__tests__/read-only.test.ts` — and two more for the desk pages:
`test_desk_readonly.py` and `lib/desk/__tests__/read-only.test.ts`. Between them: no mutating verb
on the whole API surface, no import of `baskfy_execution`, no Kite client or token named anywhere
in the desk router, every SQL statement a SELECT, no non-GET fetch, no server action, no form, no
submit control.

Execution stays in the desk console. That is the SEBI gate — the desk trades one account, its
owner's — and it is the desk's non-negotiable #1.

---

## 3b. Corporate actions, and how to undo them (M24, M27, M28)

`corporate_action` held **four rows** until 22 Aug 2026, so most splits and bonuses in the bar
history had never been applied and each one left a cliff. NSE's API serves only a forward window,
so it cannot supply the history.

**The history was recovered from data already on disk.** Kite's `historical_data` returns
*adjusted* bars — measured, and contrary to what `docs/09` assumes — while `ohlcv_daily.close_raw`
is the NSE bhavcopy's exchange print. The ratio between them is the adjustment still owed, so every
step in it is a corporate action.

```bash
cd decile-blueprint
make token-sync TARGET=momentum-desk                  # both token stores; see below
uv run python -m baskfy_worker.action_recovery        # DRY RUN — reports, writes nothing
uv run python -m baskfy_worker.action_recovery --write # writes, then rebuilds the adjusted series
```

**Dry run is the default on purpose.** The writing form rewrites price history for instruments a
live strategy ranks, so it has to be typed deliberately. It writes **only** splits and bonuses —
dividends are recovered, counted and refused, because M27 measured the reference corpus and it
computes momentum on a *price* return.

**To undo all of it**, in one predicate:

```sql
DELETE FROM corporate_action WHERE raw->>'source' = 'ratio_recovery';
```

then re-run `reprocess_instrument` for those instruments. `close_raw` is never written by any of
this, so the reversal is total — `services/worker/tests/test_action_recovery_write.py` asserts
exactly that round trip.

To re-measure the price-versus-total-return question against the corpus:

```bash
uv run python -m reconciliation.dividend_convention --write   # rewrites RECOVERED-ACTIONS.md
```

**The evidence lives in `reconciliation/RECOVERED-ACTIONS.md`** — the recovered actions, the
method, the measurement and the verdict, all regenerable.

---

## 4. Testing everything

```bash
bash tools/ci-local.sh              # from the repo root: every CI step, locally
```

It prints `SKIP — <reason>` rather than passing silently over anything it cannot do.

Individually:

```bash
cd decile-blueprint
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -p no:randomly            # screener + worker + core: 2,346 passed
pnpm -r run test                        # web app (420) and API client (110)
cd ../kite-momentum-rebalancer && .venv/bin/python -m pytest   # the desk: 1,328 passed
```

**Run each suite from its own directory.** `pytest` at the *repository root* walks into
`frozen/strangle/` and goes red — the frozen tree is excluded from CI and from
`tools/check-namespace.sh`, is deliberately unmaintained, and nothing stops a root-level collection
finding it. That is not a regression; it is what "frozen" means.

**Do not pass `-q`.** `addopts` already carries it, so a second one means `-qq` and the summary
line disappears.

**`-p no:randomly` on the db suite is not optional today.** Under random ordering nine tests fail
with `DeadlockDetectedError`; each passes alone and the whole suite passes deterministically. It is
a fixture-concurrency problem, recorded in `docs/DECISIONS-MERGE.md` M19.6, and it is not fixed.
Even with it, one run in the final pass produced a single intermittent fixture ERROR in
`test_seed.py`; it passed alone and the suite re-ran clean.

Three checks that are specific to the merge:

```bash
cd decile-blueprint
uv run python reconciliation/desk_parity.py   # the merged engine vs the desk's real scan corpus
make backend-parity                           # every desk page on SQLite and on Postgres, diffed
make shadow DATE=2026-08-18                   # both scan paths, diffed at order level
bash ../tools/check-namespace.sh              # no namespace token AND no old brand name survived
uv run python -m reconciliation.dividend_convention   # price vs total return, re-measured
uv run python -m baskfy_worker.action_recovery        # corporate actions: DRY RUN, writes nothing
make friday-drill DATE=2026-08-18                    # the whole Friday loop, zero orders
```

---

## 5. Where to look when something is wrong

| | |
|---|---|
| the desk's own runs | `/ops`, and `/ops/job/<id>` for one |
| what the regime decided, and whether it happened | `/regime` |
| plan vs broker | `/reconcile?plan_id=<id>` |
| stops, sized from the broker's holdings | `/stops` |
| the audit record of every order ever | `data/outputs/orders_journal.jsonl` |
| one execution in full | `data/outputs/execution_<plan_id>.json` |
| the screener's pipeline | `/admin` on the API, and Flower |
| metrics | `METRICS_PORT=9464` then `curl :9464/metrics` — `desk_*` are M20's |
| traces / errors | set `OTEL_EXPORTER_OTLP_ENDPOINT` / `SENTRY_DSN`; both off by default |
| runbooks | `decile-blueprint/docs/runbooks/` — six of them, #6 is a half-executed rebalance |

---

## 6. What is deliberately not running

**The options / strangle subsystem.** Frozen at M6 under `frozen/strangle/`, `OPTIONS_ENABLED=false`.
`/options` returns 404 on both database backends, by design. Thawing it is a documented `git mv`
(`frozen/strangle/README.md`) plus a config change, and it is out of scope for the merge.

**The generated scan as the default.** Built at M13 and off. `/analyze` still takes an upload;
`generate_for=YYYY-MM-DD` opts in per request. Four consecutive green shadow Fridays buy the flag
(`docs/SHADOW-MODE.md`), and the first run was red — four order deltas, one of them a substitution
caused by a missing corporate action.

**The pipeline as the desk's breadth source, in anger.** M14 wired it and the two numbers reconcile
exactly (68.6347% both sides, same 271 symbols). But `FULLY_INVESTED` is on, so `cash_pct_for`
returns 0% before it ever looks at the bands. The wiring is correct, reconciled, and inert until
somebody turns that off.

**The systemd timers on the Mumbai box, retired.** They still run. Beat has the same jobs at the
same hour; five green Beat runs retire the timers, by hand (`docs/TIMER-RETIREMENT.md`).

**The public API, billing, and everything in Phase 4+.** Not built, not merged, not in scope.

**The desk on Postgres in production.** The code default is `DESK_DB_BACKEND=sqlite` and stays
that way — the box has no Postgres, and a code-level default of `postgres` would break it silently
at 18:30 on the next deploy. The local `.env` opts in; rolling back is that one line in reverse.

---

## 7. The three rules nothing here may break

1. **`packages/core` touches nothing.** No network, no database, no clock. It takes data and
   returns data. `packages/core/tests/` asserts this structurally, over the source.
2. **`packages/execution` is the only path to an order.** Not "the recommended path" — the only
   one. Structural tests assert that nothing else imports a broker's order methods, and the web
   app cannot reach it at all.
3. **`DRY_RUN` defaults to true.** Every gate in `ProductGates` is fail-closed: dry-run on,
   intraday off, options off. A missing environment variable makes the desk safer, never riskier.
