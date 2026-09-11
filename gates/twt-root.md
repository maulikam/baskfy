# TWT — the TW run: TWT-1, the three-weeks-tight position sleeve

**Brief, 11 Sep 2026 (Maulik).** Build TWT-1 — the strategy researched in
`research/tight-close/STRATEGY.md` — into Baskfy as a third sleeve beside the weekly momentum
book and the swing book. Signals from the nightly chain, plans from the desk, every order through
`/analyze → /execute confirm → OrderGateway → GTT`, a **ratcheting trailing GTT**, and a runbook
for the first live morning. **No paper phase**: the sleeve goes live at ₹25 lakh the day Maulik
flips the flag himself. The run builds everything with the flag off, sets no capital, places no
order.

**The law of this run** is `CLAUDE.md` unchanged: the two laws, the seven non-negotiables, the
nine house rules, the safety rails, the autonomy charter. Track C (`docs/twt/02`) is forbidden.

Per-module gates: `gates/twt-0.md` … `gates/twt-10.md`. A module is done when its own file is
fully checked **with evidence**, its tests are green, and its commit exists.

---

- [ ] R0: TW0 — the docs pack. `docs/twt/` exists in the shape of `docs/swing/` / `docs/vbt/`.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-0.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R1: TW1 — the pure core, `baskfy_core.twt`, law 1 asserted.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-1.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R2: TW2 — the goldens: the research's 164 trades and its metrics reproduced or explained.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-2.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R3: TW3 — the `tw_` schema and its migration.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-3.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R4: TW4 — the nightly job, the published-session clock, tomorrow's trailing trigger.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-4.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R5: TW5 — the sleeve's own cash and book, the half-size counter.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-5.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R6: TW6 — the desk plan, `/twt/execute`, the GTT ratchet, the 15:15 sweep.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-6.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R7: TW7 — the fill-day rule and the naked-line assertion.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-7.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R8: TW8 — the desk page and the read-only web page.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-8.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R9: TW9 — the backtest on the page, from the plant's bars, drift flagged.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-9.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R10: TW10 — safety properties green, and `docs/twt/FIRST-LIVE-MORNING.md` written.
  CHECK: node /Users/maulikdave/.claude/skills/unlazy/scripts/gate-check.mjs gates/twt-10.md 2>&1 | tail -3
  EXPECT: /0 unchecked/
  EVIDENCE: pending

- [ ] R11: **No live order, no flag flip, no capital set.** The run never enables execution and
      never puts money in the sleeve.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -rn "BASKFY_TWT_EXECUTION_ENABLED" --include=*.py --include=*.example --include=*.ts decile-blueprint kite-momentum-rebalancer 2>/dev/null | grep -iE "=\s*true|default.?=.?True" | wc -l | tr -d ' '
  EXPECT: /^0$/
  EVIDENCE: pending

- [ ] R12: **Both trees still green.** The decile suite and the desk suite pass at the end of the
      run, and neither the weekly book, R1–R4, the swing book nor VBT-1 changed behaviour.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy/decile-blueprint && uv run pytest packages/core -q 2>&1 | tail -2
  EXPECT: /passed/
  EVIDENCE: pending

- [ ] R13: `TW-FINAL-REPORT.md` at the repo root, in the style of `SW-FINAL-REPORT.md`.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && test -f TW-FINAL-REPORT.md && wc -l < TW-FINAL-REPORT.md | tr -d ' '
  EXPECT: /^[1-9][0-9]{2,}$/
  EVIDENCE: pending
