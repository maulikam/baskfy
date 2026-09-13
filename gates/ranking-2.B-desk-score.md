# Gates: 2.B — Desk SCORE exactly as the book computes it (contract C2)

Scope: DESK_CONFIG in core tied to the desk's config by test, a pure service that runs the book's scan+score, the desk_score_daily table + migration 0047, and a version constant that cannot drift silently.

- [x] G1: `baskfy_core.desk_config.DESK_CONFIG` exists and a test in the kite tree asserts every attribute that `momentum_scan.build`/`score.score`/`score.required` read equals `app/config.py` (and fails if one changes). `ranking.DeskScoringDefaults` is replaced by or derived from DESK_CONFIG.
  CHECK: cd kite-momentum-rebalancer && .venv/bin/python -m pytest -q tests/test_desk_config_parity.py 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m                                                             [100%][0m | 

- [x] G2: `baskfy_core.desk_score_service.score_day(bars, as_of, trading_days, carried) -> DataFrame` (pure) returns per-instrument score/rank/A–F/ext_over_20dma/reject/score_version, and a parity test proves it equals the book's own path (`app.scan_source`-equivalent `momentum_scan.build` + `app.scoring.score`) row for row on a fixture of ≥ 40 symbols including rejected names.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_desk_score_service.py 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m.[0m

- [x] G3: `DESK_SCORE_VERSION` is pinned: a test hashes score.py source + DESK_CONFIG and fails with a "bump DESK_SCORE_VERSION" message when either changes.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_desk_score_service.py -k version 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m.[0m[32m.[0m[32m.[0m[32m                                                                      [100%][0m | [32m[32m[1m3 passed[0m, [33m18 deselected[0m[32m in 0.25s[0m[0m

- [x] G4: `models/ranking.py` DeskScoreDaily + migration `0047_desk_score_daily` (down `0046_ranking_factors`) with the C2 columns; upgrade/downgrade clean on a scratch DB.
  EVIDENCE: 2026-09-13, scratch DB baskfy_scratch_b (created, then dropped): `BASKFY_DATABASE_URL=...5433/baskfy_scratch_b uv run alembic upgrade head` -> current `0047_desk_score_daily (head)`; `\d desk_score_daily` shows all 13 C2 columns (numeric(6,1)/(8,4)/(10,4), reject varchar(200) NOT NULL default '', score_version varchar(32) NOT NULL), pk_desk_score_daily(instrument_id,date), fk to instrument, ix_desk_score_daily_date_score_rank; alembic compare_metadata(Base.metadata) diffs for desk_score_daily: []; `alembic downgrade -1` -> `0046_ranking_factors`, to_regclass('desk_score_daily') NULL; `alembic upgrade head` -> `0047_desk_score_daily (head)` again.

- [x] G5: Rejected names carry NULL score and NULL score_rank; ranks are dense 1..n over eligible names only.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_desk_score_service.py -k reject 2>&1 | tail -2
  EXPECT: /passed(?!.*failed)/
  EVIDENCE: [32m.[0m[32m.[0m[32m.[0m[32m.[0m[32m                                                                     [100%][0m | [32m[32m[1m4 passed[0m, [33m17 deselected[0m[32m in 1.01s[0m[0m

- [x] G6: Law 1 (no I/O in the service), ruff, format, mypy clean; desk suite still green.
  CHECK: cd decile-blueprint && uv run ruff check packages/core/src/baskfy_core/desk_config.py packages/core/src/baskfy_core/desk_score_service.py packages/core/src/baskfy_core/models/ranking.py packages/core/tests/test_desk_score_service.py packages/core/tests/_desk_score_fixture.py && uv run ruff format --check packages/core/src/baskfy_core/desk_config.py packages/core/src/baskfy_core/desk_score_service.py packages/core/src/baskfy_core/models/ranking.py packages/core/tests/test_desk_score_service.py packages/core/tests/_desk_score_fixture.py && uv run mypy packages/core/src/baskfy_core/desk_config.py packages/core/src/baskfy_core/desk_score_service.py packages/core/src/baskfy_core/models/ranking.py packages/core/tests/test_desk_score_service.py packages/core/tests/_desk_score_fixture.py 2>&1 | tail -1 && uv run pytest -p no:cacheprovider --color=no packages/core/tests/test_desk_score_service.py -k law1 2>&1 | tail -1 && cd ../kite-momentum-rebalancer && DRY_RUN=true .venv/bin/python -m pytest -q -p no:cacheprovider --color=no 2>&1 | tail -1
  EXPECT: /Success: no issues found[\s\S]*2 passed[\s\S]*\d+ passed(?![\s\S]*failed)/
  EVIDENCE: 2 passed, 19 deselected in 0.19s | 2062 passed, 21 skipped, 203 warnings, 12 subtests passed in 74.85s (0:01:14)

- [x] G7: DECISIONS-MERGE.md "Ranking 2.B" entry (carried-column sources, universe nse_cash, version policy).
  CHECK: grep -c "Ranking 2.B" docs/DECISIONS-MERGE.md
  EXPECT: /[1-9]/
  EVIDENCE: 1
