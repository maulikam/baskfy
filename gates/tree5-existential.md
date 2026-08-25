# Gates: Tree 5 — Existential (do these first)

Scope: Off-machine survival, CI truth, and portfolio.db backup restore proof before any feature work.

ABANDON: G1 No remote URL or GitHub credentials on this machine — NEEDS-MAULIK item 14. Maulik must `git remote add origin <url>` and push.

- [ ] G1: Git remote exists and `git remote -v` shows origin
  CHECK: git remote -v
  EXPECT: origin
  EVIDENCE: ABANDON — `git remote -v` empty; 243 commits local-only; NEEDS-MAULIK #14

- [x] G2: Local CI mirror runs and reports pass/fail counts (GitHub Actions blocked until G1)
  CHECK: tools/ci-local.sh 2>&1 | tail -3
  EXPECT: passed
  EVIDENCE: `passed 8   failed 7   skipped 6` (23 Aug 2026, ~14 min)

- [x] G3: Latest portfolio.db backup integrity is ok
  CHECK: cd kite-momentum-rebalancer && ./.venv/bin/python -m scripts.backup --check 2>&1
  EXPECT: "integrity": "ok"
  EVIDENCE: latest 20260823-233103 · integrity ok · fills 9262 · trades 8198

- [x] G4: Backup restore drill — restored copy matches backup manifest
  CHECK: cd kite-momentum-rebalancer && ./.venv/bin/python -m scripts.restore_drill 2>&1
  EXPECT: "ok": true
  EVIDENCE: restore_drill ok:true · manifest_at 2026-08-23T23:31:03 · mismatches {}

- [x] G5: Existential state recorded in NEEDS-MAULIK and docs/00-merge-status.md
  CHECK: rg -n "243 commits|148 uncommitted|restore drill|ci-local" NEEDS-MAULIK.md docs/00-merge-status.md
  EXPECT: /
  EVIDENCE: NEEDS-MAULIK §14 + docs/00-merge-status Tree 5 section updated 23 Aug 2026
