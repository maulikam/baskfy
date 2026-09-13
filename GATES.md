# Gates: ranking Phase 1.2 — desk SCORE as Sort By + explainability payload

Scope: `sort_by=desk_score` runs the book's `baskfy_core.score.score` on filtered survivors;
each row can carry A–F breakdown fields for explainability. No second formula.

- [x] G1: `desk_score` is a registered factor key
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -c "from baskfy_core.factor_registry import FACTORS; from baskfy_core.ranking import DESK_SCORE_KEY, is_desk_score_factor; print('ok' if DESK_SCORE_KEY in FACTORS and is_desk_score_factor(DESK_SCORE_KEY) else 'bad')"
  EXPECT: ok
  EVIDENCE: ok

- [x] G2: Pure re-rank helper orders by SCORE and attaches A–F without I/O
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_desk_score_screen.py -q --tb=line 2>&1 | tail -5
  EXPECT: /100%/
  EVIDENCE: ........                                                                 [100%]

- [x] G3: ScreenDefinition accepts sort_by=desk_score; rejects desk_score with factor_two
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_desk_score_screen.py::test_screen_definition_accepts_desk_score packages/core/tests/test_desk_score_screen.py::test_screen_definition_rejects_desk_score_with_factor_two -q --tb=line 2>&1 | tail -3
  EXPECT: /100%/
  EVIDENCE: ..                                                                       [100%]

- [x] G4: Decision notes Phase 1.2 desk_score wiring
  CHECK: rg -n "desk_score Sort By" /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md
  EXPECT: desk_score Sort By
  EVIDENCE: 7460:## Ranking engine — desk_score Sort By · Phase 1.2 · ⚠ UNREVIEWED

- [x] G5: PLAN status log mentions Phase 1.2
  CHECK: rg -n "Phase 1.2" /Users/maulikdave/Documents/projects/baskfy/docs/ranking/PLAN.md
  EXPECT: Phase 1.2
  EVIDENCE: 60:- 2026-09-13 Phase 1.2: `sort_by=desk_score` filters in SQL then re-ranks with `baskfy_core.score.score`;
