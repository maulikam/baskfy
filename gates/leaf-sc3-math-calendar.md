# Gates: SC3 exact diff and NSE market calendar

Scope: Compute all five plan kinds with exact money math and guard them with the stored NSE calendar.

- [ ] G1: Invest and invest-more allocate whole shares deterministically and disclose residual cash/minimum shortfall
  CHECK: cd decile-blueprint && DRY_RUN=true uv run pytest packages/core/tests/test_curated_rebalance.py -q -rA -k 'invest or minimum'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G2: Apply diffs current intended holdings against the new target, with sells funding buys and exact top-up
  CHECK: cd decile-blueprint && DRY_RUN=true uv run pytest packages/core/tests/test_curated_rebalance.py -q -rA -k 'apply or top_up'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G3: Full and partial exits never sell beyond owned intended quantity and retain exact residuals
  CHECK: cd decile-blueprint && DRY_RUN=true uv run pytest packages/core/tests/test_curated_rebalance.py -q -rA -k 'exit'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G4: Invalid/negative/stale/missing prices, duplicate instruments, malformed weights, and impossible partial exits fail closed
  CHECK: cd decile-blueprint && DRY_RUN=true uv run pytest packages/core/tests/test_curated_rebalance.py -q -rA -k 'reject or invalid or duplicate'
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G5: Calendar rules are timezone-aware, boundary-exact, holiday/weekend-aware, and return the next stored NSE opening
  CHECK: cd decile-blueprint && DRY_RUN=true uv run pytest packages/core/tests/test_curated_market_calendar.py -q -rA
  EXPECT: /PASSED /
  EVIDENCE: pending

- [ ] G6: Expert reread, oracle/property pass, bounded scale/memory proof, defect hunt, and free polish find no remaining defect
  EVIDENCE: pending
