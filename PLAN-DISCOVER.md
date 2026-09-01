# Plan: Baskets → Discover, an investment-discovery workspace

The brief asks for `Discover → Understand → Compare → Simulate → Customise → Invest → Rebalance`
across eleven sections. This file decomposes all of it, marks what is buildable against the data
that exists, and names what is blocked and on what. Gates for the slice executed in this run are
in `gates/discover-*.md`.

## The finding that shapes everything

**`cb_metrics` does not hold most of the numbers the brief's designs are made of.** Measured, the
whole column list (`packages/core/src/baskfy_core/models/curated_baskets.py:342-368`):

```
min_amount · volatility_bucket · volatility_value · ret_1m · ret_6m · ret_1y
cagr_3y · cagr_5y · since_inception_pct · volatility_basis · months_available
return_convention · dividends_included · computed_at
```

There is **no maximum drawdown, no recovery duration, no Sharpe, no Sortino, no turnover, no
benchmark delta, no rolling-return percentage, no holdings count, no sector or top-10
concentration**. §4's card table, most of §5's advanced filters and half of §6's comparison table
name figures that no table in this product currently computes.

That is a metrics-engine program (node N9), not a UI program. Building the UI first and feeding
it invented numbers would be the worst possible outcome for a product whose entire pitch is
evidence. So: every surface here renders what exists, says plainly where a figure is not
computed yet, and N9 records the exact gap as the bridge.

## Tree

```
L1  Baskets behaves like an intelligent investment-discovery workspace
├── N1  Truth pass — audit the brief's 16 claims, fix the pure defects
│   ├── 1.1 audit each claim to file:line, verdict TRUE / FALSE / PARTLY
│   ├── 1.2 return percentages render as percentages
│   ├── 1.3 "Swing" replaced with a labelled, defined risk measure
│   ├── 1.4 the December-2026 announcement points at its own page
│   └── 1.5 disclosure available where performance is shown, not only at the foot
├── N2  Information architecture
│   ├── 2.1 Baskets → Discover in nav, section tabs, vocabulary, redirects
│   ├── 2.2 Discover sub-tabs: For You · All baskets · Collections · Compare · Saved
│   └── 2.3 Create leaves Discover and lives under Build only
├── N3  The basket card
│   ├── 3.1 strategy identity that is not two initials
│   ├── 3.2 risk before return, aligned figures, units on every number
│   ├── 3.3 a prominent primary action plus Save / Compare
│   └── 3.4 what-it-does line from the strategy, not filler
├── N4  Save and select
│   ├── 4.1 save control on the card, wired to the watchlist API that already exists
│   └── 4.2 compare selection store + sticky "n of 3 selected" bar
├── N5  Compare
│   ├── 5.1 /discover/compare — like-for-like table over the metrics that exist
│   ├── 5.2 portfolio overlap computed from basket versions (data exists)
│   └── 5.3 "what is different" from construction rules
├── N6  Discover page composition
│   ├── 6.1 goal composer — sentence-style, filter language not advice language
│   ├── 6.2 three starting choices with an explicit match explanation
│   └── 6.3 full-width workspace: sticky filters · results · right rail; Cards|Table toggle
├── N7  Risk-return explorer (volatility × return, size = minimum)
├── N8  Evidence and disclosure surfaces
└── N9  Metrics gap register — what §4/§5/§6/§7 need, where it would be computed
```

## Contracts (written before any leaf, so leaves integrate)

- **Selection state** lives in `lib/discover/selection.ts`, capped at 3, persisted in
  `localStorage` under `baskfy.compare.v1`. Slugs only — never whole cards.
- **Saving** goes through the existing `POST/DELETE /api/v1/watchlist` (`explore.py:496-567`).
  No second store, no optimistic write that cannot be reconciled.
- **Metric presentation** goes through one module, `lib/discover/metrics.ts`. Every percentage
  in the product gets its unit there; no component formats a return by hand again.
- **Match explanation** is computed in `lib/discover/match.ts` and always states which
  preferences matched *and how many were checked*. It is filter language: "matches 4 of your 5
  preferences", never "best for you" — D3 is unreviewed and the product is not an adviser.
- **Route naming**: `/discover` is the new hub. `/baskets` and every `/baskets/*` path redirect
  to it through the existing `LEGACY_REDIRECTS` + `next.config.ts` mechanism, and
  `scripts/check-shadowed-routes.mjs` keeps a redirected path holding nothing but a redirect.

## Regulatory and repo boundaries this plan will not cross

- **No execute route in the web app** (root CLAUDE.md, non-negotiable #1 and the D3 block).
  §10's broker connection, order preview and rebalance execution are the desk's, not this tree's.
- **No "recommended for you", no "best"** anywhere in Discover. The brief agrees; this makes it
  a checked gate rather than a good intention.
- **No invented metric.** A figure that is not computed is absent and labelled, never estimated.

## Status log

- 2026-08-25 — plan written; N1 audit completed against the working tree.
- 2026-08-25 — N1–N6, N8, N9 built and gated (`gates/discover-workspace.md`, 23/23).
  **N7 (risk-return explorer) not built** — it plots return against volatility, and with six
  baskets that all sit in one corner of that plane the chart would be decoration. It becomes
  worth building when the catalogue has spread or when maximum drawdown exists to put on the
  x-axis (`docs/DISCOVER-METRICS-GAP.md`, item 4). The other unbuilt sections of the brief — §7's
  richer detail page, §8's clone-into-builder, §9's Ask Baskfy, §10's broker and portfolio work —
  are unchanged above and are either blocked on the metrics gap or, for execution, on D3.
