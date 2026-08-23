# Gates: leaf-1.4.2-legacy

Scope: Legacy /baskets merge-or-redirect decided + implemented

- [x] G1: DECISIONS-SC records legacy choice
  CHECK: rg -n 'legacy|/baskets' docs/smallcase/DECISIONS-SC.md
  EXPECT: basket
  EVIDENCE: 53:**Taken.** **Extend, don't duplicate.** `basket_snapshot` and `GET /api/v1/baskets` stay as the desk | 62:**Reversal.** Drop `cb_*` migration 0014 and models; `/baskets` continues unchanged.

<!-- integrity: security, performance, memory, accuracy required -->
