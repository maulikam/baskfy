# Gates: F3-3 — the EOD re-test (what can be claimed)

- [x] R1: the daily-rules-only proxy of F3 (levels, direction, next-session entry at settle, exits by close/settle) runs on real NIFTY and BANKNIFTY option bhavcopies fetched through the provider for at least 2024-01 → 2026-09, with costs, and its trades, R and per-year numbers are written to docs/fno/evidence/f3-retest.md
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && test -s docs/fno/evidence/f3-retest.md && grep -c 'trades' docs/fno/evidence/f3-retest.md
  EXPECT: /[1-9]/
  EVIDENCE: 2

- [x] R2: the doc says plainly which of his rules the EOD proxy cannot test (the 75-minute confirm, the intraday alignment, the intraday level break, the "cut at 50") and no number is claimed for them
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c 'cannot test\|not tested' docs/fno/evidence/f3-retest.md
  EXPECT: /[1-9]/
  EVIDENCE: 2
