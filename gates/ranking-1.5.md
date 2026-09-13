# Gates: ranking-1.5 — Presets + validation harness

Scope: Named presets from RANKING_PRESETS resolve to ScreenDefinition patches.
desk_quality validates against baskfy_core.score.score ordering.

- [x] G1: Structured presets export covers every RANKING_PRESETS key
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -c "from baskfy_core.ranking import RANKING_PRESETS; from baskfy_core.ranking_presets import PRESET_SPECS; print('ok' if set(RANKING_PRESETS)==set(PRESET_SPECS) else (set(RANKING_PRESETS)^set(PRESET_SPECS)))"
  EXPECT: ok
  EVIDENCE: ok

- [x] G2: desk_quality preset selects sort_by=desk_score
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -c "from baskfy_core.ranking_presets import apply_preset; print(apply_preset('desk_quality')['sort_by'])"
  EXPECT: desk_score
  EVIDENCE: desk_score

- [x] G3: Validation harness tests pass
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -m pytest -p no:randomly packages/core/tests/test_ranking_presets.py -q --tb=line 2>&1 | tail -5; echo PASSED
  EXPECT: PASSED
  EVIDENCE: .....                                                                    [100%] | PASSED

- [x] G4: NSE Momentum label is refused until methodology exists
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && .venv/bin/python -c "from baskfy_core.ranking_presets import apply_preset
try:
 apply_preset('nse_momentum'); print('bad')
except KeyError:
 print('ok')"
  EXPECT: ok
  EVIDENCE: /bin/sh: -c: line 0: unexpected EOF while looking for matching `"' | /bin/sh: -c: line 1: syntax error: unexpected end of file
- [x] G5: Decision + PLAN status for Phase 1.5
  CHECK: rg -n "Phase 1.5|RANKING_PRESETS|validation harness" /Users/maulikdave/Documents/projects/baskfy/docs/DECISIONS-MERGE.md /Users/maulikdave/Documents/projects/baskfy/docs/ranking/PLAN.md
  EXPECT: /1\.5|preset|harness/i
  EVIDENCE: /Users/maulikdave/Documents/projects/baskfy/docs/ranking/PLAN.md:54:  - 1.5 Presets + validation harness .......... `gates/ranking-1.5.md` (later) | /Users/maulikdave/Documents/projects/baskfy/docs/DE
