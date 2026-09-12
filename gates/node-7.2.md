# Gates: node 7.2 — words and affordances (integration)

- [ ] G1: Leaves 7.2.1–7.2.2 green with evidence.
      ⚠️ **Repaired 12 Sep 2026: the EXPECT could not match, on case alone.** It read
      `/0 unmet|all met/` and `gate-check.mjs` prints **`ALL MET (22 met)`** in capitals, so
      this row went red against leaves that were entirely green. The `i` flag is the fix.
      Same class as the eleven rows `gates/twt-root.md` records, found the same way — by
      running the check instead of reading it.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs --status gates/leaf-7.2.1-chip-labels.md gates/leaf-7.2.2-tooltips.md 2>&1 | tail -10
  EXPECT: /0 unmet|all met/i
  EVIDENCE: pending

- [ ] G2: On a live render, no element anywhere on the page contains a raw slug or an
      ALL-CAPS factor string.
  EVIDENCE: pending
