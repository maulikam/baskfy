# Gates: F3-4 — the evening scan

- [x] N1: the nightly F3 scan writes one fo_scan row per underlying per session — CANDIDATE with direction, levels, the weekly range, the chosen expiry and strikes and the proposed lots in detail; NO_SIGNAL when direction is NONE; NO_DATA when history is short; OPEN_POSITION when one is held; PAUSED under the book's pauses
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_fno_f3_scan.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 6 passed in 4.05s

- [x] N2: the scan is behind BASKFY_FNO_SCAN_ENABLED like F1/F2's, on the same Beat entry, and places nothing (the safety proof scans it)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_fno_safety_proof.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 16 passed in 7.79s
