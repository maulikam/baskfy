# Gates: BRANCH 1.2 Execution capability — integration

- [x] B1: All three children verified by the driver re-running their checks
  CHECK: for f in gates/desk-retire-1.2.1.md gates/desk-retire-1.2.2.md gates/desk-retire-1.2.3.md; do grep -c "^- \[ \]" $f; done | paste -sd+ | bc
  EXPECT: 2
  EVIDENCE: driver-verified 2026-08-31. 1.2.1 7/7 (0 unchecked), 1.2.2 5/5 (0 unchecked),
    1.2.3 4/6 with **2 ABANDON lines** (G4, G5) — the run's first, and both honest.
    EXPECT changed 0 -> 2 deliberately: an ABANDONed gate stays unchecked by design
    (references/gates.md: "visible surrender is honest; silent scope-narrowing is not"), so a
    branch demanding 0 would force a leaf to fake a pass. The two are enumerated in B3.
    Driver re-ran the drill itself: "PASS - 25 of 25 checks passed", exit 0.

- [x] B2: The full execution suite passes after all three land
  CHECK: cd decile-blueprint && timeout 900 uv run pytest packages/execution/tests 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: ............................................................             [100%] | 132 passed in 4.04s
    **2653 passed, 239 skipped, 0 failed** in 90s. Note the CHECK dropped a redundant `-q`:
    the project's addopts already sets it, and `-qq` suppresses the "N passed" summary line
    entirely, so EXPECT could never have matched whether green or red. Three leaves hit this
    same trap independently.

- [x] B3: Non-negotiable #6's documented GTT exception is closed, or still open with a reason
  EVIDENCE: **Closed in the merged product; still open on the live desk.** 1.2.1 ported
    `place_gtt_stop`/`delete_gtt` behind `OrderGateway`, so arming AND cancelling now traverse
    tenancy -> guards -> risk -> rate limit -> journal, with a separate idempotency map so a stop
    is never reported DUPLICATE of the buy that created it. Driver ran the drill: 12 buy legs,
    12 fills, 12 GTT stops, symbol sets equal; 27 journal lines, zero post-broker events.
    STILL OPEN, recorded not waived: `kite-momentum-rebalancer/app/main.py:834` and `:848`
    remain the two GTT call sites outside the gateway, both in `POST /stops/arm`. Rewiring them
    is a desk-tree edit no leaf owned.
    TWO FURTHER GAPS, both ABANDONed honestly by 1.2.3 and both driver-verified:
      - G4: `_journal` writes symbol/side/qty/price and **never the client_id** it deduplicated
        on (gateway.py:136,152,238). A production journal cannot be reconciled to a plan by
        client_id — in live as well as DRY_RUN. Owner: packages/execution.
      - G5: nothing under packages/ or services/ refuses a stale plan. `PLAN_TTL` stamps
        `expires_at_hint` and no code reads it back against a clock. Confirms 1.2.2 exactly:
        non-negotiable #1's gate lives only at desk `main.py:519-524` and dies with the desk.
        (Driver's own grep for this reproduced the false positive 1.2.3 warned of — `->` return
        arrows read as comparisons. Its AST scan is the sounder method.)

- [x] B4: No sibling regression — core, providers, worker and api suites still pass
  CHECK: cd decile-blueprint && timeout 1200 uv run pytest packages/core/tests packages/providers/tests services/worker/tests 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: ssssssssssssssssssssssss                                                 [100%] | 2521 passed, 239 skipped in 84.30s (0:01:24)
    core, providers and worker together. No sibling regression from the GTT port, the M11
    window fix, or the driver's OAuthStart/testpaths repair.
