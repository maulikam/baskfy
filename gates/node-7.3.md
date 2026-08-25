# Gates: node 7.3 — the list is the hero (integration)

- [ ] G1: Leaves 7.3.1–7.3.2 green with evidence.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-7.3.1-default-view.md gates/leaf-7.3.2-motion.md 2>&1 | tail -10
  EXPECT: /0 unmet|all met/
  EVIDENCE: pending

- [ ] G2: FLIP and the table-first default coexist: landing on the table and then sorting animates
      correctly on first interaction.
  EVIDENCE: pending
