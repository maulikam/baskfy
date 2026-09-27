# Gates: LV10 — his pyramiding on all three sleeves (Maulik, 28 Sep 2026)

Scope: DECISIONS-LV LV9.0 (3). A fresh qualifying setup in a held name is a new entry with its own size and stop; at most two open entries per name; re-entry after an exit on a new signal. Per-sleeve `SizingConfig.pyramiding` (default true), `max_entries_per_name` (2).

- [x] P1: the three planners refuse a held name only when pyramiding is off or the name already has max_entries_per_name open entries; the detail says which; slots count entries, not names
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_pyramiding.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 13 passed in 0.29s

- [x] P2: the evening and live builders hand the planners per-name entry counts (TWT book_state, VBT evening, swing eod, live_scan), and every exit/raise line carries the position it acts on
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_pyramiding_worker.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed in 3.06s

- [x] P3: the desk's buy handlers (TWT, VBT limit and market, swing trigger) allow a second entry under the cap and refuse a third; exit and raise handlers act on the line's position when a name holds two; the reconciler judges each position's own GTT and compares holdings per symbol against the sum
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_twt_execute.py tests/test_vbt_execute.py tests/test_swing_execute.py tests/test_reconcile.py -k 'pyramid or second_entry or two_positions or position_id' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 4 passed, 234 deselected in 1.51s

- [x] P4: docs (three 04s, DECISIONS TW21 / VB18 / SW entry) and the trade cards say it; both suites green; lint clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c '^## TW21' docs/twt/DECISIONS-TW.md; grep -c '^### VB18' docs/vbt/DECISIONS-VB.md; cd decile-blueprint && make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED
  EXPECT: /^1\n1\nLINT CLEAN$/m
  EVIDENCE: 1 | LINT CLEAN
