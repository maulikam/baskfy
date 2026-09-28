# Gates: F3-2 — schema and data

- [x] S1: migration 0059 adds sleeves F3N/F3B to the fo_sleeve enum, group F3 to the config and audit checks, CREDIT_SPREAD to the three structure checks, ADD to the plan kinds, and creates fo_index_daily; models agree; downgrade drops what can be dropped and says what an enum cannot
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_migration_0059.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed in 11.07s

- [x] S2: the index daily history job writes NIFTY and BANKNIFTY OHLC from Kite history into fo_index_daily (idempotent, rounded at write), backfills from a start date, and extends each evening; the minute collector covers NIFTY BANK
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_fno_index_daily.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 6 passed in 4.16s

- [x] S3: 75-minute bars aggregate correctly from minute bars (five a session, the last one closing 15:30, a partial session excluded)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_fno_directional.py -k 'seventy_five' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 4 passed, 45 deselected in 0.09s
