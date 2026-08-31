# Gates: BRANCH 1.5 Surface and governance — integration

- [x] B1: Both children verified by the driver re-running their checks
  CHECK: for f in gates/desk-retire-1.5.1.md gates/desk-retire-1.5.2.md; do grep -c "^- \[ \]" $f; done | paste -sd+ | bc
  EXPECT: 0
  EVIDENCE: driver-verified 2026-08-31. 1.5.1 4/4, 1.5.2 6/6 — 0 unchecked, 0 ABANDON.
    1.5.1: 38 URL surfaces inventoried (34 routes + /static + 3 conditional docs routes);
    both desk copies agree on the route table exactly, so the 58-file divergence is entirely
    below the route line. Classification 10 already ported / 9 needs porting / 14 dies with the
    desk / **5 ORDER-CAPABLE and blocked**. Port plan 14-18 person-days.
    1.5.2: driver re-verified its three load-bearing measurements — routers/kite.py has 0
    @router.post, gateway.py had 0 GTT at the time it measured, and the M11 parity test's two
    full-row cases skip on an unset BASKFY_PARITY_BARS (so "44 passed" said nothing about
    parity, and the document says so).

- [x] B2: No execute route exists anywhere in Baskfy
  CHECK: grep -rln "@router.post" decile-blueprint/services/api/src/baskfy_api/routers/kite.py 2>/dev/null | wc -l
  EXPECT: 0
  EVIDENCE: 0
