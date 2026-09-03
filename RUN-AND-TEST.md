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

**One web dev server per build directory.** `pnpm run dev` goes through
`apps/web/scripts/dev-guard.mjs`, which refuses to start a second `next dev` against a `.next`
that already has one. Two of them overwrite each other's chunks and the app then dies on a
*request* with `Runtime TypeError: __webpack_modules__[moduleId] is not a function` pointing at
`.next/server/webpack-runtime.js` — a message that reads like a source bug while `next build` of
the same tree is clean. To run a second server anyway (two branches, two ports), give it its own
build directory:

```bash
BASKFY_WEB_DIST_DIR=.next-alt pnpm --filter @baskfy/web run dev --port 3003
```

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

### The swing book's own scan

The nightly chain's twelfth step (`compute_swing`, SW3) writes `sw_setup_daily` and
`sw_market_daily` from the bars it has just published, and Beat runs the same job again at 21:00
IST in case the chain failed its quality gate. Neither can fail the night.

To run it by hand for a date the chain already published:

```bash
cd decile-blueprint
make swing DATE=2026-09-01              # one date
make swing DATE=2026-09-01 SESSIONS=5   # the Saturday scan: the last five sessions
```

It prints the funnel — universe → with a bar today → liquid → candidates per setup — because
"0 flags" and "0 flags out of 41 liquid names" are different answers. It reads bars and writes
two tables; **it places nothing and makes no Kite call.**

**It needs 125 sessions of history per name.** A database with a few weeks of bars produces no
candidates and says so in the funnel rather than failing.

### The morning Kite login link (SW18)

At **08:45 IST** on a weekday, if no usable Kite token is stored for the day, one email arrives:
subject "Kite login needed before 09:15", carrying a single Kite login link. **Tap it, log in on
Zerodha's page, and you are done** — the token is stored by `/api/v1/brokers/callback` and the
09:14 monitor picks it up. If the browser asks you to sign in to Baskfy on the way back, that is
the last hop, not a failure. A link is good for 30 minutes and can be used once; if the first has
expired, a **second and last** message follows at 09:05 with a fresh one. Nothing arrives when a
token from this morning is already stored, on an NSE holiday, or with the flag off.

The box needs `BASKFY_KITE_LOGIN_NUDGE_ENABLED=true`, `BASKFY_KITE_LOGIN_NUDGE_TO=<address>`,
`BASKFY_SOLE_USER_ID`, `BASKFY_KITE_API_KEY` and `BASKFY_BROKER_OAUTH_STATE_PATH` on the shared
state volume. The message never carries the api secret, the token or the encryption key, and
there is nothing in it to reply to. **After a fresh login, `docker compose restart desk`** — the
desk web service reads the token once, at construction (Q-SW13-1); the monitor does not need it.

```bash
cd decile-blueprint
# what Beat runs at 08:45 / 09:05, by hand:
uv run python -c "from baskfy_worker.tasks.celery_tasks import kite_login_nudge_task as t; print(t('first'))"
```

### The swing book's morning (SW6)

Two Beat entries and one desk process, all three dark by default:

```bash
cd decile-blueprint
make swing-premarket DATE=2026-09-02 STAGE=LEVELS   # 08:50: re-express watched levels under today's adj_factor
make swing-premarket DATE=2026-09-02                # 09:09: the pre-open gap scan + the MORNING plan
```

The 09:09 stage pulls quotes **only** with `BASKFY_SWING_EP_PREMARKET_ENABLED=true`, and then
through the rate-limited Kite provider in batches of at most 500; with the flag off (the
default) it makes no Kite call and still rebuilds the plan. It prints the report: levels
refreshed, universe quoted, gap candidates, and the plan id.

```bash
cd kite-momentum-rebalancer
python -m app.swing_monitor       # 09:15-10:45: the opening-range monitor. Needs a Kite login.
```

With `BASKFY_SWING_MONITOR_ENABLED=false` (the default) it logs one line and exits 0 without
building anything — a launchd entry can exist before the flag does. With it on, it watches the
`WATCHING` rows, and a break of the 5-minute opening range writes an `sw_signal` row and a
one-line `sw_plan` of `source=SIGNAL`. **It holds no gateway and cannot place**; the line it
writes is `PROPOSED` and only the desk page's confirm (SW7) can move it.

