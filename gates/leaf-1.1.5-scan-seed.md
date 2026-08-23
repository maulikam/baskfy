# Gates: leaf-1.1.5-scan-seed

Scope: First SCAN basket seeded idempotently; STATUS SC2

- [ ] G1: seed creates SCAN basket path
  CHECK: rg -n 'SCAN|seed_scan|scan_basket' decile-blueprint/services/api/src/baskfy_api/curated_seed.py
  EXPECT: SCAN|scan
  EVIDENCE: pending

- [x] G2: STATUS SC2 marked
  CHECK: rg -n 'SC2' docs/smallcase/STATUS.md
  EXPECT: SC2
  EVIDENCE: 111:- No basket rows seeded beyond managers — baskets/versions/constituents are SC2/SC3 | 122:5. Legacy `/baskets` vs explore catalog collision is deferred to SC5 (do not invent a second MomentumScan 

<!-- integrity: security, performance, memory, accuracy required -->
