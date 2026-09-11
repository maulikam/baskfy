# TW8 — the pages: the web app's read-only view and the desk's actionable one

**Plan:** `docs/twt/06-module-plan.md` § TW8. **Spec:** `docs/twt/05-ui-spec.md` §1–3.
**Built ahead of its data.** TW4 and TW5 are not finished, so this module is written against the
DOCUMENTED contract (`03-data-model.md`) and fed by fixtures, exactly as the portfolio panels were.
The parent wires the real reads when the producing modules land.

- [ ] G1: Web `/twt` per `05` §1 — the gate, today's tight names with the point-in-time sentence,
      the open book with its highest-high, its trigger and **the distance to it**, and the
      half-size counter.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt src/app --silent --reporter=basic -t "twt" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G2: **Read-only.** Every non-GET route under `/twt` is a 405 except the two that change no
      money (a note and a dismissal). The web app has no order path and does not gain one.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/auth/__tests__ src/app --silent --reporter=basic -t "read-only|405" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G3: `/twt/backtest` per `05` §3, with `01` §8's caveats rendered as a component rather than
      a footnote (house rule 9's second half).
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "backtest|caveat" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G4: The point-in-time sentence and the caveats appear **verbatim** — this is the sentence
      that tells a reader why the live scan shows fewer names than Chartink on some days.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "point-in-time" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G5: **No bare dash**, and nothing internal on screen. Every unavailable figure carries its
      reason, in a reader's words — no route, no column name, no file path, no host. The portfolio
      work established both rules and `no-internals.test.tsx` is the pattern to copy.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "unavailable|internal" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G6: The desk page per `05` §2 — exits first, then entries, then the book, then Confirm, and
      the 15:15 strip. `DRY_RUN` is a **badge**, and an expired plan's buttons are **absent**
      rather than disabled: a disabled Confirm on an expired plan invites a reload and a retry.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "desk|expired" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G7: Three states render and explain themselves: an empty sleeve, a SHUT gate, and a naked
      line (an open position with no resting GTT).
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/twt --silent --reporter=basic -t "state|empty|shut|naked" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G8: Light and dark both pass the existing contrast check, and `--muted-foreground` on
      `--muted` is among the pairs asserted — it failed AA until 11 Sep 2026.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/contrast.test.ts --silent --reporter=basic 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: pending

- [ ] G9: The whole web suite is green and `pnpm run lint` reports zero errors.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files" && pnpm run lint 2>&1 | tail -3
  EXPECT: /0 errors/
  EVIDENCE: pending

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