To see what a morning would have raised without a broker at all:

```bash
cd kite-momentum-rebalancer
.venv/bin/python ../tools/swing/replay.py ../tools/swing/fixtures/morning-synthetic.csv \
    --watchlist ../tools/swing/fixtures/morning-synthetic.watchlist.json \
    --expect ../tools/swing/fixtures/morning-synthetic.expected.json
```

### The swing book's DRY_RUN morning drill (SW10) — the whole paper session, zero orders

The swing book's own version of §3's Friday drill, and the first condition of the real-money
gate (`docs/swing/02` §3.1). It runs one full paper session end to end through the production
code paths — the evening job, the two premarket stages, the opening-range monitor's store, the
desk's `execute_line` over the real gateway, the evening job again, the next morning's plan —
and counts the orders that reached a broker. That number must be zero.

```bash
cd decile-blueprint
export BASKFY_DATABASE_URL="$(grep '^BASKFY_TEST_DATABASE_URL=' .env | cut -d= -f2- | sed 's#/baskfy_test$#/baskfy_sw_t3#')"
export BASKFY_SOLE_USER_ID=1 DRY_RUN=true
uv run python ../tools/swing/drill.py
```

**It resets the database it is pointed at** — `alembic upgrade head`, then the pipeline and
`sw_` tables truncated and re-seeded — so it refuses any `BASKFY_DATABASE_URL` whose name does
not say `test`, `drill` or `_t<n>` unless you pass `--database-is-disposable`. It also refuses
to start with `DRY_RUN=false` or `BASKFY_SWING_EXECUTION_ENABLED=true` in the environment, the
Friday drill's rule: a refusal, not a warning. Nothing in it needs a Kite login; nothing in it
reads the clock.

What it prints, step by step (a real run, 2 Sep 2026, on `baskfy_sw_t3`, after SW10.4's
confirm-time gate):

```
Swing DRY_RUN drill — 2026-08-18 evening → 2026-08-19 morning → 2026-08-20 plan   DRY_RUN=true   BASKFY_SWING_EXECUTION_ENABLED=false   BASKFY_SWING_MONITOR_ENABLED=false
  0. migrate         alembic upgrade head → 0030_swing_primary_sources (head)
  0. database        reset + seeded: user 1, broker account 7, sleeve ₹1,000,000, 4 names × 40 bars, ...
  1. evening before  EOD 2026-08-18: gate GREEN rung 0→0, watch +4 −0, managed 0, exits 0, entries 1, skips 3, ... sessions logged 1
  2. 08:50 LEVELS    LEVELS 2026-08-19: refreshed 0, unchanged 4
  3. 09:09 MORNING   MORNING plan 2026-08-19: quotes pulled 0 (flag off), entries 1, exits 0, skips 3
  4. 09:15-10:45     replayed morning-synthetic.csv through PgSignalStore: 4 signals, 2 SIGNAL lines, gate GREEN rung 0
                       09:20  LOCKED_UPPER_CIRCUIT   GAMMALOCK  ...
                       09:31  TRIGGERED              ALPHAFLAG  entry=  100.80 stop=   97.80
                       09:35  BELOW_PIVOT            DELTAWAIT  ...
                       09:45  TRIGGERED              BETAEP     entry=  210.50 stop=  204.50
  5. confirm         two TRIGGERED lines through execute_line + the real gateway (dry-run) over an exploding broker client, each under the session lock and re-sized to the rung (A5)
                       SWING BUY ALPHAFLAG x1666 @ 100.80 stop 97.80 → SIMULATED; position 1 x1666 gtt DRY-…:ALPHAFLAG:GTT simulated=True
                       SWING BUY BETAEP x833 @ 210.50 stop 204.50 → SIMULATED; position 2 x389 gtt DRY-…:BETAEP:GTT simulated=True
                         re-sized at confirm 833 → 389 (A5): re-sized at confirm 833 → 389 (size by CASH): book ₹167,932.80 + ₹81,884.50 = 24.98% of the sleeve, ceiling 25% at rung 0, 1 entry today
                       EXPOSURE after confirms ₹249,817.30 = 25.0% of the sleeve (rung ceiling 25% = ₹250,000.00); 2 entries today, 2 of 2 positions at rung 0
                       swing journal (swing_orders_journal.jsonl): dry_run, gtt_dry_run, dry_run, gtt_dry_run
                       broker client touched: 0
  6. the close       bars, detectors' rows and the market row for 2026-08-19: ALPHAFLAG 104.50, BETAEP 212.00, ...
  7. 21:05 EOD       EOD 2026-08-19: gate GREEN rung 0→0, ... managed 2, exits 1, entries 0, skips 4, naked none, sessions logged 2
                       SWING RAISE GTT ALPHAFLAG to 100.80 — BREAKEVEN_AT_R [PROPOSED]
  8. 09:09 MORNING   MORNING plan 2026-08-20: quotes pulled 0 (flag off), entries 0, exits 1, skips 4
  every sw_ row     51 rows across the sw_ tables, all user 1's
  sw_session 2026-08-18: mode=DRY_RUN monitor_ran=False signals=0 confirms=0 fills=0 manage_actions=0 plans=1
  sw_session 2026-08-19: mode=DRY_RUN monitor_ran=True signals=4 confirms=2 fills=2 manage_actions=0 plans=1
  orders that reached a broker: 0   (journal: dry_run, gtt_dry_run, dry_run, gtt_dry_run)
DRILL OK
```

