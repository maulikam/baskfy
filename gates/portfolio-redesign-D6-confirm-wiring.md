# Gates: leaf D6 — wire the onboarding confirm button

Scope: the last loose end of the portfolio redesign. `POST /portfolio` exists (leaf C4) and
`NewPortfolioFlow` ends in a review step, but the page hosting it passes no `onCreate`, so
confirming is a no-op. This connects them.

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/portfolio-redesign-D6-confirm-wiring.md
```

## Facts measured before writing these gates

- `PortfolioDraft` (what the flow hands back) is `{ start, kind, name, benchmark, keys, sourceId }`
  where `benchmark` is a **display string** ("Nifty 500") and `start` is
  `SUBSCRIBED | MY_SCREEN | MY_STRATEGY | HOLDINGS | EMPTY`.
- `NewPortfolioIn` (what the API takes) is `{ name, kind, source, benchmark_index_id, holdings }`
  where `source` is `SUBSCRIBED | MY_SCREEN | MY_STRATEGY | HOLDING_GROUP` and
  `benchmark_index_id` is an integer.
- So two mappings are needed, and neither is the identity: `start → source` (`HOLDINGS` and
  `EMPTY` both become `HOLDING_GROUP`), and benchmark **name → index id**.
- `GET /meta/universes` already serves `index_id`, `slug` and `name`. The benchmark can therefore
  be resolved from data rather than from a hard-coded table of database ids in the web app.
- The web posts through **server actions** (`src/app/actions/*.ts`, `"use server"`), which is what
  keeps the bearer token on the server. A browser `fetch` to the API would leak it.

---

- [x] G1: A server action creates the portfolio, and the flow's confirm button calls it. The
      "not wired yet" note is gone because it is no longer true.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio/__tests__/create.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  18 passed (18)

- [x] G2: `start → source` is total and correct — every one of the five starts maps, and
      `HOLDINGS`/`EMPTY` both become `HOLDING_GROUP`. A start with no mapping must be impossible
      rather than silently defaulted.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "source" src/lib/portfolio/__tests__/create.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  6 passed | 12 skipped (18)

- [x] G3: The benchmark is resolved from `GET /meta/universes`, never from database ids typed
      into the web app. An unresolvable name sends `null` — §6.3 says `None` means "fall back to
      the product default", so that is honest — rather than guessing an id.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "benchmark" src/lib/portfolio/__tests__/create.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  7 passed | 11 skipped (18)

- [x] G4: A refusal from the API is shown to the user, not swallowed. Criterion 2's conflict
      ("this stock is already in another portfolio") is the case that matters: C4 answers it with
      a sentence naming the stock and the portfolio, and that sentence must reach the screen.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "refus" src/components/portfolio/__tests__/confirm-wiring.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  4 passed | 3 skipped (7)

- [x] G5: The app typechecks, the whole web suite is green (baseline 1,841 passing), and eslint
      adds nothing to the pre-existing 22.
  CHECK: bash tools/portfolio/confirm-wiring-check.sh
  EXPECT: /WIRING OK/
  EVIDENCE: Tests  1869 passed (1869) | WIRING OK

- [x] G6: The §8 jargon scanner still passes over the new source, and no §9 forbidden word
      ("managed", "advisory", "PMS") reaches the screen.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/no-jargon.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  9 passed (9)

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
