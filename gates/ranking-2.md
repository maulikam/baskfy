# Gates: 2 — ranking engine complete (root integration)

- [x] G1: All leaf gate files 2.A–2.H fully met (or ABANDON lines surfaced).
  CHECK: for f in gates/ranking-2.[A-H]-*.md; do node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status $f 2>&1 | tail -1; done
  EXPECT: /^(?![\s\S]*unmet)[\s\S]*ALL MET/i
  EVIDENCE: 2026-09-14: gate-check --status over 2.A–2.H → ALL MET (51 met)

- [x] G2: Whole Python suite (with DB) + make lint green at the integrated HEAD.
  CHECK: cd decile-blueprint && BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test uv run pytest --color=no -p no:cacheprovider 2>&1 | tail -1 && make lint 2>&1 | tail -3
  EXPECT: /passed(?![\s\S]*failed)[\s\S]*/
  EVIDENCE: 2026-09-14 CHECK run as written (plus BASKFY_REDIS_URL=redis://localhost:6380/3, DRY_RUN=true) on uncommitted round-2 tree: 8990 passed, 6 skipped, 1 xfailed, 0 failed in 23m07s, exit 0; make lint green (ruff check + format, mypy 725 files no issues, api-client tsc, web typegen/tsc/eslint 0 errors 1 pre-existing incompatible-library warning, shadowed-routes ok). Also vitest 216 files / 3388 tests passed; test_api_meta.py 21 passed against DB.

- [x] G3: Desk tree suite green (the desk must rebalance on any Friday).
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest --color=no 2>&1 | tail -1
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: 2026-09-14 kite-momentum-rebalancer .venv pytest -q --color=no -p no:cacheprovider (DRY_RUN=true): 2066 passed, 17 skipped, 12 subtests passed, 0 failed in 75s.

- [x] G4: Every brief item mapped to where it is implemented, with evidence, in docs/ranking/PLAN.md "Phase 2 — brief coverage" table (corrections 1–7 + NSE, 8 new factors, volume baseline, acceleration raw+scaled, 3 modes, 4 preferences, 3 scopes + default + provenance, quality vs selection, entry/retention from validation, explainability 7 fields, same desk service, families + family weights, ablation protocol, implementation order 1–5).
  EVIDENCE: 2026-09-14 docs/ranking/PLAN.md "Phase 2 — brief coverage" table written with 21 rows (15 met, 6 partial, 0 not done); every path and symbol grepped. The partials are correction 1 (no server-side refusal of non-rankable terms), correction 3 (path_quality preset still single pos_days_6m), NSE (preset refused, not testable), explainability (desk B–E have no stored inputs), ablation (2018-03 start, rs_persist_126 and nse_momentum_score not testable, no sector weights) and implementation order (wave 5 open, 2.C G5 test_swing_scan_now red on HEAD).

- [x] G5: Committed in verified waves; docs/DECISIONS-MERGE, NEEDS-MAULIK (anything needing hands), RUN-AND-TEST updated.
  EVIDENCE: 2026-09-14: committed in verified waves 9fd9f08, 4033355, ef78783, 403e6fa, f52df2c, d73bc54, f865654, a513866, b87521d. docs/DECISIONS-MERGE.md, NEEDS-MAULIK.md §35 and RUN-AND-TEST.md §3e updated.

- [ ] G6: Deployed to staging outside market hours with ship.sh green, migrations 0046/0047 at head on the box, backfill-ranking run for enough history that rank_persist_20 is populated for the latest session, and a live preview of a composite screen returns provenance + ranks.
  EVIDENCE: pending
