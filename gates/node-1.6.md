# Gates: node-1.6

Scope: SC7–9 engagement

- [x] G1: sip or create surface present
  CHECK: ls decile-blueprint/apps/web/src/app/\(app\)/create/page.tsx 2>/dev/null || ls decile-blueprint/packages/core/src/baskfy_core/*sip* 2>/dev/null | head -1
  EXPECT: /.|sip|create/
  EVIDENCE: create/page.tsx + curated_sip.py 2026-08-23T00:25Z (SC7+SC8; SC9 engage still open)

<!-- integrity: security, performance, memory, accuracy required -->
