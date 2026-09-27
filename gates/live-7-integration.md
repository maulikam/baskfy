# Gates: LV7 — integration, deploy, documentation, report

Scope: everything above on the box and in the record.

- [ ] I1: screener Python suites green (core, providers, execution, api, worker) — run serially with the desk suite, never together
  CHECK: cd decile-blueprint && uv run pytest 2>&1 | tail -1
  EXPECT: /^\d+ passed/
  EVIDENCE: pending

- [ ] I2: web tests green
  CHECK: cd decile-blueprint/apps/web && npx vitest run 2>&1 | grep -E 'Tests ' | tail -1
  EXPECT: /Tests\s+\d+ passed/
  EVIDENCE: pending

- [ ] I3: deployed to the box with ship.sh outside market and nightly windows; sixteen services up; migrations 0055 and 0056 applied
  EVIDENCE: pending

- [ ] I4: on the box after deploy: /meta/status reports live_quotes true while a session exists regardless of the API's DRY_RUN; /sleeves/state answers; session-supervisor heartbeat row exists; eq_minute_bar exists
  EVIDENCE: pending

- [ ] I5: docs/00-merge-status.md has an LV section loud about what is NOT done; DECISIONS-MERGE.md points at docs/live/DECISIONS-LV.md; NEEDS-MAULIK.md carries the step-5 questions with options
  CHECK: grep -c '^## LV' docs/00-merge-status.md && grep -c 'DECISIONS-LV' docs/DECISIONS-MERGE.md && grep -c '^## LV' NEEDS-MAULIK.md
  EXPECT: /[1-9]/
  EVIDENCE: pending

- [ ] I6: the review document itself is annotated with what each P-item became (a status column), so the next reader knows what is built and what is not
  CHECK: grep -c 'LV[0-9]' docs/trading-readiness-review-2026-09-27.md
  EXPECT: /[1-9]/
  EVIDENCE: pending