**What "0 orders" is proven by**, in order of strength:

1. **The broker client explodes.** The gateway is built by the desk's own `build_swing_gateway`
   over an `ExplodingKC` whose `place_order`, `place_gtt`, `delete_gtt` and `instruments` raise
   and count every touch. The gateway's dry-run branch returns before any of them; a touch would
   surface as a `REJECTED` outcome, fail the drill, and print the count.
2. **The swing journal is read back.** `swing_orders_journal.jsonl` (written to a temporary
   directory, never the desk's real journal) must contain exactly `dry_run, gtt_dry_run` twice —
   one LIMIT buy and one GTT stop per confirmed line — and nothing else.
3. **The rows say so.** Both `sw_position` rows carry `simulated=true` and a `DRY-…` trigger id,
   both `sw_fill` rows are `simulated=true`, and `sw_session` for the morning counts exactly
   two confirms and two fills.
4. **The flags are pinned, not assumed.** The drill sets `DRY_RUN=true`,
   `BASKFY_SWING_EXECUTION_ENABLED=false` and `BASKFY_SWING_MONITOR_ENABLED=false` in its own
   environment before importing the desk, and asserts `swing_gates().dry_run` before the first
   confirm. The monitor flag stays false: the strategy is driven directly, the way
   `tools/swing/replay.py` drives it, with the Postgres store in place of the harness's list.

**What the `EXPOSURE` line is proven by** (SW10.4, STANDING-ANSWERS A5). Both SIGNAL lines
were sized at their own triggers, before either was confirmed: 1,666 ALPHAFLAG (₹1,67,932.80)
and 833 BETAEP (₹1,75,346.50), together 34.3 % of a sleeve whose rung 0 allows 25 %. Each
confirm runs under `PgSwingStore.lock_session_for_update` — a transaction holding the day's
`sw_session` row `FOR UPDATE` from before the book is re-derived until after the gateway has
answered — and re-sizes the line through the same `entries_now` the monitor sizes a SIGNAL line
with. The first fits whole; the second is shrunk to the ₹82,067.20 of headroom (389 × 210.50 =
₹81,884.50), its row rewritten with the quantity, the risk and the value that went out, and its
position, fill and GTT all carry 389. The drill then **exits 1** if the book after the confirms
is over the rung's ceiling, if the session does not count exactly the two entries, if the
position count is over the rung's, or if the number of re-sized lines is not exactly one (the
fixture is built so that one, and only one, confirm has to shrink). Before SW10.4 the same
line read `WARNING book after confirms ₹343,279.30 = 34.3%` and did not fail
(`docs/swing/DECISIONS-SW.md` SW10.2, now closed by SW10.4).

