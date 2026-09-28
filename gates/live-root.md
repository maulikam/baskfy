# Gates: LV root — live at any login time (integration)

Scope: LV0–LV10 merged into one product change, verified, deployed, documented.

- [x] R1: every leaf gates file is fully met or honestly abandoned
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/live-0-audit.md gates/live-1-market-data.md gates/live-2-reconcile.md gates/live-3-risk.md gates/live-4-login-event.md gates/live-5-eq-bars.md gates/live-6-lifecycle.md gates/live-7-integration.md gates/live-8-live-scans.md gates/live-9-qulla-exits.md gates/live-10-pyramiding.md
  EXPECT: ALL MET
  EVIDENCE: gates/live-10-pyramiding.md: 4 gates | ALL MET (76 met)

- [x] R2: the desk suite is green (the desk must be able to rebalance on any Friday)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q -x 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 2539 passed, 143 skipped, 234 warnings, 12 subtests passed in 88.83s (0:01:28)

- [x] R3: the screener trees' lint is clean (ruff, format, mypy strict, TS)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED
  EXPECT: LINT CLEAN
  EVIDENCE: LINT CLEAN

- [x] R4: no order path was widened — no new auto-execute flag, no default flipped
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff 3eea54a -- decile-blueprint/infra/docker/compose.prod.yml kite-momentum-rebalancer/app/config.py | grep -E '^\+.*(AUTO_EXECUTE|EXECUTION_ENABLED|DRY_RUN).*(true|True)' | grep -v '^\+\s*#' | wc -l | tr -d ' '
  EXPECT: 0
  EVIDENCE: 0

- [x] R5: every number in the final report was re-measured at report time (ledger pasted N of N)
  EVIDENCE: measured 05:20 IST, 28 Sep 2026, by `grep -c '^- \[x\]'` per gates file: 0-audit 5/5, 1-market-data 7/7, 2-reconcile 10/10, 3-risk 6/6, 4-login-event 8/8, 5-eq-bars 7/7, 6-lifecycle 6/6, 7-integration 6/6, 8-live-scans 9/9, 9-qulla-exits 8/8, 10-pyramiding 4/4 — 76 of 76 leaf gates; root R1–R4 met by the runner after the LV10 commit (R1 `ALL MET (76 met)`, R2 desk suite `2539 passed`, R3 `LINT CLEAN`, R4 `0`). Commits since 84cbb71: LV0 84cbb71, LV1 00725f5, LV2 2195d88, LV3 cfccfa2, LV4 7a89488, LV5 4af83e5, LV6 ece537a, LV8 dde3c1e, LV7 cc0f36d, LV9 6458c75, LV10 669cf4b, plus this ledger commit. Fourth deploy, after the close on 28 Sep (15:50 IST): c600217 from a clean worktree, `DEPLOYED c600217`, `SWING OK`, 15 services (Laya's container, volume and image gone from the box), alembic 0059_f3_directional, the five TWT positions OPEN with GTTs, disk 27 %, 1,372 MB free. Third deploy of the night: 669cf4b at ~04:45–05:00 IST from a clean worktree, `✓ DEPLOYED 669cf4b … sixteen services up`, `pins=3 running=16 twt_execution_true=1 twt_auto_true=1`, `ship exit=0`; on the box afterwards alembic `0058_qulla_exits`, `docker ps` 16 up, every sleeve book empty (`0|0|0` open positions — so no position's exit changed mid-flight), `BASKFY_EQ_BARS_ENABLED=false`, disk 26 %, `/api/v1/meta/status` `live_quotes: True`.
