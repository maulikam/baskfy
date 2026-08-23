# Gates: leaf-1.1.1-metrics

Scope: Pure min_amount, vol buckets, returns (04 §§2–4)

- [x] G1: min_amount property: shares ≥1 at min
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q --tb=line -k min_amount
  EXPECT: [100%]
  EVIDENCE: 13 tests [100%] 23 Aug; Decimal.sqrt for vol; CAGR via math.log/exp then Decimal

- [x] G2: volatility fixed thresholds PACK.1
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_curated_metrics.py -q --tb=line -k volatility
  EXPECT: [100%]
  EVIDENCE: 13 tests [100%] 23 Aug; Decimal.sqrt for vol; CAGR via math.log/exp then Decimal

- [x] G3: no float in curated_metrics.py
  CHECK: rg -n 'float\(' decile-blueprint/packages/core/src/baskfy_core/curated_metrics.py || echo none
  EXPECT: none
  EVIDENCE: 13 tests [100%] 23 Aug; Decimal.sqrt for vol; CAGR via math.log/exp then Decimal

<!-- integrity: security, performance, memory, accuracy required -->
