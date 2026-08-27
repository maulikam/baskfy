# Gates: bring the "From intent to result" animation up to date

> ## ⚠️ SUPERSEDED, 27 Aug 2026 — read `gates/marketing-flow-responsive.md` instead
>
> **G1 of this file is reversed.** It reads *"The stage shows what a step costs, before the plan
> reaches a rail"*, and the cost box it demanded has been deleted on Maulik's direct instruction,
> written down in `FLOW-REFINE-PROMPT.md` at the repo root and recorded as **MKT3** in
> `docs/DECISIONS-MERGE.md`. The section heading changed with it, so the page no longer promises a
> cost the picture does not draw. G1's CHECK will now fail, and that failure is correct.
>
> **G5 is retired, not reversed.** It re-derived every wire's length against the fixed
> `1360 × 420` canvas. That canvas is gone — the diagram is a CSS grid in normal document flow and
> connectors are per-gap SVGs with no coordinates of their own — so the arithmetic it checked no
> longer exists to be checked. Its replacement is structural: greps that fail if `absolute`, a
> fixed width or an `overflow-x-auto` wrapper ever come back.
>
> **G2, G3, G4, G6, G7 and G8 survive in intent** and are re-expressed against the new spec in
> `gates/marketing-flow-responsive.md` (as G4, G5, G6, G10, G9 and G11 respectively).
>
> This file is kept rather than deleted because it is the record of what was **measured** on
> 25–26 Aug 2026 — the broker counts, the factor count, the shipped Portfolio tabs, and the honest
> reading of what `curated_plans.py` does and does not carry. That measurement is still true; only
> the conclusion drawn about where the fee belongs on the stage has changed.

Scope: `apps/web/src/components/marketing/how-it-works-flow.tsx`, the animated diagram under the
landing heading *"From intent to result, with the cost visible before it runs."* It was drawn on
24 Aug 2026, before the portfolio redesign shipped, and it now describes a product that is a
version behind. The section's own heading promises a cost the diagram never shows.

```
node ~/.claude/skills/unlazy/scripts/gate-check.mjs gates/marketing-flow-refresh.md
```

## Facts measured before writing these gates (25–26 Aug 2026)

Every number below was read off the source, not recalled.

| Claim on the stage today | Measured reality |
|---|---|
| "Sixty-four factors" | `factor_registry.NAMED_FACTOR_COUNT == 64`. **Correct — keep.** |
| Left node = *Screen · Basket · Portfolio*, "Set the rules once." | The redesign made the front door holdings-first: connect a broker, holdings arrive **Unallocated**, then you organise. `new-portfolio-flow.tsx`, `unallocated-section.tsx`. |
| Rails = *Zerodha Kite · Manual · CSV export* | `broker_connections.BROKERS` has **10** brokers. Exactly **one** (`zerodha`) is `holdings_sync="ready"` / `trading="ready"`; the other nine are `planned`/`partner`. Any copy that implies ten live connections is false, and `test_broker_capability_honesty.py` exists because that failure has happened before. |
| Right node pills = *Allocations · Weights · Rebalances · Tradebook* | Shipped hub is `Portfolio → Overview | Portfolios | Holdings | Activity | Watchlist` (`lib/nav.ts`, and ten `page.tsx` files under `app/(app)/portfolio/`). Plus daily NAV (`portfolio_nav.py`), XIRR (`MetricKind.XIRR_*`), reconciliation (`reconciliation.py`), CAS statement import (`cas_import.py`). |
| **No cost anywhere on the stage** | The heading promises one. |

### The cost claim, measured honestly

`page.tsx:201` currently reads *"what the step will cost you in brokerage and statutory charges
is on screen before you press it."* That is **overstated**:

- `curated_plans.py` and `routers/curated_investments.py` carry **no** cost field — grep for
  `cost|charge|stt|brokerage` returns nothing but a comment. No plan surface in the web app
  renders a brokerage or STT line.
- `/portfolio/[id]/costs` exists but is **platform fees accrued**, not statutory charges.
- What *is* shipped and true: the platform fee model in `fee-faq.tsx` — buy/invest-more accrue
  `min(₹100, 1.5% × amount) + 18% GST`; SIP instalments cap at ₹10; **rebalance, exit, partial
  exit and customize accrue zero** — and its own sentence, *"Broker and statutory charges stay
  with the broker and are not modeled here."*
- `packages/core/src/baskfy_core/costs.py` models the six statutory components for Zerodha CNC,
  but it feeds backtests, not a pre-trade screen.

So the diagram gets a cost card carrying the platform fee that is real, and says plainly that
brokerage and statutory charges are the broker's. The section blurb is corrected to match. A
diagram that drew a charges screen this product does not have would be the exact failure the
house rules exist to prevent.

---

- [ ] G1: The stage shows what a step costs, before the plan reaches a rail — and states the
      platform fee that `fee-faq.tsx` states, not an invented one.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "cost" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: reversed — see the banner above; the cost box is gone by instruction

- [x] G2: No broker is presented as connected except the one that is. `zerodha` is the only
      `ready` entry in `BROKERS`; the diagram must not imply the other nine are live.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "broker" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  4 passed | 19 skipped (23)

- [x] G3: The diagram's journey matches the shipped one — holdings arrive first and land in the
      `Portfolio` hub's own surfaces, not in the pre-redesign vocabulary.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "journey" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  7 passed | 16 skipped (23)

- [x] G4: The accessible fallback still carries the whole journey in reading order — the SVG is
      `aria-hidden`, so the numbered list is the only version a screen reader gets, and it must
      not have fallen behind the stage.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "reading order" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  3 passed | 20 skipped (23)

- [ ] G5: Geometry still agrees with itself. Every wire's `length` is the path's real length, so
      the travelling dash is the same size on all of them; every node the wires point at is where
      the wires say it is.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "geometry" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: retired — the fixed canvas this measured no longer exists

- [x] G6: §8's retired jargon does not reappear, and no §9 forbidden word ("managed",
      "advisory", "PMS") reaches the marketing copy.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/__tests__/no-jargon.test.ts 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  9 passed (9)

- [x] G7: The section blurb no longer promises a brokerage-and-statutory screen the app does not
      have, and the landing page still says nothing executes here.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run -t "blurb" src/components/marketing/__tests__/how-it-works-flow.test.tsx 2>&1 | grep -E 'Tests +[0-9]+ (passed|failed)' | tail -1
  EXPECT: /Tests +\d+ passed/
  EVIDENCE: Tests  3 passed | 20 skipped (23)

- [x] G8: Typecheck clean, the whole web suite green (baseline 1,869 passing), eslint adds
      nothing to the pre-existing 22 problems.
  CHECK: bash tools/marketing/flow-refresh-check.sh
  EXPECT: /FLOW OK/
  EVIDENCE: ✖ 22 problems (21 errors, 1 warning) | FLOW OK (eslint 22 <= 22)

ABANDON: G1 reversed 27 Aug 2026 by Maulik's instruction (FLOW-REFINE-PROMPT.md, MKT3) — the fee gets no box on the stage; re-expressed inverted as G3 of gates/marketing-flow-responsive.md
ABANDON: G5 retired 27 Aug 2026 — the fixed 1360x420 canvas whose geometry this re-derived was deleted; replaced by the structural checks G1/G6 of gates/marketing-flow-responsive.md

<!--
A checked box with EVIDENCE still "pending" counts as UNMET.
If a gate becomes impossible: add `ABANDON: G<n> <reason>` and say so in the report.
-->
