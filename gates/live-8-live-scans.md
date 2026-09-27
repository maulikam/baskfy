# Gates: LV8 — live scans for TWT and VBT; entries now, at market (Maulik, 28 Sep 2026)

Scope: DECISIONS-LV LV8.0. During the session, TWT's and VBT's Scan build today's provisional bar from Kite quotes, run the same detector over today-so-far, and build a `LIVE` plan whose entries are market buys at the live price with Kite market protection and the same-session GTT; TWT's auto-execute confirms a LIVE plan any time in the session; VBT's is confirmed by hand. Nothing the nightly writes changes.

- [x] V1: migration 0057 admits `LIVE` plans, `BUY_AT_MARKET` VBT lines and `provisional` on the two scan-run tables and the five detection tables; models agree; downgrade clean
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/api/tests/test_migration_0057.py 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 3 passed in 9.29s

- [x] V2: a provisional bar is built per name from a quote in the TWT/VBT bar schema (adjusted prices, raw close, volume so far), a name without a quote or a factor is skipped and counted, and a scan before 09:15 or after the publish is the last published session
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_live_scan.py -k 'bars or decision or decide' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 11 passed, 12 deselected in 4.58s

- [x] V3: the TWT and VBT detectors accept today's provisional frame, write their rows marked provisional, and a provisional TWT run never ratchets the book; the nightly's real rows replace provisional ones and delete the stragglers
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_live_scan.py -k 'detect or provisional or ratchet or sweep' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 13 passed, 10 deselected in 5.50s

- [x] V4: a live scan with signals builds a LIVE plan (30-minute expiry) — TWT entries as BUY_AT_OPEN sized on the live close, VBT entries as BUY_AT_MARKET — and one without signals builds none
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest services/worker/tests/test_live_scan.py -k 'plan' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 6 passed, 17 deselected in 4.47s

- [x] V5: VBT's BUY_AT_MARKET confirm is a MARKET buy with Kite market protection, the sleeve's guards (cap, held, working, protection), a fill through the same `_apply_fill`, and the GTT in the same request; dry-run rehearses the whole path
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_vbt_execute.py -k 'market' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 6 passed, 22 deselected in 1.08s

- [x] V6: twt-auto accepts a LIVE plan built today and not expired, refuses an EVENING plan or an expired LIVE one, and the session supervisor drains a LIVE plan on its tick only while all three flags are on
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q tests/test_twt_auto.py tests/test_session_supervisor.py -k 'live or drain' 2>&1 | tail -1
  EXPECT: /[1-9]\d* passed/
  EVIDENCE: 12 passed, 47 deselected in 0.64s

- [x] V7: the scan-run wire shape carries `provisional`; the pages say "provisional — scanned HH:MM from live quotes"; `/sleeves/state`'s scan_means says TWT and VBT scan today so far during the session
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest services/api/tests/test_sleeve_state.py services/api/tests/test_api_twt_scan.py services/api/tests/test_api_vbt_scan.py 2>&1 | tail -1 && cd apps/web && npx vitest run 'src/app/(app)/twt' 'src/app/(app)/vbt' 2>&1 | grep -E 'Tests ' | tail -1
  EXPECT: /Tests\s+\d+ passed/
  EVIDENCE: 18 passed, 30 skipped in 0.14s | Tests  116 passed (116)

- [x] V8: no new auto-execute flag; VBT has none; the TWT widening is recorded as Maulik's (DECISIONS-TW TW19, DECISIONS-VB VB16)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && printf 'vbt_flag_in_code=%s tw19=%s vb16=%s\n' "$(awk '/BASKFY_VBT_AUTO_EXECUTE/ && !/^[[:space:]]*#/ {n++} END {print n+0}' decile-blueprint/infra/docker/compose.prod.yml kite-momentum-rebalancer/app/config.py)" "$(grep -c '^## TW19' docs/twt/DECISIONS-TW.md)" "$(grep -c '^### VB16' docs/vbt/DECISIONS-VB.md)"
  EXPECT: vbt_flag_in_code=0 tw19=1 vb16=1
  EVIDENCE: vbt_flag_in_code=0 tw19=1 vb16=1

- [x] V9: openapi + client regenerated; lint clean on both trees; desk suite green
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && make lint >/dev/null 2>&1 && echo LINT CLEAN || echo LINT FAILED
  EXPECT: LINT CLEAN
  EVIDENCE: LINT CLEAN
