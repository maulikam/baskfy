# Gates: leaf-1.5.1-ui-investor

Scope: Investments, watchlist, fees pages

- [x] G1: fees page exists
  CHECK: test -f decile-blueprint/apps/web/src/app/\(app\)/fees/page.tsx && echo yes
  EXPECT: yes
  EVIDENCE: yes (2026-08-23) — also `/investments`, `/investments/[id]`, `/investments/[id]/orders`, `/watchlist`; ShowDetailsModal + InvestmentActions; nav + vocabulary; vitest read-only suite

<!-- integrity: security, performance, memory, accuracy required -->
