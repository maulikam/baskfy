# Gates: leaf B2-portfolio-nav

Scope: §5.1/§5.2 EOD NAV series, TWR, since-grouped, drawdown

Written by the parent before fan-out. The leaf agent proves its own work; the parent
re-runs these checks independently — that verification hierarchy is the whole point of
orchestrated mode.

---

- [x] G1: The module exists, is pure, and its tests pass.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_portfolio_nav.py -p no:randomly 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: ..........................................................               [100%] | 58 passed in 1.66s

- [x] G2: TWR is flow-neutral — a cash assignment does not move it. That property is why §5.2 uses TWR.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_portfolio_nav.py -p no:randomly -k "twr or flow_neutral" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .............                                                            [100%] | 13 passed, 45 deselected in 1.42s

- [x] G3: A holding group with no transaction history refuses XIRR and says why (§5.2).
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_portfolio_nav.py -p no:randomly -k "grouped or history or unavailable" 2>&1 | tail -2
  EXPECT: /\d+ passed/
  EVIDENCE: .........                                                                [100%] | 9 passed, 49 deselected in 1.23s

- [x] G4: House rules clean.
  CHECK: bash tools/portfolio/leaf-hygiene.sh portfolio_nav
  EXPECT: /HYGIENE OK/
  EVIDENCE: mypy clean | HYGIENE OK

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