Exit code 1, with `DRILL FAILED: <reason>` on stderr, for anything else that does not do what
the rules say: a replay that raises other signals than the fixture's, a confirm that is not
`SIMULATED`, a journal with any other event, a touched broker client, a session row that does
not count two and two, an `sw_` row that is not the sole user's.

### Filling market cap and P/E for a date the pipeline already published

Step 6 fetches NSE fundamentals as part of a night. For a **past** date — a table that was never
filled, or a night where NSE was down — do not re-run the whole chain; it would refetch bars that
are already correct and hand the quality gate a date nobody asked about.

```bash
make fundamentals DATE=2026-08-18      # ~2,540 symbols at NSE's 1 req/s, roughly 40 minutes
make refactors    DATE=2026-08-18      # rebuild factor_daily from data already on disk
```

Both are safe to interrupt and re-run: `--resume` skips symbols already stored, the raw-file
archive never refetches a key it already holds, and the fill commits every 25 symbols. The run
ends with a line accounting for every symbol in scope — `stored / already_present / no_quote /
unmatched / failed` — and names the failures so a second pass can target them. `ACCOUNTED OK`
means those five add up to the scope; anything else means a symbol went missing and the run
should not be believed.

**Never run a bare `next build` while `make web` is running.** Next writes a production build
into `.next`, which is the directory the dev server owns, and the result is a *mixed* tree: the
page still returns 200 but its `main-app.js` and `polyfills.js` 404 as `text/plain`, React never
hydrates, and every button and tab on the page looks disabled. It reads exactly like a product
bug and is not one.

The repo already has the convention that avoids it — `next.config.ts` reads
`BASKFY_WEB_DIST_DIR`, and `tsconfig.json` expects `.next-build`, `.next-e2e`, `.next-gate`:

```bash
cd decile-blueprint/apps/web
BASKFY_WEB_DIST_DIR=.next-build pnpm exec next build     # never touches the dev server
```

If you hit it anyway: `pkill -f "next dev"; pkill -f next-server; rm -rf .next`, then `make web`.
Kill both — `pkill -f "next dev"` alone leaves the `next-server` child alive and serving from the
clobbered directory. Verify with:

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
  http://localhost:3000/_next/static/chunks/main-app.js     # want 200 application/javascript
```

A stale `.next*/types/validator.ts` causes the same class of confusion in `pnpm run lint`: it
still imports pages that were deleted, and `tsc` reports errors that look like source errors.
Same fix — remove the stale build dir.

**If it stalls.** Observed once during the first real fill: the log starts repeating
`provider retry`, the archived-file count stops rising, and the process sits at 0% CPU — while
`curl` against the same NSE endpoint answers 200 in 0.3s. A stuck HTTP connection, not NSE. Kill
it and re-run the same command; `--resume` picks up from the last committed batch and the archive
means nothing already fetched is fetched twice. Watch progress with either of:

```bash
docker exec baskfy-postgres psql -U baskfy -d baskfy -tAc \
  "select count(*) from fundamental_daily where date='2026-08-18'"
find decile-blueprint/.archive/nse/equity-fundamentals -name '2026-08-18.json' | wc -l
```

The archive count leads the row count by up to one batch (25); if **both** are frozen for more
than a minute or two, it is stalled.

**Which date do you actually want?** Almost always the one the API serves, which is
`max(pipeline_run.trade_date)` where `data_version IS NOT NULL` — *not* `max(ohlcv_daily.date)`.
Filling only the newest bar date leaves every rendered surface on an em dash while the table looks
full. `bash tools/tree3/surfaces.sh` checks the rendered end of that.

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

## 3c. Deep history, indices and breadth (M29, M31, M32)

**Do not run `make backfill`.** It writes the provider's `close` into `close_raw` on docs/09's
assumption that Kite returns unadjusted bars, and Kite does not (M24). These are the modules that
know what they are storing:

```bash
cd decile-blueprint
uv run python -m baskfy_worker.deep_backfill                 # DRY RUN — daily bars, 2017 onward
uv run python -m baskfy_worker.deep_backfill --write

uv run python -m baskfy_worker.index_backfill                # DRY RUN — 136 NSE index levels
uv run python -m baskfy_worker.index_backfill --write

