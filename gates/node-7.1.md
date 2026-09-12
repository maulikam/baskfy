# Gates: node 7.1 — the encodings tell the truth (integration)

- [ ] G1: Leaves 7.1.1–7.1.3 all green with evidence.
      ⚠️ **Repaired 12 Sep 2026: the EXPECT could not match, on case alone.** It read
      `/0 unmet|all met/` and `gate-check.mjs` prints **`ALL MET (22 met)`** in capitals, so
      this row went red against leaves that were entirely green. The `i` flag is the fix.
      Same class as the eleven rows `gates/twt-root.md` records, found the same way — by
      running the check instead of reading it.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-7.1.1-scorebar.md gates/leaf-7.1.2-column-policy.md gates/leaf-7.1.3-dead-columns.md 2>&1 | tail -10
  EXPECT: /0 unmet|all met/i
  EVIDENCE: pending

- [ ] G2: `cell-encodings.tsx` was edited by two leaves in sequence and holds both changes.
  EVIDENCE: pending

- [ ] G3: On a live render, the top row's bar is visibly longer than row 2's in proportion to
      their scores — measured, not eyeballed.
  EVIDENCE: pending
