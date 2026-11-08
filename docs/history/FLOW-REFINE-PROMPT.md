# Prompt: rebuild the "From intent to result" flow diagram + animation

Paste this whole file into a fresh agent session at the repo root. Read order first:
`CLAUDE.md` → this file. Scope is the marketing web app only
(`decile-blueprint/apps/web`); the desk tree is untouched.

## What is being redone, and why (Maulik, 27 Aug 2026)

`apps/web/src/components/marketing/how-it-works-flow.tsx` — the animated diagram under the
landing heading *"From intent to result, with the cost visible before it runs."* Maulik reviewed
the rendered section and rejected it. His notes, verbatim in intent:

1. **It is not mobile responsive.** The stage is a fixed `1360 × 420` canvas that side-scrolls
   on a phone with tiny fixed-pixel type (11px bodies, 8px eyebrows). That has to go.
2. **No overlaid boxes.** Every card today is `absolute`-positioned over a full-bleed SVG wire
   layer, and cards visually pile onto and into each other (a card of five cards, captions
   floating under boxes at hand-tuned `top` offsets). The new layout uses **normal document
   flow** — nothing absolutely positioned, nothing layered over anything.
3. **No cost box.** Remove the `cost` stage from the engine. The fee does not get a box on the
   stage. This deliberately reverses G1 of `gates/marketing-flow-refresh.md` — it is a direct
   instruction from Maulik, not an agent judgement call. Record the reversal in
   `docs/DECISIONS-MERGE.md` citing this file; no `⚠ UNREVIEWED` tag needed.
4. **The animation itself is weak.** Travelling dashes read as disconnected specks with
   arbitrary delays. Rebuild it as one legible, choreographed story.

## Decisions already taken — do not re-litigate

- **Heading changes with the cost box.** The section title must stop promising a cost the stage
  no longer shows. Retitle to something in the register of *"From intent to result — nothing
  runs until you confirm."* Fine-tune the words; the constraint is that every promise in the
  heading is drawn on the stage. Update the section blurb in
  `apps/web/src/app/(marketing)/page.tsx` the same way: the fee sentences move out of the
  diagram's frame; the fee itself stays truthfully documented where it already lives
  (`fee-faq.tsx`, the numbered "Confirm" step, the attribution sentence can live in the blurb
  or under the steps — just not on the stage).
- **One responsive layout that reflows, not a hidden-on-mobile diagram.** Desktop reads left to
  right; below `lg` the same nodes stack top to bottom in narrative order and the connectors
  turn vertical. No horizontal scroll at any width. No fixed canvas. No duplicate
  mobile-only markup beyond what responsive utilities genuinely require.

## Layout spec

Narrative order, which is also the DOM order: **You → Market data → Baskfy engine (screen ·
basket · organize · plan) → rails (Zerodha Kite · Manual · CSV export) → Your portfolios**, with
the dashed "holdings sync back" return path closing the loop.

- Build it as a CSS grid/flex composition in normal flow. Connectors are **flow items that
  occupy the gaps between cells** — each one a small self-contained SVG (an arrow segment with
  its travelling pulse), rotating from horizontal to vertical with the breakpoint. This removes
  the whole class of bugs the old file documents (hand-measured card heights, coordinates that
  drift when copy changes, a full-canvas overlay that must agree with the DOM). Do not use a
  resize observer or runtime measurement; do not use a single full-bleed SVG.
- The engine's four stages sit side by side on desktop and wrap to a 2×2 (or single column) on
  small screens. Keep each stage to label + badge + one short line, as today.
- Type uses the existing vaaya tokens with responsive sizes (`clamp()` or Tailwind responsive
  steps). Nothing below ~12px effective at any breakpoint. All reader-facing text remains real
  DOM text; the SVG layer stays `aria-hidden`.
- Wire captions ("holdings", "feeds the ranking", "you confirm at your broker", "holdings sync
  back") become HTML captions attached to their connector cells, not floating SVG `<text>`.

## Animation spec

- One choreographed cycle: a pulse enters at **You**, passes through the engine stage by stage,
  splits to the rails, lands in **portfolios**, then the dashed return path sweeps back. Stagger
  with `animation-delay` so at any instant the eye has exactly one thing to follow; loop with a
  beat of rest.
- As the pulse passes a stage, the stage acknowledges it (e.g. border/eyebrow briefly lifts via
  the same delay schedule). Subtle — this is chrome, not a light show.
- CSS only: `stroke-dashoffset` / `opacity` / `transform`. Compositor-friendly properties only;
  no JS animation loop, no new dependencies (house rule 1 — nothing outside
  `decile-blueprint/docs/02-tech-stack-adr.md`'s locked stack without arguing it first).
- `prefers-reduced-motion` removes all travelling pulses and stage acknowledgments entirely
  (keep the existing `globals.css` pattern), leaving the static hairlines and arrowheads.
- Short per-cell connectors make the old `pathLength` dash-width arithmetic mostly moot; if any
  normalisation survives, keep the test that re-derives it.

## Facts that must stay true (measured 25–27 Aug 2026 — see gates/marketing-flow-refresh.md)

- "Sixty-four factors" (`factor_registry.NAMED_FACTOR_COUNT == 64`).
- Exactly one live broker: Zerodha. The other nine are a count with the caveat
  ("Nine more brokers planned. None live yet."), never named as live rails.
- Portfolio node chips come from `SECTION_TABS.portfolio` (imported, never retyped).
- Portfolios plural, "Unallocated until you file it", "your own demat — nothing pooled".
- The numbered `STEPS` list below the stage remains the canonical prose and the screen-reader
  journey — five steps, unchanged in substance (Confirm keeps its fee sentence).
- The return path stays dashed: holdings are a fact you read, not a call we make.

## Tests and gates

- Rewrite `__tests__/how-it-works-flow.test.tsx` to the **new spec** (house rule 2: tests
  assert the spec, and the spec just changed — this is not weakening a test, record it):
  engine order is exactly screen → basket → organize → plan with **no cost stage and no fee
  copy anywhere on the stage**; broker honesty tests survive as-is in intent; add: no
  `absolute` positioning inside the stage, no fixed `w-[1360px]`/`h-[420px]`, no
  `overflow-x-auto` wrapper, DOM order matches narrative order.
- Update `gates/marketing-flow-refresh.md` (or add `gates/marketing-flow-responsive.md`):
  G1 is superseded per Maulik 27 Aug 2026; new checks for the vitest run, a grep proving the
  component contains no `absolute left-[` / `top-[` stage positioning, lint, and build.
- Done means: tests green, `make lint` clean (no `type: ignore`, no `any`), build passes,
  `docs/00-merge-status.md` updated, decision recorded, **one commit**:
  `M<N>: green — <one line>`.

## What not to touch

`fee-faq.tsx` · `lib/nav.ts` / `SECTION_TABS` · the fee model or any copy about what the fee
*is* elsewhere on the site · anything under `kite-momentum-rebalancer/` or `frozen/` · the
three-ways section. No new npm dependencies.
