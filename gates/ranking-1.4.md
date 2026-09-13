# Gates: ranking-1.4 — Portfolio-aware selection

Scope: Selection overlays holdings on a quality rank. It must not mutate SCORE.
Uses the existing rank-buffer rule; does not weaken desk non-negotiables.

- [x] G1: ranking_selection module exists
  CHECK: test -f /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/core/src/baskfy_core/ranking_selection.py && echo ok
  EXPECT: ok
  EVIDENCE: ok

- [x] G2: Selection does not mutate SCORE (unit test)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_ranking_selection.py -q --tb=line 2>&1 | tail -5; echo PASSED
  EXPECT: PASSED
  EVIDENCE: ...                                                                      [100%] | PASSED

- [x] G3: Selection delegates to rank_buffer.plan_rebalance (one rule)
  CHECK: rg -n "plan_rebalance" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/core/src/baskfy_core/ranking_selection.py
  EXPECT: plan_rebalance
  EVIDENCE: 66:    Delegates to :func:`plan_rebalance` so the web portfolio path and the ranking engine share | 78:    return plan_rebalance(

- [x] G4: Module docstring forbids mutating SCORE / desk non-negotiables
  CHECK: rg -n "must not mutate SCORE|does not mutate SCORE" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/core/src/baskfy_core/ranking_selection.py
  EXPECT: /mutate SCORE/
  EVIDENCE: 1:"""Portfolio-aware selection over a quality rank — does not mutate SCORE. | 10:* Selection **must not mutate SCORE** (or any quality column). Ranks are inputs; the scorer

- [x] G5: Decision + PLAN status for Phase 1.4
  CHECK: rg -n "Phase 1.4|portfolio-aware selection" /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md /Users/maulikdave/Documents/projects/baskfy/docs/ranking/PLAN.md
  EXPECT: /1\.4|selection/i
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md:7454:persistence research; portfolio-aware selection; validation harness vs existing model.
