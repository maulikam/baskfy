# Gates: LV9 — Qullamaggie exits on TWT and VBT; TWT auto-execute sends the partial (Maulik, 28 Sep 2026)

Scope: DECISIONS-LV LV9.0 (2). The swing book's stop rule replaces TWT's trail and VBT's EMA exit. Nothing here places anything; the desk's confirm (or TWT's flagged auto-execute) does.

- [x] Q1: one rule: TWT's and VBT's evening management call `baskfy_core.swing.stops.manage` through pure adapters (`baskfy_core.exits.qulla`) — no second implementation of the partial, breakeven or trail rule
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core/tests/test_qulla_exits.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 12 passed in 0.26s

- [x] Q2: migration 0058 adds partial_done/trail (both sleeves), TWT's queued-sell columns, MA_TRAIL and PARTIAL reasons, VBT's RAISE_GTT_STOP kind; models agree; downgrade clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_migration_0058.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed in 10.73s

- [x] Q3: the TWT evening writes the partial (bar 3–5, green), the breakeven raise and the MA-trail exit onto positions and the plan carries SELL_AT_OPEN (partial quantity or all) and RAISE_GTT_STOP lines; the 20 % ratchet no longer runs when qulla_exits is on; a young history (no MA20) holds
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_twt_qulla.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 7 passed in 5.58s

- [x] Q4: the VBT evening does the same — partial and MA-trail sells as SELL_AT_OPEN with the reason, the breakeven as RAISE_GTT_STOP; the stop and no-bar rules unchanged; the EMA exit no longer fires when qulla_exits is on
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_vbt_qulla.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 6 passed in 3.02s

- [x] Q5: the desk: TWT's SELL_AT_OPEN is a MARKET sell with protection through the gateway, recorded in lv_exit_order and booked from the fill (partial leaves the position open with partial_done and a re-sized GTT; a full sell closes it with the reason); VBT's RAISE_GTT_STOP cancels and re-arms, never lowers; VBT's partial sets partial_done; dry-run rehearses all of it
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_twt_execute.py tests/test_vbt_execute.py -k 'sell or raise or partial or exit' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 17 passed, 77 deselected in 0.92s

- [x] Q6: twt-auto's kinds are exactly the desk's executable kinds, now including SELL_AT_OPEN; the drain sends sells first; VBT gains no flag
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_twt_auto.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 41 passed in 2.43s

- [x] Q7: the trade cards and /lifecycle print the new rule for TWT and VBT (partial, breakeven, MA trail); tests that pinned "no partial, no MA exit" now pin LV9.0 and cite it; docs/twt/04 §7, docs/vbt/04 §6, the two 03s, TW20 and VB17 written
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -c '^## TW20' docs/twt/DECISIONS-TW.md; grep -c '^### VB17' docs/vbt/DECISIONS-VB.md; grep -c 'Qullamaggie\|qulla' kite-momentum-rebalancer/app/exit_rules.py
  EXPECT: /^1\n1\n[1-9]/m
  EVIDENCE: 1 | 8

- [x] Q8: both suites green and lint clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED
  EXPECT: LINT CLEAN
  EVIDENCE: LINT CLEAN
