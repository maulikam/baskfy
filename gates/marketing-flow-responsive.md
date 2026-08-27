# Gates: the "From intent to result" flow, rebuilt in normal document flow

Scope: `decile-blueprint/apps/web/src/components/marketing/how-it-works-flow.tsx` and the section
that frames it in `src/app/(marketing)/page.tsx`. The diagram was a hard-coded 1360 × 420 canvas
with every card `absolute`-positioned over one full-bleed SVG wire layer; it side-scrolled on a
phone at 11px body copy and 8px eyebrows, and the file's own comments record copy edits silently
sliding a card off its connector. This rebuilds it as a responsive grid in normal flow with
per-gap connector SVGs, drops the cost box, retitles the section to match, and replaces the eight
arbitrary travelling dashes with one choreographed pulse.

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/marketing-flow-responsive.md
```

## Supersedes G1 of `gates/marketing-flow-refresh.md`

G1 there reads *"The stage shows what a step costs, before the plan reaches a rail."* Maulik's
instruction of 27 Aug 2026 (`FLOW-REFINE-PROMPT.md` §3) reverses it: **the fee does not get a box
on the stage.** That is his call, not an agent judgement, so it is recorded in
`docs/DECISIONS-MERGE.md` without an `⚠ UNREVIEWED` tag. G2–G8 of the old file survive in intent
and are re-expressed here against the new spec; the old file is marked superseded rather than
deleted, because it is the record of what was measured on 25–26 Aug 2026.

The fee itself stays truthfully documented where it already lives: `fee-faq.tsx`, the numbered
"Confirm" step, and the section blurb. Only the *stage* loses it.

## Facts that must survive the rebuild (measured, not recalled)

| Claim | Source of truth |
|---|---|
| "Sixty-four factors" | `factor_registry.NAMED_FACTOR_COUNT == 64` |
| Exactly one live broker | `broker_connections.BROKERS`: only `zerodha` is `ready`/`ready` |
| Portfolio chips | `SECTION_TABS.portfolio` in `lib/nav.ts`, imported never retyped |
| Plans expire | non-negotiable 1 — "expires in 30 minutes" |
| Return path dashed | holdings are a fact you read, not a call we make |

---

- [x] G1: The stage is normal document flow. No absolute positioning anywhere in the component,
      no fixed stage width or height, no horizontal-scroll wrapper, no full-bleed overlay.
  CHECK: cd decile-blueprint/apps/web && perl -0777 -pe 's{/\*.*?\*/}{}gs; s{^\s*//.*$}{}gm' src/components/marketing/how-it-works-flow.tsx | grep -E 'absolute|fixed|overflow-x-auto|w-\[1360px\]|h-\[420px\]|inset-0|ResizeObserver|getBoundingClientRect|use client' | wc -l | tr -d ' '
  EXPECT: 0
  EVIDENCE: 0

- [x] G2: Nothing on the stage renders below ~12px at any breakpoint — the 11px bodies and 8px
      eyebrows that made the old stage unreadable on a phone are gone.
  CHECK: cd decile-blueprint/apps/web && perl -0777 -pe 's{/\*.*?\*/}{}gs' src/components/marketing/how-it-works-flow.tsx | grep -oE 'text-\[[0-9.]+px\]' | sort -u | awk -F'[][]' '{gsub("px","",$2); if ($2+0 < 12) print "TOO SMALL: " $0}' | wc -l | tr -d ' '
  EXPECT: 0
  EVIDENCE: 0

- [x] G3: The engine is exactly the four stages that ship — screen → basket → organize → plan —
      with no cost stage and no fee copy anywhere on the stage.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "engine stages" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  5 passed | 35 skipped (40)

- [x] G4: No broker is presented as connected except the one that is. Zerodha is named; the other
      nine are a count with the caveat in the same breath, never named as live rails.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "broker honesty" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  3 passed | 37 skipped (40)

- [x] G5: The journey drawn is the journey shipped — 64 factors, `SECTION_TABS.portfolio` chips
      imported from `lib/nav`, Unallocated, plural portfolios, your own demat, plans that expire.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "journey shipped" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  6 passed | 34 skipped (40)

- [x] G6: One layout that reflows. DOM order is the narrative order (You → Market data → engine →
      rails → portfolios), connectors are per-gap SVGs that turn vertical below `lg`, and the
      wires stay `aria-hidden` while every reader-facing word is real DOM text.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "responsive flow" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  12 passed | 28 skipped (40)

- [x] G7: The animation is one story, not eight specks. Every animated element shares one cycle
      duration so the phases cannot drift, and the delays increase strictly along the narrative:
      you → engine stages → rails → portfolios → return sweep.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "choreographed cycle" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  7 passed | 33 skipped (40)

- [x] G8: `prefers-reduced-motion` removes every travelling pulse and every stage acknowledgment,
      leaving the static hairlines and arrowheads — not a dash parked at the end of a wire.
  CHECK: cd decile-blueprint/apps/web && sed -n '/Unlayered on purpose/,$p' src/app/globals.css | grep -E '^\s*\.flow-(pulse|sweep|ack)' | wc -l | tr -d ' '
  EXPECT: 3
  EVIDENCE: 3

- [x] G9: The heading and blurb promise only what the stage draws — the cost promise leaves with
      the cost box, and the page still says nothing on the site executes anything.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "section blurb" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  4 passed | 36 skipped (40)

- [x] G10: §8's retired jargon has not crept into the new copy and no §9 forbidden word reaches
      the marketing page.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/no-jargon.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  9 passed (9)

- [x] G11: Typecheck clean, the whole web suite green, eslint adds nothing to the tree's
      pre-existing count. That ceiling was re-measured on 27 Aug 2026 from 22 to **24** — the two
      new problems are not this rebuild's: stashing only its four files and re-running still
      reports 24, and every one of them is in a file it never touched. `pnpm exec eslint` over the
      rebuild's own four files reports nothing at all.
  CHECK: bash tools/marketing/flow-refresh-check.sh
  EXPECT: /FLOW OK/
  EVIDENCE: ✖ 24 problems (23 errors, 1 warning) | FLOW OK (eslint 24 <= 24)

- [x] G12: The production build passes, **and every arbitrary Tailwind utility this layout is
      built on actually reached the stylesheet** — a class the scanner never saw fails no build,
      no typecheck and no jsdom test, it just renders the diagram as one unstyled column.
  CHECK: bash tools/marketing/flow-responsive-check.sh
  EXPECT: /FLOW-RESPONSIVE OK/
  EVIDENCE: FLOW-RESPONSIVE OK (build green, 14 utilities emitted into 9eb16ee57aed6c8d.css)

- [x] G13: The decision is recorded and the status page updated — the G1 reversal cites
      `FLOW-REFINE-PROMPT.md` as Maulik's instruction, and `gates/marketing-flow-refresh.md` says
      it is superseded rather than quietly disagreeing with this file.
  CHECK: cd /Users/maulikdave/Documents/projects/baskfy && grep -l "FLOW-REFINE-PROMPT" docs/DECISIONS-MERGE.md docs/00-merge-status.md gates/marketing-flow-refresh.md | wc -l | tr -d ' '
  EXPECT: 3
  EVIDENCE: 3

- [x] G14: One commit, and the desk tree is untouched — nothing under `kite-momentum-rebalancer/`
      or `frozen/` is in it, and no new npm dependency was added.
  CHECK: echo "commits=$(git log --oneline HEAD~1..HEAD | wc -l | tr -d ' ') desk_or_frozen=$(git show --name-only --format= HEAD | grep -cE '^(kite-momentum-rebalancer|frozen)/') deps=$(git show --name-only --format= HEAD | grep -cE 'package\.json|pnpm-lock')"
  EXPECT: commits=1 desk_or_frozen=0 deps=0
  EVIDENCE: commits=1 desk_or_frozen=0 deps=0 (11 files at HEAD; the SHA is deliberately not quoted — this box is closed by amending the commit it describes, so a quoted SHA would be stale the moment it was written)

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
