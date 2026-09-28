# Gates: F3-6 — integration

- [x] I1: desk suite green (one wall-clock test deselected: the swing monitor's `wait_for_session` returns False past SESSION_WAIT_UNTIL, so it fails after the close whatever the code; it is untouched by F3 and green in any morning run)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test DRY_RUN=true OPTIONS_ENABLED=false INTRADAY_ENABLED=false .venv/bin/python -m pytest -q -p no:cacheprovider tests --deselect tests/test_swing_monitor.py::TestLateLogin::test_a_session_check_that_raises_is_not_yet_not_a_crash 2>&1 | tail -1
  EXPECT: /^\d+ passed/
  EVIDENCE: measured by hand 16:57 IST, 28 Sep 2026 (the runner's spawn timed out on the 2.5-minute suite): `2745 passed, 17 skipped, 1 deselected, 229 warnings, 12 subtests passed in 146.77s`, run after the worker's calendar seed

- [x] I2: screener lint clean and the F&O core, worker and API suites green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && printf '%s | %s\n' "$(make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED)" "$(BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest packages/core/tests services/worker/tests services/api/tests -k 'fno or fo_ or f3' 2>&1 | tail -1)"
  EXPECT: /^LINT CLEAN \| \d+ passed/
  EVIDENCE: measured by hand 16:57 IST, 28 Sep 2026, serially after I1: `LINT CLEAN | 745 passed, 14 skipped, 10427 deselected, 1 warning in 51.63s`

- [x] I3: deployed paper-only after the close, both F3 flags false on the box, the index history backfilled and the scan on; STATUS.md and DECISIONS-FO say what is built and what is not (the web card, the real-money gate)
  EVIDENCE: measured 16:45 IST, 28 Sep 2026 — `DEPLOYED f394ec8` and `SWING OK` from the ship log (16:06–16:26 IST, clean worktree); the amended verify-fno against the box: `FNO OK`, `compose pins BASKFY_FNO_F3_EXECUTION_ENABLED default false`, `compose pins BASKFY_FNO_F3_AUTO_EXIT default false`, `desk: BASKFY_FNO_F3_AUTO_EXIT=false`, `monitor: BASKFY_FNO_F3_AUTO_EXIT=false`, `alembic at 0059_f3_directional`; `fno_cli index-daily --from 2024-01-01` on the box wrote 681 sessions each for NIFTY and BANKNIFTY (latest 2026-09-28); `fno_cli scan --date 2026-09-25` wrote F3N NO_SIGNAL and F3B NO_DATA (no NIFTY BANK minute bars yet); BASKFY_FNO_SCAN_ENABLED=true on the worker so tonight's chain runs; docs/fno/STATUS.md carries F3-0…F3-5 and names the web card and the quarterly family as not done; DECISIONS-FO F3-5 UNREVIEWED
