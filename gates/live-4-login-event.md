# Gates: LV4 — login is the event; per-sleeve state; the monitor reloads its watchlist

Scope: a session supervisor that reacts to a Kite token at any hour; heartbeats every process can be judged by; the swing monitor takes new setups without a restart and waits for a late login; the API says per sleeve what state it is in and why; the login queues every sleeve's honest scan.

- [ ] L1: the supervisor detects a token arriving (blob mtime), runs a reconcile pass at once and every 10 s in session, seeds risk exposure from holdings, and writes heartbeats — driven by fakes in tests
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_session_supervisor.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending

- [x] L2: swing monitor — start at 09:15, add an eligible setup at 11:00, deliver a valid trigger: the running loop observes it exactly once without restart; a removed name stops being watched
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_swing_monitor.py -k 'reload' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 1 passed, 61 deselected in 0.85s

- [ ] L3: swing monitor main() waits for a Kite session (until 15:20) instead of exiting, and an empty list at start still enters the loop so a later setup is watched
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_swing_monitor.py -k 'late_login or empty_start' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending

- [ ] L4: GET /sleeves/state answers a deterministic state and reason per sleeve for: no session; session before the open; 10:30 login with a TWT plan expired (missed_window); a queued scan (scanning); a plan today (plan_ready); a fresh monitor heartbeat (monitoring); an open protection issue (blocked)
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_sleeve_state.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending

- [ ] L5: the login callback queues the swing live scan AND the TWT and VBT closed-session scans, and never fails the login when the queue is down
  CHECK: cd decile-blueprint && uv run pytest services/api/tests/test_brokers_login_refresh.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: pending

- [ ] L6: the web shows the sleeve state chip beside each Scan button on /swing, /twt and /vbt, with the reason on hover, and never labels an EOD detector as an intraday scan
  CHECK: cd decile-blueprint/apps/web && npx vitest run src/components/screens/__tests__/sleeve-state.test.tsx 2>&1 | grep -E 'Tests ' | tail -1
  EXPECT: /Tests\s+\d+ passed/
  EVIDENCE: pending

- [ ] L7: the container plumbing names the new service everywhere it must: Dockerfile command, compose service, deploy-swing RESTART_SERVICES, ship.sh count
  CHECK: grep -c 'session-supervisor' decile-blueprint/infra/docker/Dockerfile.desk decile-blueprint/infra/docker/compose.prod.yml tools/deploy/deploy-swing.sh tools/deploy/ship.sh | awk -F: '$2==0{bad=1} END{print bad?"MISSING":"ALL NAMED"}'
  EXPECT: ALL NAMED
  EVIDENCE: pending

- [ ] L8: openapi.json + TS client regenerated; lint clean on both trees; desk suite green
  CHECK: cd decile-blueprint && a=$(shasum packages/api-client/openapi.json packages/api-client/src/generated/schema.ts | shasum); make openapi >/dev/null 2>&1; make client >/dev/null 2>&1; b=$(shasum packages/api-client/openapi.json packages/api-client/src/generated/schema.ts | shasum); [ "$a" = "$b" ] && echo IN SYNC || echo DRIFT
  EXPECT: IN SYNC
  EVIDENCE: pending
