# 05 — UI spec: the web app's basket-product surfaces

Target: the existing Next.js app (`apps/web`), inside its current shell, brand (M25/M38)
and component culture. The desk console (:8420) is untouched except where a module says
otherwise. The observed product's screen inventory is spec §5–6; this file maps it to
routes and states the deviations. Disclaimers are components, not footers. The web app
never gains an order route — every order-shaped CTA ends in a **plan hand-off panel**
("Plan #… created — open the desk console to execute; expires at HH:MM"), or the
closed-market state.

## Route map

| Route | Replicates | Notes |
|---|---|---|
| `/explore` | §6.3 catalog | Filter/sort state entirely in query params (shareable URLs). Chips: Filters, Sort by, Under ₹25k, Under ₹5k, Free Access, Fee Based (last chip hidden while subscriptions off). Full-screen filter dialog with the §6.3 facet tree pruned to facets that have data — volatility, category, min-amount, rebalance frequency, constituents type, include-new toggle |
| `/basket/[slug]` | §6.4 detail | Tabs: Overview / Constituents / Updates (unread dot). Overview: thesis with read-more, performance chart (range pills, SIP toggle, benchmark compare), about-the-manager card, costs-and-returns card, disclosure footer component. Sticky CTA: Invest now / Invest more / Start SIP per state |
| `/basket/[slug]/constituents` | §6.4 | Rebalance timeline (next review → history with +N/−N chips → collapsed spans → "went Live"), holdings-distribution segment bars, grouped constituents table with subtotals, per-row weight. Export icon downloads CSV |
| `/manager/[slug]` | §6.7 | Profile from `cb_manager`; engine manager lists its strategies as "managed by" cards; SEBI line renders reg no. or "registration pending" honestly |
| `/collections/[slug]` | §6.1 "Take your pick" | Data-driven from `cb_collection` |
| `/watchlist` | §6.10 | Count, rows with watch date, daily change, moved-since-watchlisted, CTA |
| `/investments` | §6.8 | Net-worth header with eye-toggle, pending-actions carousel, grouped investment rows with nudge strips ("Rebalance update available", "N days since last investment"), exited-history link |
| `/investments/[id]` | §6.9 | Performance block with Show-Details modal (current investment, money put in, current/realized returns, XIRR, dividends), constituents table with per-stock returns, dividends card, rebalance banner, Manage list (constituents, SIP, orders, exit) — each Manage action ends in a plan hand-off or a read-only view |
| `/investments/[id]/orders` | §6.9→orders | Read-only batch list joined to desk journal status |
| `/fees` | §6.12 | Ledger display with accrued (not collected) framing and the fee-math FAQ |
| `/create` | §6.16 | Add ≥2 instruments (existing instrument search), weighting scheme (equal/custom with normalize), backtest preview (existing machinery), name & save → PRIVATE basket |
| `/home` additions | §6.1 | Pending actions + "based on your watchlist" + trending modules slot into the existing home/dashboard rather than replacing it |

Existing M22/M30-era `/baskets*` read-only pages: SC5 decides merge-or-redirect (they were
built from scans; the new catalog subsumes them). Record the decision; do not leave two
competing catalogs.

## Shared components (build once, in SC5)

- **BasketCard** — icon/monogram, name, manager line, access badge, one-line pitch,
  Min. Amount, headline return (window-labeled), volatility chip, watchlist toggle.
- **VolatilityChip** (LOW/MED/HIGH gauge), **AccessBadge**, **ReturnStat** (always shows
  its window label), **SegmentBars**, **RebalanceTimeline**, **PendingActionCard**
  (dismissible, with terminator card), **PlanHandoffPanel**, **MarketClosedModal** (reopen
  datetime + notify-me), **DisclosureBlock** (variants: performance-not-verified,
  registration-pending, history-caveat), **ShowDetailsModal** for investor math.
- Charts follow the app's existing charting stack; SIP-mode and compare are series
  toggles, not new chart types.

## States that must exist (the product lives in its edge states)

- Catalog empty-facet (filter matches nothing) with clear-all.
- Basket younger than every CAGR window (young-basket metric labels).
- Investment with pending rebalance vs applied vs skipped.
- Drift pending action, and the post-fix confirmation.
- Market closed on every order-shaped CTA.
- Broker session stale: order-shaped views show "desk session expired — refresh the token
  bridge" instead of silently failing.
- XIRR hidden (<1y) with the explanatory tooltip.
- PRIVATE basket rendering (no manager card, no disclosures beyond the standard, "your
  basket" framing).

## Explicit non-goals for the UI

No mobile-app chrome (5-tab bottom nav) — the app keeps its own navigation with these
routes added. No credit/FD/US tiles, no app-download banners, no referral/marketing
surfaces, no AI-picker entry point. No dark-pattern replication of upsells; the spec's
promo carousels are skipped except where a module needs a slot (pending actions).
