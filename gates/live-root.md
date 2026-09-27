# Gates: LV root — live at any login time (integration)

Scope: LV0–LV8 merged into one product change, verified, deployed, documented.

- [x] R1: every leaf gates file is fully met or honestly abandoned
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/live-0-audit.md gates/live-1-market-data.md gates/live-2-reconcile.md gates/live-3-risk.md gates/live-4-login-event.md gates/live-5-eq-bars.md gates/live-6-lifecycle.md gates/live-7-integration.md gates/live-8-live-scans.md
  EXPECT: ALL MET
  EVIDENCE: gates/live-8-live-scans.md: 9 gates | ALL MET (64 met)

- [x] R2: the desk suite is green (the desk must be able to rebalance on any Friday)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q -x 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 2527 passed, 143 skipped, 234 warnings, 12 subtests passed in 83.77s (0:01:23)

- [x] R3: the screener trees' lint is clean (ruff, format, mypy strict, TS)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED
  EXPECT: LINT CLEAN
  EVIDENCE: LINT CLEAN

- [x] R4: no order path was widened — no new auto-execute flag, no default flipped
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && git diff 3eea54a -- decile-blueprint/infra/docker/compose.prod.yml kite-momentum-rebalancer/app/config.py | grep -E '^\+.*(AUTO_EXECUTE|EXECUTION_ENABLED|DRY_RUN).*(true|True)' | grep -v '^\+\s*#' | wc -l | tr -d ' '
  EXPECT: 0
  EVIDENCE: 0

- [x] R5: every number in the final report was re-measured at report time (ledger pasted N of N)
  EVIDENCE: measured 03:55 IST, 28 Sep 2026, by `grep -c '^- \[x\]'` per gates file: live-0 5/5, live-1 7/7, live-2 10/10, live-3 6/6, live-4 8/8, live-5 7/7, live-6 6/6, live-7 6/6, live-8 9/9 — 64 of 64 leaf gates; root R1–R4 met by the runner (R1 `ALL MET (64 met)`, R2 `2527 passed, 143 skipped`, R3 `LINT CLEAN`, R4 `0`). Commits `git log 84cbb71^..HEAD`: 84cbb71 LV0, 00725f5 LV1, 2195d88 LV2, cfccfa2 LV3, 7a89488 LV4, 4af83e5 LV5, ece537a LV6, dde3c1e LV8, plus the LV7 commit that carries this ledger. Screener suite: `1 failed, 13554 passed, 19 skipped, 1 xfailed in 32:13` with the one failure fixed and its file re-run `33 passed` (I1). Web `292 passed` (I2, `Tests 292 passed`). Box: `running=16`, alembic `0057_live_scans`, `live_quotes: True`, `twt_execution_true=1 twt_auto_true=1`, `BASKFY_EQ_BARS_ENABLED=false`, disk 25 %.