uv run python -m baskfy_worker.breadth_backfill --from 2021-08-01 --every 5 --write
```

**Kite's own limits, measured rather than assumed:** history from **2000-01-03** and nothing
earlier; a hard **2,000-day** cap per `day` request; 3 req/s through a Redis token bucket shared
across workers, 5-attempt jittered backoff, circuit breaker at 5 failures.

**`breadth_backfill` is sampled, and `--every` is why.** `compute_factors` costs about **45 seconds
per as-of date**, and it is the computation, not the database — loading 1.2M bars takes 6 seconds.
Nine years daily is roughly 30 hours. `--every 5` is one trading day a week: 52 points a year, the
same line on a chart, a fifth of the cost. `--every 1` is the full job.

It also carries index membership backwards, marked **`source = 'derived'`**, because NSE publishes
constituents for today only and Kite has no constituents endpoint at all. That is survivorship
bias; `/market-health` says so on the page.

**To undo the deep history:** `DELETE FROM ohlcv_daily WHERE source = 'kite'`. The bhavcopy segment
is untouched — the write is `ON CONFLICT DO NOTHING`.

---

## 3d. Sleeves — a portfolio run as several screens (M34)

`/portfolios/[id]/sleeves`, linked beside Rebalance on each portfolio card.

Two questions, two surfaces: **Rebalance** answers *"which symbols changed"*; **Sleeves** answers
*"how much goes where"*. A sleeve is one slice with its own capital and its own source — a saved
screen, or `manual` for capital you run yourself, counted so the totals are honest and never
allocated.

```
GET  /api/v1/portfolios/{id}/sleeves
PUT  /api/v1/portfolios/{id}/sleeves        # replace the whole division
GET  /api/v1/portfolios/{id}/allocation?apply_regime_cap=false
```

**Amounts and target weights, never unit counts.** That is structural rather than a promise:
`baskfy_core.sleeves` receives no market quote, so it cannot produce a number of units.
`test_sleeves_are_not_orders.py` asserts the vocabulary of an order appears in neither the
allocator nor the router, and that every route is GET or PUT.

**The market stance is stated, not recommended.** The page shows the desk's current tier and what
it implies — *"under R1 the strategy caps equity at 100%"* — with an unticked control to size the
screen sleeves to that cap. Applying it withholds capital as cash; a crore stays a crore.

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
uv run pytest -p no:randomly            # screener + worker + core: 2,414 passed
pnpm -r run test                        # web app (433) and API client (110)
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
uv run python -m baskfy_worker.deep_backfill          # deep history: DRY RUN, writes nothing
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

---

## 8. Curated baskets — explore, publish, Friday apply

The smallcase-shaped layer (`cb_*`) is Track A for a single operator. It does not add a web
execute path. Cold bring-up for the catalog:

```bash
cd decile-blueprint
make up && make migrate && make seed   # managers + Momentum Scan basket when instruments exist
make api                               # GET /api/v1/explore
make web                               # http://127.0.0.1:3000/explore
```

`make seed` upserts the two curated managers (Baskfy Engine, Maulik) and, when enough instruments
are present, the SCAN projection basket. Re-running is idempotent. Without that seed, `/explore`
renders an empty catalog — that is missing data, not a broken route.

**Catalog metrics.** Beat entry `cb-eod-metrics` fires `baskfy.cb.compute_metrics` Mon–Fri at
**20:20 IST** (after the 20:15 publish SLO, before 20:30 alerts). Idempotent upsert on
`(basket_id, as_of_date)`. Cards on `/explore` and `/basket/[slug]` read the latest row; an empty
metrics join means min-amount / returns show as em dashes until the job has run once.

**Publish version mental model.** A curated basket version is an immutable cut of weights
(GENESIS / CHANGED / NO_CHANGE). Publishing appends a version and may raise
`REBALANCE_AVAILABLE` — it does **not** place orders. SC publish is "the target book changed";
the operator still has to apply on a Friday through the desk. Drift / apply-preview maths live in
`baskfy_core.curated_versions`; the web Invest CTA only opens `PlanHandoffPanel` ("Plan #" /
desk / expires) and points at the desk console.

**Friday apply — desk only, not web execute.** The web app has no execute control for curated
baskets; do not look for one on `/explore`, `/basket/[slug]`, or investments. Execution is only
on the desk console with `confirm=true`.

### Friday operator checklist

Exact steps for a curated rebalance Friday. Stay in order.

1. **Confirm `DRY_RUN` / desk session.**
   - On the desk box (or local desk tree): `grep DRY_RUN .env` — keep `DRY_RUN=true` until you
     deliberately mean live orders.
   - Confirm a live desk session (Kite login / token bridge) so holdings and quotes are real for
     the session. Agents never flip to live; operator does that by hand outside market hours only
     when ready.

2. **Publish or confirm basket version (curated).**
   - Ensure the target curated version is the one you intend (GENESIS / CHANGED / NO_CHANGE).
   - Publishing appends an immutable version and may raise `REBALANCE_AVAILABLE` — it does **not**
     place orders. Confirm the version / pending rebalance on the basket before applying.

3. **Generate apply plan** via `POST /api/v1/cb/plans/apply` **or** the UI Invest →
   `PlanHandoffPanel` hand-off.
   - API: `POST /api/v1/cb/plans/apply` with holdings, target weights, prices, and amount (NSE
     session open; outside hours returns closed-market, no plan).
   - UI: Invest CTA opens the hand-off panel (`Plan #…` / desk / expires) and links to the desk
     console — still preview / hand-off only.
   - This step never calls the execution gateway.

