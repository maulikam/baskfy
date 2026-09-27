# Gates: LV root — live at any login time (integration)

Scope: LV0–LV7 merged into one product change, verified, deployed, documented.

- [ ] R1: every leaf gates file is fully met or honestly abandoned
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/live-0-audit.md gates/live-1-market-data.md gates/live-2-reconcile.md gates/live-3-risk.md gates/live-4-login-event.md gates/live-5-eq-bars.md gates/live-6-lifecycle.md gates/live-7-integration.md
  EXPECT: ALL MET
  EVIDENCE: pending

- [ ] R2: the desk suite is green (the desk must be able to rebalance on any Friday)
  CHECK: cd kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q -x 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: pending

- [ ] R3: the screener trees' lint is clean (ruff, format, mypy strict, TS)
  CHECK: cd decile-blueprint && make lint 2>&1 | tail -4
  EXPECT: /Success: no issues|no issues found/
  EVIDENCE: pending

- [ ] R4: no order path was widened — no new auto-execute flag, no default flipped
  CHECK: git diff 3eea54a -- decile-blueprint/infra/docker/compose.prod.yml kite-momentum-rebalancer/app/config.py | grep -E '^\+.*(AUTO_EXECUTE|EXECUTION_ENABLED|DRY_RUN).*(true|True)' | grep -v '^\+\s*#' | wc -l | tr -d ' '
  EXPECT: 0
  EVIDENCE: pending

- [ ] R5: every number in the final report was re-measured at report time (ledger pasted N of N)
  EVIDENCE: pending
