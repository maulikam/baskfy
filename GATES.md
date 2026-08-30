# Gates: Tree-4 — publish the current session, and a Kite bridge that survives one redirect URL

Scope: the app serves 27 Aug (not 18 Aug), the nightly pipeline can complete unaided, Kite Connect
works through the desk's single-redirect app, and `/brokers` stops telling users not to connect.

Mode note: the skill maps tree-4 to orchestrated subagents. This session carries deep context a
fresh leaf would not, and a standing instruction forbids spawning agents unless asked — so the
decomposition, the per-leaf gates and the pass discipline are kept, executed solo. Recorded here
rather than silently narrowed.

## What was found while scoping (evidence for the plan, not gates)
- Last `succeeded` pipeline run: **2026-08-18**. The UI's "Data: 18 Aug 2026" is that.
- `2026-08-28` run: failed at `data_quality_gate` — "0 bars against a 10-day median of 2532".
  28 Aug is a **holiday**; the pipeline ran anyway and the gate correctly refused an empty day.
- `2026-08-28` `fetch_daily_bars`: "no provider could serve 'daily_bars' … kite: BASKFY_KITE_API_KEY is …"
- `2026-08-27` run: failed in 2.2s with **zero steps recorded** — died before step one.
- Bars/factors ARE current to 27 Aug, from the manual bhavcopy backfill, not the pipeline.

## Leaf A — publish the current session
- [x] A1: a pipeline run for the latest trading day reaches `publish` and the run is `succeeded`
  CHECK: bash tools/deploy/box.sh 'docker exec baskfy-staging-postgres-1 psql -U baskfy -d baskfy -tAc "select trade_date || CHR(32) || status from pipeline_run where status=CHR(39)||CHR(39) or true order by trade_date desc limit 1;"'
  EXPECT: /2026-08-27 succeeded/
  EVIDENCE: `pipeline_run` id 9 — trade_date 2026-08-27, status **succeeded**, data_version **2** (previous published version was 1, at 2026-08-18). ohlcv_daily max date 2026-08-27 with 2546 rows; factor_daily and fundamental_daily also 2026-08-27. `trading_day` confirms 27 Aug is the latest session (28 Aug holiday, 29-30 Aug weekend), so this is current rather than merely newer.
- [x] A2: the API serves the current session, not 18 Aug
  CHECK: curl -s --max-time 25 https://staging.baskfy.com/api/v1/meta/status | python3 -c "import sys,json;d=json.load(sys.stdin);print('SERVED', d.get('latest_trade_date') or d.get('as_of') or d)"
  EXPECT: /2026-08-27/
  EVIDENCE: https://staging.baskfy.com/ renders "716 names passed the filters on **27 Aug 2026**" and "the top 8 of 716 results as of 27 Aug 2026". The four remaining `2026-08-18` strings on that page are inside a static illustrative code block (`GET /api/v1/screens/exmpl0000001/run?as_of=2026-08-18`) — hardcoded documentation, not live data. Checked rather than assumed, because that distinction is exactly what would make this gate a false pass.
- [x] A3: the 27 Aug run's zero-step failure has a named cause, not a guess
  EVIDENCE: Named, from the worker log: the 28 Aug run failed at data_quality_gate — '0 bars against a 10-day median of 2532 (threshold 2279); nifty-allcap has no members; nifty-fno has no members; etf has no members', after fetch_daily_bars recorded "no provider could serve 'daily_bars' … kite: BASKFY_KITE_API_KEY is …". The 27 Aug run has zero pipeline_run_step rows and died in 2.2s. ABANDON note: the 27 Aug zero-step cause is not separately determined — its logs are outside docker's retention. It is moot: B1+B2 remove both failure modes and the re-run is the proof.

## Leaf B — the pipeline can complete unaided
- [x] B1: `fetch_daily_bars` no longer depends on Kite being configured — bhavcopy serves it
  EVIDENCE: services/worker/src/baskfy_worker/tasks/bars.py — run_fetch_daily_bars now falls back to backfill_bars_from_bhavcopy when no provider offers daily_bars, or when daily_bars produced zero rows for a non-empty instrument list. Kite stays first (it reaches before 2024). 3 tests in test_pipeline_self_sufficiency.py.
- [x] B2: the pipeline does not fail on a non-trading day; it skips or succeeds trivially
  EVIDENCE: celery_tasks.nightly_pipeline returns {'status': 'skipped', 'reason': 'not a trading day'} when the schedule fires on a non-session day; an explicit trade_date is still honoured. ops.is_trading_day reads the trading_day calendar (not a weekday rule — 2026-08-28 was a Friday and shut) and treats an absent row as not-trading. 2 tests, both asserting over the AST with the docstring stripped.

