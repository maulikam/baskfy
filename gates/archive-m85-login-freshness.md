# M85 — the data is current when Maulik logs in, whatever time that is

**What he reported, 4 Sep 2026.** "The system is coded such that I would have to log in a day
early, before the market opens, but my sleep schedule is different — I wake at 1pm, sometimes 3pm.
Once I log in I always have the older dataset, and everything lags. The swing data is lagging, so
there is a price differentiation and I am not able to buy any stock which is in swing."

**Scope.** After a broker login at *any* hour, the day's holdings and the swing book become
current without a second manual action; every Kite read in the deployment shares Kite's stated
combined ceiling of 3 req/s; and the login-time work is never starved by the historical catch-up
running beside it. Nothing here goes near the execution path — non-negotiable #1 stands, the web
app gains no execute route, and `DRY_RUN=true` remains the default everywhere.

**The bug M85 had to fix before it could ship.** Making every Kite read wait on one shared
departure clock (right — Kite's limit is combined, not per endpoint) put the login's holdings read
and the live swing scan behind the catch-up chain's hour of `historical_data`. A single clock has
no lanes, so the interactive reads would exceed their wait budget and fail: the product would be
*more* stale after logging in. `KiteLane` fixes it as arithmetic — the bulk lane is held strictly
below the ceiling, so the shared clock is drained faster than a backfill can fill it.

---

- [x] **G1 — a verified login starts both refreshes, and neither waits for the other**
  One callback publishes the missed-session catch-up (`default` queue) and today's live swing
  scan (`compute` queue). Two queues, `--concurrency=2` on the worker: they run at the same time.
  CHECK: `cd decile-blueprint && uv run pytest -p no:randomly services/api/tests/test_broker_oauth.py services/api/tests/test_broker_holdings_provenance.py -q`
  EXPECT: /passed/
  EVIDENCE: 97 passed. `test_a_verified_token_starts_both_session_refreshes` asserts the exact
  publication order `[session_catch_up, swing.scan_after_login]`; a simulated token publishes
  neither.

- [x] **G2 — an afternoon login produces a scan of *today*, once, and a late one defers**
  `request_login_scan` creates one `sw_scan_run` on the provisional path and refuses a duplicate
  while one is in flight. Outside the cash session it records nothing, because the published data
  is then the honest answer.
  CHECK: `cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest -p no:randomly services/worker/tests/test_swing_scan_now.py -q`
  EXPECT: /passed/
  EVIDENCE: 31 passed, against a real database. A 13:42 login → one QUEUED row, `source:
  broker-login`; a second login one second later → `scan-in-flight`, same run id. An 18:00 login
  → `outside market hours: the last published session`.

- [x] **G3 — one ceiling for the whole box, and a backfill cannot starve a login**
  Every Kite read — API, worker, desk — waits on `baskfy:ratelimit:kite:read` at 3 req/s. Bulk
  callers take `baskfy:ratelimit:kite:bulk` at 2 req/s *first*, so ~1 req/s stands free.
  Batching is Kite's own: 500 instruments per `/quote`, 1,000 per `/quote/ltp`.
  CHECK: `cd decile-blueprint && uv run pytest -p no:randomly packages/providers -q` and `cd kite-momentum-rebalancer && ./.venv/bin/python -m pytest tests/test_kite_limits.py -q`
  EXPECT: /passed/
  EVIDENCE: providers 317 passed (14 of them `test_kite_lanes.py`, new); desk 60 passed.
  `TestTheHeadroomIsReal` runs both lanes against the real Redis on one wall clock: with a
  backfill already running, twelve interactive calls each waited under one ceiling interval, and
  eighteen mixed reads still took at least 17/3 s — the ceiling holds.

- [x] **G4 — the screens say what is fresh, and refresh themselves while the market is open**
  A portfolio layout that carries the expired-session banner and a 30-second `router.refresh()`
  bounded to 09:15–15:30 IST; the swing header labels a provisional scan and polls while one is
  in flight.
  CHECK: `cd decile-blueprint/apps/web && pnpm run test -- --run src/lib/market src/components/portfolio "src/app/(app)/swing"`
  EXPECT: /passed/
  EVIDENCE: see below.

- [x] **G5 — the whole gate, both trees, with no live-order operation invoked**
  CHECK: `DRY_RUN=true tools/ci-local.sh`
  EXPECT: /failed 0/
  EVIDENCE: see below.

- [x] **G6 — committed locally, with the module convention, and pushed nowhere**
  Maulik's instruction was explicit: commit, do not push.
  CHECK: `git status --short --branch && git log -1 --pretty=%s`
  EXPECT: /^M85: green/ and a branch ahead of its remote
  EVIDENCE: see below.

- [ ] **G7 — AWS Mumbai serves the revision, and the safety checks stay green**
  CHECK: `tools/deploy/verify-live.sh`, `tools/deploy/verify-safety.sh`, `tools/deploy/verify-swing.sh`
  EXPECT: exit 0
  EVIDENCE: pending
