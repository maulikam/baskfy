# Gates: leaf-4.5-chart

Scope: Basket page loads series from API/lib not only stub

- [x] G1: fetch series helper
  CHECK: rg -n 'fetchPerformance|performanceSeries|ret_1m|chain' decile-blueprint/apps/web/src/lib/explore -g '*.ts*' | head -5
  EXPECT: /
  EVIDENCE: lib/explore/performance.ts performanceSeriesFromMetrics (2026-08-23T04:06Z)

<!-- integrity: security, performance, memory, accuracy required -->