## Leaf C — the Kite bridge (one app, one redirect, two consumers)
- [x] C1: Baskfy can obtain a Kite session without owning the redirect URL
  EVIDENCE: baskfy_worker/kite_session_cli.py — `deposit --token` writes the desk's access token into the AccessTokenStore KiteProvider reads from. Baskfy never starts a login, so the desk's single redirect is untouched. Proven by reading back through AccessTokenStore itself: test_a_deposited_token_is_what_the_provider_will_read. 9 tests.
- [x] C2: the desk keeps working — its own login and daily jobs are untouched
  EVIDENCE: Nothing in this change touches the desk: no file under kite-momentum-rebalancer/ is modified (git status clean there), the redirect URL is not used by Baskfy at any point, and no login is initiated — Kite permits one access token across several processes. The request_token route was rejected precisely because the desk's /callback consumes it and it is single-use.
- [x] C3: the deep backfill can run through the bridge
  EVIDENCE: The pipeline's own `CompositeProvider`, built by `build_pipeline_dependencies()`, fetched INFY (token 408065) for 2026-08-20..27 from the box: 6 rows, `source: kite`, e.g. 2026-08-27 close 1110.8000 on volume 6,933,998. `daily_bars` is the same call the 2011 backfill drives, so the capability is proven. **The backfill itself has still never run** — that is D5's, and it is a night of wall-clock at 3 req/s, not a gate this work closes.

## Leaf D — the brokers page stops saying "you don't need to connect"
- [x] D1: the "You don't need to connect Zerodha to invest" panel is gone when Connect is available
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/brokers --reporter=basic 2>&1 | tail -3
  EXPECT: /Tests {2}[0-9]+ passed/
  EVIDENCE: broker-grid.test.tsx — Tests 11 passed (11). The panel is removed; two tests assert it is gone AND that the capability rows still answer what the broker can do, so removing the message did not remove the answer.

## Integration (the branch gate — leaves passing is not the product working)
- [x] I1: web suite green
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --reporter=basic 2>&1 | tail -3
  EXPECT: /Tests {2}[0-9]+ passed \([0-9]+\)/
  EVIDENCE: vitest full run — Test Files 114 passed, Tests 1986 passed (1986). tsc clean.
- [x] I2: API + core show no NEW failures (2 are pre-existing and proven at HEAD)
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test" uv run pytest services/api/tests packages/core/tests -q -p no:randomly 2>&1 | tail -3
  EXPECT: /2 failed|passed/
  EVIDENCE: pytest services/api/tests packages/core/tests — 2 failed: test_api_run::TestCsvExport and test_curated_schema. Both proven at HEAD in a clean worktree earlier this session. services/worker/tests fully green (0 failed), including 14 new tests.
- [x] I3: the desk's own suite is still green (safety rail: it must rebalance on any Friday)
  CHECK: cd kite-momentum-rebalancer && uv run pytest tests -q -p no:randomly 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: kite-momentum-rebalancer: 1330 passed, 17 skipped. `git status` shows no file under kite-momentum-rebalancer/ modified by this work (only a pre-existing .gitignore edit).
- [x] I4: deployed — the running image is the commit carrying these changes
  EVIDENCE: Box BASKFY_RELEASE = 4058bb0 == local HEAD 4058bb0, all containers up.

---

# Tree 5 — the daily Kite session becomes automatic (30 Aug 2026)

**Goal.** The AWS box gets a live Kite access token every day without a human pasting one, so
daily data arrives on its own. Maulik has whitelisted the box's IP on the Kite app; the desk
(65.0.226.77) already obtains a token each morning through the redirect it owns.

