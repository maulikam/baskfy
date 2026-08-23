# Gates: SC0–SC12 product run

Scope: Deliver every commissioned SC module in order, safely, with one green commit per module.

- [x] G0: SC0 baseline commit exists
  CHECK: git log --format='%s' --grep='^SC0: green — ' -1
  EXPECT: /^SC0: green — /
  EVIDENCE: SC0: green — baseline the product-layer run safely

- [x] G1: SC1 schema/domain module is green
  CHECK: git log --format='%s' --grep='^SC1: green — ' -1
  EXPECT: /^SC1: green — /
  EVIDENCE: SC1: green — establish exact curated-basket foundations

- [ ] G2: SC2 catalog computation/API module is green
  CHECK: git log --format='%s' --grep='^SC2: green — ' -1
  EXPECT: /^SC2: green — /
  EVIDENCE: pending

- [ ] G3: SC3 versions/rebalance/plans module is green
  CHECK: git log --format='%s' --grep='^SC3: green — ' -1
  EXPECT: /^SC3: green — /
  EVIDENCE: pending

- [ ] G4: SC4 investment-accounting module is green
  CHECK: git log --format='%s' --grep='^SC4: green — ' -1
  EXPECT: /^SC4: green — /
  EVIDENCE: pending

- [ ] G5: SC5 discovery/detail web module is green
  CHECK: git log --format='%s' --grep='^SC5: green — ' -1
  EXPECT: /^SC5: green — /
  EVIDENCE: pending

- [ ] G6: SC6 investor-surfaces web module is green
  CHECK: git log --format='%s' --grep='^SC6: green — ' -1
  EXPECT: /^SC6: green — /
  EVIDENCE: pending

- [ ] G7: SC7 SIP reminders module is green
  CHECK: git log --format='%s' --grep='^SC7: green — ' -1
  EXPECT: /^SC7: green — /
  EVIDENCE: pending

- [ ] G8: SC8 create/customize module is green
  CHECK: git log --format='%s' --grep='^SC8: green — ' -1
  EXPECT: /^SC8: green — /
  EVIDENCE: pending

- [ ] G9: SC9 engagement module is green
  CHECK: git log --format='%s' --grep='^SC9: green — ' -1
  EXPECT: /^SC9: green — /
  EVIDENCE: pending

- [ ] G10: SC10 gating/dark-machinery module is green
  CHECK: git log --format='%s' --grep='^SC10: green — ' -1
  EXPECT: /^SC10: green — /
  EVIDENCE: pending

- [ ] G11: SC11 hardening/safety module is green
  CHECK: git log --format='%s' --grep='^SC11: green — ' -1
  EXPECT: /^SC11: green — /
  EVIDENCE: pending

- [ ] G12: SC12 verification module and final report are green
  CHECK: test -f SC-FINAL-REPORT.md && git log --format='%s' --grep='^SC12: green — ' -1
  EXPECT: /^SC12: green — /
  EVIDENCE: pending
