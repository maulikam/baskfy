# Gates: leaf-2.4-chart

Scope: Basket detail performance chart with SIP toggle + benchmark

- [x] G1: chart component exists
  CHECK: test -f decile-blueprint/apps/web/src/components/explore/performance-chart.tsx && echo yes
  EXPECT: yes
  EVIDENCE: yes 2026-08-23; visx LinePath; range pills 1M/1Y/3Y/5Y/MAX; SIP + benchmark toggles; series props + stubPerformanceSeries

- [x] G2: used on basket page
  CHECK: rg -n 'PerformanceChart|performance-chart' decile-blueprint/apps/web/src/app/\(app\)/basket -g '*.tsx' | head -1
  EXPECT: /
  EVIDENCE: page.tsx imports PerformanceChart; DisclosureBlock performance-not-verified (+ history-caveat) adjacent; no order buttons on chart

<!-- integrity: security, performance, memory, accuracy required -->