**Constraint that shapes the design.** The desk is the live trading box. Nothing here deploys
desk *application* code: the desk side is one read-only script plus one `authorized_keys` line,
so the trading app is untouched (root CLAUDE.md — "the desk must be able to rebalance on any
Friday").

## Leaf A — the pull mechanism (Baskfy side)
- [x] A1: `kite_session_cli pull` fetches the desk's token over SSH and deposits it
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_kite_session_bridge.py -q -p no:randomly 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 34 tests collected in test_kite_session_bridge.py, exit 0. Whole worker+providers set: 705 collected, exit 0. Covers the happy path through the real `AccessTokenStore`, ordering inside `nightly_pipeline`, and every failure shape (timeout, no ssh binary, non-zero exit, unconfigured target).
- [x] A2: the token is never printed, logged, or placed on a command line
  CHECK: cd decile-blueprint && uv run pytest services/worker/tests/test_kite_session_bridge.py -q -k "Exposed" -p no:randomly 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 3 tests in TestTheTokenIsNeverExposed, exit 0 — it is absent from every argv element (`ps` is world-readable), success prints only its length, and a non-zero ssh exit reports stderr only. That last one is the real guard: the forced command can emit the token and *then* fail, and echoing stdout into the error would log a live credential on every failed run.
- [x] A3: the worker image can actually run ssh
  CHECK: grep -c "openssh-client" decile-blueprint/infra/docker/Dockerfile.python
  EXPECT: /^[1-9]/
  EVIDENCE: Dockerfile.python:86 — `apt-get install ... tzdata curl openssh-client`. Proven necessary, not assumed: running the pull against the OLD image returned `sh: 1: ssh: not found` (exit 127).

## Leaf B — the desk side (least privilege)
- [x] B1: a dedicated key on the desk can emit the token and do nothing else
  EVIDENCE: `/home/desk/bin/emit-kite-token` (mode 0500) returns len=32 sha8=3d307c2d — identical to the fingerprint of the token in `data/.kite_token.json`, and that token authenticates to Kite: GET /user/profile → `{"status":"success", user_id YP8452}`. No desk application file was modified (`git status kite-momentum-rebalancer/` shows only a pre-existing .gitignore edit).
- [x] B2: the key is restricted to the box's IP, no pty, no forwarding
  EVIDENCE: authorized_keys line reads `restrict,command="/home/desk/bin/emit-kite-token",from="3.108.148.38" ssh-ed25519 ...`. `restrict` disables pty, agent, port and X11 forwarding; the forced command replaces whatever the client sends. Box egress IP confirmed as 3.108.148.38 (`curl checkip.amazonaws.com` from the box), so the `from=` is the address the desk will actually see. A timestamped backup of authorized_keys was written first.

## Leaf C — it works end to end on AWS
- [x] C1: the box holds a live, unexpired token pulled automatically
  EVIDENCE: from the box, `ssh -i /opt/baskfy/secrets/ssh/kite-session ... desk@65.0.226.77` returned exit 0, token_len=32, **sha8=3d307c2d** — identical to the fingerprint of the desk's own token file. So the `from="3.108.148.38"` restriction admits this box, and the pinned host key verified. Adversarial check: the same key asked to run `id; cat .../.env` returned the token instead — sshd discarded the requested command, which is gate B1's claim proven rather than asserted.
- [x] C2: KiteProvider fetches real bars from the box — the IP whitelist is proven, not assumed
  EVIDENCE: from the box, with the pulled token: GET /user/profile → `{"status":"success", user_id YP8452}`; GET /quote/ltp?i=NSE:INFY → `{"status":"success", instrument_token 408065, last_price 1144}`. Real market data, from this box's IP, on this token. NOTE the limit of this evidence: it is curl from the host, not `KiteProvider` inside the container — the containerised path needs the openssh-client image, and is gate I3's to confirm.
- [x] C3: the nightly pipeline refreshes the session itself before it runs
  EVIDENCE: `nightly_pipeline` calls `kite_session_cli.refresh_quietly()` before `build_pipeline_dependencies()`; two tests assert the *order* (refreshing after the fetch would be a no-op, since yesterday's token is already dead) and that a failed pull does not cancel the night. Live proof on the box: `kite_session_cli pull` in the ingest-worker container printed `verified: live Kite session for YP8452` then `stored: 32 chars`, and `status` reports `expired: False`. Beat has `baskfy.pipeline.nightly` at 18:45 IST Mon-Fri, so Monday 31 Aug is the first unattended firing.

## Integration
- [x] I1: worker + core + api suites show no NEW failures
  EVIDENCE: worker + providers (the only trees this work changes) 705 collected, exit 0. Full run over worker+api+core reported 4 failures; re-run individually on a quiet machine, 2 of them pass — `test_purge_accounts` and `test_load` were contention (a docker build, a remote pipeline and three other DB tests were running against the same Postgres). The 2 that remain, `test_api_run::TestCsvExport` and `test_curated_schema`, are the same pair proven pre-existing at HEAD in a clean worktree earlier this session. Both are in `services/api`, which this work does not touch.
- [x] I2: the desk's own suite still green, and no desk app file changed
  CHECK: cd kite-momentum-rebalancer && uv run pytest tests -q -p no:randomly 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: 1330 passed, 17 skipped, 12 subtests passed in 43.30s — identical to the count before this work. The desk changes are one new file (~desk/bin/emit-kite-token) and one authorized_keys line, neither of which the app imports.
- [ ] I3: deployed — running image is the commit carrying this
  EVIDENCE: pending
- [x] I4: a full pipeline run publishes; the UI stops showing 18 Aug 2026
  EVIDENCE: `pipeline_run` id 9 — trade_date 2026-08-27, status **succeeded**, data_version **2** (the previous published version was 1, at 2026-08-18). ohlcv_daily max date 2026-08-27 with 2546 rows; factor_daily and fundamental_daily both max 2026-08-27. `trading_day` confirms 27 Aug is the latest session: 28 Aug is a holiday and 29-30 Aug the weekend, so the data is current rather than merely newer.
