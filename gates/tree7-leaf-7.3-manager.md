# Gates: Tree 7 leaf 7.3 — manager profile page

Scope: `/manager/[slug]` renders cb_manager bio, strategies, disclosures (SEBI furniture).

- [x] G1: Route exists under apps/web
  CHECK: test -f decile-blueprint/apps/web/src/app/\(app\)/manager/\[slug\]/page.tsx && echo ok
  EXPECT: ok
  EVIDENCE: page.tsx created Tree 7

- [x] G2: Page reads manager fields (bio or disclosures)
  CHECK: rg -n "disclosures|bio|sebi|CbManager|manager" decile-blueprint/apps/web/src/app/\(app\)/manager -g '*.{ts,tsx}' | head -10
  EXPECT: /
  EVIDENCE: sebi_reg_no, bio, strategies, disclosures_md rendered

- [x] G3: No order path added
  CHECK: rg -n "place_order|OrderGateway|/execute" decile-blueprint/apps/web/src/app/\(app\)/manager 2>/dev/null; echo exit:$?
  EXPECT: exit:1
  EVIDENCE: exit:1 (no matches)
