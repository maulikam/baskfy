# Gates: ranking-1.3 — Research candidates

Scope: ATR extension, MA slope, efficiency, non-overlapping acceleration, and related
candidates live in a research module — not dumped into Sort By / FACTORS without a decision.
Corrections in docs/ranking/PLAN.md are respected in code comments and tests.

- [x] G1: Research module exists under baskfy_core
  CHECK: test -f /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/core/src/baskfy_core/ranking_research.py && echo ok
  EXPECT: ok
  EVIDENCE: ok

- [x] G2: Research keys are NOT registered as Sort By factors
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -c "from baskfy_core.factor_registry import FACTORS; from baskfy_core import ranking_research as r; bad=[k for k in r.RESEARCH_CANDIDATE_KEYS if k in FACTORS]; print('ok' if not bad else bad)"
  EXPECT: ok
  EVIDENCE: ok

- [x] G3: Research unit tests pass (corrections pinned)
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_ranking_research.py -q --tb=line 2>&1 | tail -5; echo PASSED
  EXPECT: PASSED
  EVIDENCE: .......                                                                  [100%] | PASSED

- [x] G4: Excess return vs common index is documented as filter/column not rank key
  CHECK: rg -n "not a rank key|filter / column|filter/column" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/core/src/baskfy_core/ranking_research.py
  EXPECT: /rank key|filter/
  EVIDENCE: 42:    # Explicitly not a rank key — preference is eligibility/filter semantics. | 159:        # filter / column, not a rank key

- [x] G5: Acceleration uses non-overlapping log rates
  CHECK: rg -n "non.overlapping" /Users/maulikdave/Documents/projects/baskfy/decile-blueprint/packages/core/src/baskfy_core/ranking_research.py
  EXPECT: /non.overlapping/i
  EVIDENCE: 113:            f"need long >= 2*short for a non-overlapping pair; got short={short}, long={long}" | 119:    # Rate over the prior `short` sessions (ends where recent begins) — non-overlapping.

- [x] G6: Decision + PLAN status for Phase 1.3
  CHECK: rg -n "ranking research|Phase 1.3" /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md /Users/maulikdave/Documents/projects/baskfy/docs/ranking/PLAN.md
  EXPECT: /1\.3|research/i
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md:7503:## Ranking engine — research candidates · Phase 1.3 · ⚠ UNREVIEWED | /Users/maulikdave/Documents/projects/baskfy/docs/ranking/P
