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
- [ ] A1: a pipeline run for the latest trading day reaches `publish` and the run is `succeeded`
  CHECK: bash tools/deploy/box.sh 'docker exec baskfy-staging-postgres-1 psql -U baskfy -d baskfy -tAc "select trade_date || CHR(32) || status from pipeline_run where status=CHR(39)||CHR(39) or true order by trade_date desc limit 1;"'
  EXPECT: /2026-08-27 succeeded/
  EVIDENCE: pending
- [ ] A2: the API serves the current session, not 18 Aug
  CHECK: curl -s --max-time 25 https://staging.baskfy.com/api/v1/meta/status | python3 -c "import sys,json;d=json.load(sys.stdin);print('SERVED', d.get('latest_trade_date') or d.get('as_of') or d)"
  EXPECT: /2026-08-27/
  EVIDENCE: pending
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
- [ ] C3: the deep backfill can run through the bridge
  EVIDENCE: pending

## Leaf D — the brokers page stops saying "you don't need to connect"
- [ ] D1: the "You don't need to connect Zerodha to invest" panel is gone when Connect is available
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/brokers --reporter=basic 2>&1 | tail -3
  EXPECT: /Tests {2}[0-9]+ passed/
  EVIDENCE: pending

## Integration (the branch gate — leaves passing is not the product working)
- [ ] I1: web suite green
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --reporter=basic 2>&1 | tail -3
  EXPECT: /Tests {2}[0-9]+ passed \([0-9]+\)/
  EVIDENCE: pending
- [ ] I2: API + core show no NEW failures (2 are pre-existing and proven at HEAD)
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test" uv run pytest services/api/tests packages/core/tests -q -p no:randomly 2>&1 | tail -3
  EXPECT: /2 failed|passed/
  EVIDENCE: pending
- [ ] I3: the desk's own suite is still green (safety rail: it must rebalance on any Friday)
  CHECK: cd kite-momentum-rebalancer && uv run pytest tests -q -p no:randomly 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: pending
- [ ] I4: deployed — the running image is the commit carrying these changes
  EVIDENCE: pending