4. **Open desk console — execute only there.**
   - Open `https://desk.modelbasket.in` (or `NEXT_PUBLIC_DESK_URL`).
   - On the desk: `/analyze` (or upload / adopt the hand-off) → review legs →
     `POST /execute` with `confirm=true` and the desk `plan_id`.
   - **Do not instruct or attempt web execute.** There is no web `/execute` for curated baskets.

5. **Plan expiry — 30 minutes.**
   - Desk non-negotiable #1: the `plan_id` from `/analyze` expires in **30 minutes**. After that,
     re-analyze; do not re-POST a stale plan. The hand-off copy says the same (`expires` /
     `expires_at_hint`).

6. **After fills — sync holdings / drift check.**
   - Sync broker holdings: `POST /api/v1/brokers/{broker_id}/sync-holdings` or the `/brokers` UI
     sync (qty + T1 + collateral).
   - Confirm the book vs curated target (investments / pending `DRIFT` or `REBALANCE_AVAILABLE`
     actions). Fix drift only through another desk-confirmed plan if needed — never from the web.

**Track B flags stay false.** D7 is recorded in `docs/DECISIONS-MERGE.md` as UNREVIEWED engineering
continuity — **do not flip** these without counsel + Maulik amounts:

| Flag | Default | While false |
|---|---|---|
| `BASKFY_SUBSCRIPTIONS_ENABLED` | false | every basket is Free Access; paywall routes 404 |
| `BASKFY_FEE_COLLECTION_ENABLED` | false | ledger math only; no collection call sites |
| `BASKFY_PUBLIC_SIGNUP_ENABLED` | false | signup-shaped routes 404 |

Flipping them is a deliberate deploy, not a byproduct of seeding the catalog or a Friday apply.

**`DRY_RUN`.** Same rule as §1 and §7: every agent environment and every Friday drill keeps
`DRY_RUN=true`. Curated plan previews and hand-offs are read-only by construction; the desk is
where a simulated (or live) execute can happen, and only with `confirm=true`.

### Catalog metrics (`cb_metrics`) — Tree 7

Beat schedules `cb-eod-metrics` nightly, but until the job has actually written rows the
`/baskets` / `/explore` cards show `metrics: null`. Populate (or refresh) with:

```bash
cd decile-blueprint
make cb-metrics                 # as-of today IST
make cb-metrics DATE=2026-08-21 # pin a trading day that has bars
# or: uv run python -m baskfy_worker.cb_metrics_cli --date 2026-08-21
```

Requires a live Postgres with at least one non-archived `cb_basket` and price history for its
constituents. Idempotent per `(basket_id, as_of_date)`.
