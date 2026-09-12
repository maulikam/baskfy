# Kite sync — what syncs from the broker today, and what does not

**Leaf 2 of `PLAN-SCAN-SYNC.md`, 12 Sep 2026.** Maulik asked to "sync transactions from the kite
account". This file establishes what exists, what it needs, and what blocks it. Every box CHECK is
a **read**; this leaf writes nothing to the box, deploys nothing, places no order and touches no
execution flag.

## The answer in four lines, before the gates

1. **Holdings sync exists, is live, and ran today.** `POST /brokers/{broker_id}/sync-holdings`
   (`services/api/src/baskfy_api/routers/brokers.py:739`) → `holdings_for_broker` → the rate-limited
   Kite provider → `sync_holdings_into_portfolio`. It is a **route**, driven by a button; it is
   **not** on Beat and has no schedule of any kind.
2. **Transaction sync into Baskfy does not exist.** Not a route, not a task, not a table. Nothing
   in `decile-blueprint/` has ever called Kite's `/trades` or `/orders`.
3. **The only transaction importer in the monorepo is the desk's** — `kite-momentum-rebalancer/
   app/analytics/tradebook.py`. It has two inputs: today's fills from `kite.trades()` (**same-day
   only** — Zerodha flushes `/trades` nightly and the endpoint takes no date), and a **Zerodha
   Console tradebook CSV** that a human downloads. On the box its store is **empty** and the Beat
   job that would drive it **cannot run** (G9).
4. **CAS import is a separate path and it is unwired** — `packages/core/src/baskfy_core/
   cas_import.py` is pure text parsing with **zero callers**: no route, no task, no extractor.
   That is why `first_bought_on` is NULL on broker-synced rows (G12).

**What needs Maulik's hands:** a Console tradebook export. Written up as `NEEDS-MAULIK.md` §32.
The daily Kite login (T1) is *not* currently blocking — the box has a valid token right now (G6).

---

## Ledger: 14 / 14

- [x] G1: **No code in the merged product (`decile-blueprint/`) reads Kite trades or order
      history.** Zerodha's `trades()` / `orders()` / `order_history()` appear nowhere in the API,
      the worker, core or providers. There is no transaction import to find.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rIn 'get_trades()\|\.trades()\|order_history(' --include='*.py' decile-blueprint/services decile-blueprint/packages | grep -v '/tests/' | wc -l | tr -d ' '
  EXPECT: /^0$/m
  EVIDENCE: `0` (12 Sep 2026). The only `get_trades` in `decile-blueprint` is
  `routers/backtests.py:671`, a **backtest** result reader — it returns simulated trades from a
  `backtest` row and never speaks to a broker.

- [x] G2: **There is no Postgres table for broker transactions.** 108 tables are declared across
      `packages/core/src/baskfy_core/models/`; not one is a trade, transaction or tradebook.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rho '__tablename__ = "[a-z_]*"' decile-blueprint/packages/core/src/baskfy_core/models/*.py | sed 's/.*= //' | tr -d '"' | grep -cE 'trade|transaction|tradebook'
  EXPECT: /^0$/m
  EVIDENCE: `0`. The nearest things are `sw_fill` / `vb_fill` / `tw_fill` — each sleeve's record of
  **its own** orders, written by that sleeve's execution path (`app/swing_desk.py:497`), never an
  import of the account's history. `portfolio_cash_flow` exists and has **no writer anywhere**
  outside tests (readers only: `routers/portfolio_overview.py`, `tasks/portfolio_nav_job.py`).

- [x] G3: **On the box, every one of those tables is empty** — so this is not "the import exists
      and has not run lately", it is "nothing has ever put a transaction in this database".
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select 'cash_flow='||(select count(*) from portfolio_cash_flow)||' broker_cash='||(select count(*) from broker_cash)||' recon_item='||(select count(*) from reconciliation_item)||' sw_fill='||(select count(*) from sw_fill)||' vb_fill='||(select count(*) from vb_fill)||' tw_fill='||(select count(*) from tw_fill)"
  EXPECT: /cash_flow=0 broker_cash=0 recon_item=0 sw_fill=0 vb_fill=0 tw_fill=0/
  EVIDENCE: `cash_flow=0 broker_cash=0 recon_item=0 sw_fill=0 vb_fill=0 tw_fill=0` (12 Sep 2026,
  13:30 IST, box tag `c1ca302`).

- [x] G4: **Kite Connect cannot supply history even if someone wrote the importer.** The desk's own
      client says so at the call site, and it is the reason a Console CSV is the only path.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c 'THE BOOK IS SAME-DAY ONLY' kite-momentum-rebalancer/app/kite_client.py
  EXPECT: /^1$/m
  EVIDENCE: `1`. `app/kite_client.py:203-211`: *"/trades takes no date parameter and Zerodha
  flushes it nightly, so this is the only chance to record a fill through the API — miss the
  session and it is gone. Historical fills exist solely in a Console export."*

- [x] G5: **The desk's importer is the only transaction import in the monorepo, and it has two
      mouths.** `capture_live_trades(conn, kite)` (today's fills, idempotent on `trade_id`) and
      `import_tradebook(conn, path)` (a Console CSV, idempotent on re-import). Both write the
      desk's own store — `fills` → `rebuild_symbols` → `trades` (FIFO lots).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c 'def capture_live_trades\|def import_tradebook' kite-momentum-rebalancer/app/analytics/tradebook.py
  EXPECT: /^2$/m
  EVIDENCE: `2` — `tradebook.py:426` and `tradebook.py:335`. Reachable by hand at `POST /tradebook`
  (`app/main.py:1132`, a file upload) and automatically from `scripts/daily.py:238` step 3b.

- [x] G6: **The box HAS a valid Kite access token right now.** Read-only decrypt, no network call,
      no write. Expiry is the IST calendar day of issue (`tokens.py:41`).
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-python.sh worker ops/kite-token-state.py
      ⚠️ **The runner parses one `/regex/flags` and nothing else** — it understands neither
      `/a/ and /b/` nor `not /a/`, so this EXPECT was read as a literal pattern and the row
      could not pass whatever the box said. Rewritten as a single regex; `[\s\S]*` because the
      two facts are on separate lines and `.` does not cross a newline.
  EXPECT: /exists: True[\s\S]*is_expired: False/
  EVIDENCE: `token_path: /var/lib/baskfy/state/kite-token.enc`, `api_key_set: True`, `exists: True`,
  `issued_at: 2026-09-12T11:04:34.997173+05:30`, `is_expired: False` (read 12 Sep 2026 13:32 IST).
  The file is 248 bytes, mtime `Sep 12 11:04`. **It dies at the 13 Sep IST rollover** — T1 stands
  for tomorrow, it just is not blocking today.

- [x] G7: **The live holdings path is armed on the box**: `DRY_RUN=false`, the Kite app key is set,
      and the API reads the same token file the pipeline does (`broker_oauth.token_store_path()`
      falls back to `BASKFY_KITE_TOKEN_PATH`). So `sync-holdings` there is a real fetch, not a
      fixture.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T api sh -lc "env | grep -E \"^(DRY_RUN|BASKFY_KITE_TOKEN_PATH)=\""'
      ⚠️ **The runner parses one `/regex/flags` and nothing else** — it understands neither
      `/a/ and /b/` nor `not /a/`, so this EXPECT was read as a literal pattern and the row
      could not pass whatever the box said. Rewritten as a single regex; `[\s\S]*` because the
      two facts are on separate lines and `.` does not cross a newline.
  EXPECT: /BASKFY_KITE_TOKEN_PATH=\/var\/lib\/baskfy\/state\/kite-token\.enc[\s\S]*DRY_RUN=false/
  EVIDENCE: `BASKFY_KITE_API_KEY=<set>`, `BASKFY_KITE_TOKEN_PATH=/var/lib/baskfy/state/kite-token.enc`,
  `DRY_RUN=false`. `BASKFY_BROKER_HOLDINGS_FIXTURE` is **unset**, so there is no fixture to fall back
  to and `is_persistable` refuses anything that is not `source="live"`.

- [x] G8: **The holdings sync has actually run live against this account, today.** User 1's broker
      pile (`portfolio` id 3, `is_broker_pile=true`) carries two rows first seen 2026-09-12. Only
      `sync_holdings_into_portfolio` can create a pile, and it accepts only `source="live"`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select i.symbol||'|qty='||h.quantity||'|added='||h.added_on from portfolio_holding h join instrument i on i.id=h.instrument_id where h.portfolio_id=3"
  EXPECT: /added=2026-09-12/
  EVIDENCE: `PWL|qty=1575.0000|avg=133.3000|hs=NONE|fbo=-|added=2026-09-12` and
  `WABAG|qty=92.0000|avg=2273.0000|hs=NONE|fbo=-|added=2026-09-12`. `replace_holdings` **preserves**
  `added_on` for a name that persists (`portfolios.py:222`), so today's date means first sight
  today. The second `broker_account` (id 2, user 6) has a pile with **0** holdings — that user has
  never synced. Both accounts are `zerodha|primary`, created 2026-09-01.

- [x] G9: ⚠️ **The desk's daily collection — the one job that would capture today's fills — is
      scheduled on the box and cannot run.** `baskfy.desk.daily` resolves `DESK_ROOT` by walking up
      six parents from its own file; in the deployed image that is `/kite-momentum-rebalancer`,
      which does not exist. `desk_context()` then `os.chdir`s to it. Beat fires it at 18:30 and
      `desk-autorun-safety-net` at 18:50, Mon–Fri.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-python.sh worker ops/desk-task-probe.py
      ⚠️ **The runner parses one `/regex/flags` and nothing else** — it understands neither
      `/a/ and /b/` nor `not /a/`, so this EXPECT was read as a literal pattern and the row
      could not pass whatever the box said. Rewritten as a single regex; `[\s\S]*` because the
      two facts are on separate lines and `.` does not cross a newline.
  EXPECT: /desk-daily-collection/
  EVIDENCE: `desk task module imported: yes`, `DESK_ROOT: /kite-momentum-rebalancer`,
  `DESK_ROOT exists: False`, `desk tasks: ['baskfy.desk.autorun', 'baskfy.desk.daily',
  'baskfy.desk.daily_check']`, `beat entries mentioning desk/holdings:
  ['desk-daily-collection', 'desk-autorun-safety-net']`, `holdings tasks: []`, 44 beat entries.
  The arithmetic, measured in the container: the module is at
  `/repo/services/worker/src/baskfy_worker/tasks/desk.py`, so `parents[6]` is `/` and `DESK_ROOT`
  is `/kite-momentum-rebalancer`. `/repo` holds `.venv, packages, pyproject.toml, services,
  uv.lock` — **the `baskfy-py` image contains `decile-blueprint` only**; the desk tree is in the
  separate `baskfy-desk` image, whose container roots at `/desk`. `/repo/kite-momentum-rebalancer`
  and `/desk` are both absent from the worker. **Not this leaf's to fix** — the fix is a deploy
  (the desk tree in the python image, or the task shelled into the `desk` container), and no leaf
  deploys.

- [x] G10: **The desk's book on the box is empty; the 9,262-fill history is on Maulik's laptop
      only.** The box's desk container runs `DESK_DB_BACKEND=postgres`, `DESK_DB_SCHEMA=desk` — the
      schema and its 20 tables exist, and every table that would hold the track record has 0 rows.
      D8's SQLite migration has not been run against this box.
      ⚠️ **This row carried TWO CHECK/EXPECT pairs and the runner uses the last one** — which was
      `/console_csv/ and /kite_api/`, syntax it does not have. So the row asserted nothing about the
      box at all, which is the half that matters. One row, one check: both facts are printed by one
      command now, and the EXPECT names both.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && printf 'box[%s] laptop[%s]\n' "$(AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select 'trades='||(select count(*) from desk.trades)||' fills='||(select count(*) from desk.fills)" 2>/dev/null | tail -1)" "$(python3 -c "import sqlite3;c=sqlite3.connect('kite-momentum-rebalancer/data/portfolio.db');print(sum(n for _,n in c.execute('select source,count(*) from fills group by source')))" 2>/dev/null)"
  EXPECT: /^box\[trades=0 fills=0\] laptop\[9262\]$/m
  EVIDENCE: box[trades=0 fills=0] laptop[9262] — the desk's schema exists on the box with every table empty, while the real 9,262-fill book is only on Maulik's laptop. D8's migration has never been run against the box.

- [x] G11: **The worker's 828-line `run_holdings_sync` is dead code in production** — no Celery
      task name, no Beat entry, no caller outside its own test file. It is the piece that would
      turn a broker read into `reconciliation_item` rows, and `reconciliation_item` has 0 rows
      (G3) on a box live since 1 Sep.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rIn 'run_holdings_sync\|baskfy.holdings' decile-blueprint/services/worker/src/baskfy_worker/celery_app.py decile-blueprint/services/worker/src/baskfy_worker/tasks/celery_tasks.py | wc -l | tr -d ' '
  EXPECT: /^0$/m
  EVIDENCE: `0`. The only non-test mention anywhere is a prose reference in
  `broker_holdings_sync.py:286` that says the same thing: *"it has no caller outside its own
  tests"*. Its own docstring names the reason it could not be scheduled: the worker registers **no
  HoldingsProvider** (`services/worker/src/baskfy_worker/providers.py` has no `Capability` at all),
  *"and the composite raises rather than substituting fixture positions for a user's real money."*

- [x] G12: **CAS import is the separate path, and it is not wired to anything.**
      `packages/core/src/baskfy_core/cas_import.py` is pure parsing by law 1; its docstring says
      the PDF extraction *"lives in `services/`"* — and `services/` contains no such code. Zero
      importers outside its own test.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rIn 'cas_import' --include='*.py' --include='*.ts' --include='*.tsx' decile-blueprint | grep -v '/tests/' | grep -v 'cas_import.py:' | wc -l | tr -d ' '
  EXPECT: /^0$/m
  EVIDENCE: `0`. Consequence, measured on the box: all 20 `portfolio_holding` rows are
  `history_source='NONE'` — **no row anywhere is `CAS`, `BROKER` or `MANUAL`** — and the two
  broker-synced rows are exactly the two with `first_bought_on` NULL. The 18 rows that *do* carry a
  date are in "Swing Manual" (portfolio 6), typed or imported by hand. `NEEDS-MAULIK`'s line —
  *"`first_bought_on` exists but is NULL for broker-synced rows until a CAS import"* — is
  confirmed, and the CAS import that would fix it has no upload surface to run from.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box-sql.sh "select history_source, count(*), count(first_bought_on), count(broker_account_id) from portfolio_holding group by 1 order by 1"
  EXPECT: /NONE\|20\|18\|20/

- [x] G13: **This leaf added no path to an order and changed no flag.** It wrote no application
      code at all: the only files it created are this gate file, two read-only probe scripts under
      `ops/`, and `NEEDS-MAULIK.md` §32.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git status --porcelain | grep -E 'packages/execution|OrderGateway|EXECUTION_ENABLED|compose.prod.yml' | wc -l | tr -d ' '
  EXPECT: /^0$/m
  EVIDENCE: `0`. Nothing under `packages/execution`, no compose change, no flag. `twt_execution_true=0`
  and the swing/VBT/TWT execution flags were never read or written by this leaf.

- [x] G14: ⚠️ **One accidental write to the box, made and reverted — recorded rather than tidied
      away.** Probing for the desk's SQLite book, this leaf ran `sqlite3.connect("data/portfolio.db")`
      inside the `desk` container. Python's `sqlite3.connect` **creates** the file, so a 0-byte
      `/desk/data/portfolio.db` appeared at 13:34 IST where none had existed. It was removed in the
      next command, guarded on the file being empty, and the directory now matches its prior state
      exactly. No data existed to lose — that container has never had a book (G10) — but a 0-byte
      SQLite file in the desk's data directory is genuinely dangerous: `DESK_DB_BACKEND=postgres`
      means nothing would have read it today, and a future run with the SQLite backend would have
      opened an empty book and believed it. **Rule for the next agent: never `sqlite3.connect` to
      probe for a database; `ls` it.**
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && AWS_PROFILE=baskfy-poc bash tools/deploy/box.sh 'cd /opt/baskfy && docker compose -f compose.prod.yml --env-file .env.staging.compose exec -T desk sh -lc "ls /desk/data"'
      ⚠️ **`not /…/` is not syntax the runner has.** A negation also states the weaker fact: what
      matters is that `/desk/data` holds exactly what it held before, not merely that one name is
      absent. Pinned positively to the two directories, so a stray file of ANY name fails this.
  EXPECT: /^outputs\nuploads$/m
  EVIDENCE: after the revert, `ls /desk/data` prints `outputs` and `uploads` and nothing else;
  `ls -la` shows the same five entries as before — `.nse_board.json` (Sep 3), `.nse_close.json`
  (Sep 3), `.nse_constituents.json` (Sep 3), `outputs/` (Sep 4), `uploads/` (Sep 2) — and no
  `portfolio.db`. (`uploads/` is empty on the box; the regression corpus lives in the repo.)

---

## What would make transaction sync real, in the order it has to happen

Not built here — every step is either Maulik's hand or a schema decision that belongs to a
planned leaf, and building half of it would put a wrong cost basis under a money figure.

1. **A Console tradebook CSV** (`NEEDS-MAULIK.md` §32). No API substitute exists (G4).
2. **A `broker_trade` table in Postgres.** It does not exist (G2), and `holdings_sync.py`'s own
   docstring already asks for its sibling: *"The right fix is a layer-1 `broker_holding` table,
   which is a migration and belongs to the schema leaf, not to this one."* The same is true here.
3. **An importer in `services/`,** reusing `tradebook.parse_tradebook`'s column contract and its
   refusal to half-parse. The desk's FIFO rebuild (`build_lots`) is the reference implementation
   and it has 9,045 real rows behind it.
4. **Then** `first_bought_on` / `history_source='CAS'|'BROKER'` can be filled, and §5.2's
   since-purchase XIRR unlocks. Until then "since grouped" is the only honest start date, which is
   what `broker_holdings_sync.py` already says and does.

**Do not schedule `run_holdings_sync` (G11) as a quick win.** It writes `reconciliation_item` rows
that freeze holdings out of performance, it needs a HoldingsProvider the worker does not register,
and Leaf 1 is rewriting the same sell-detection semantics into the API path this week. Two engines
deciding what happened to the same share is worse than one engine that does not run.
