# 01 — Requirements: the smallcase spec, translated

Source: `smallcase-product-documentation.md` (repo root) — a build-ready walkthrough of the
live product, 22 Aug 2026. This doc does not repeat it; it sorts every feature into
**replicate**, **adapt**, or **skip**, and names the Baskfy owner for each. Section numbers
(§) refer to the source document.

## A. Replicate faithfully (the product core)

These are the features whose behaviour we copy as closely as the spec allows, because they
*are* the product.

1. **The basket abstraction** (§2): name, thesis/description, constituents with target
   weights grouped by segment, volatility label, min amount, access type, rebalance
   schedule, launch date, manager attribution. Owner: SC1 (model), SC2 (computation).
2. **Versioning and the rebalance timeline** (§6.4): every change to constituents/weights is
   a new immutable version with an effective date and a +N/−N/no-change label; the timeline
   renders from genesis ("went Live {date}") to next review date. Owner: SC3.
3. **The apply-rebalance flow** (§7.4): a published version does nothing to a portfolio until
   the investor applies it; the diff (buys/sells) is previewed; applying generates a batch of
   orders; skipping is a recorded state ("yet to apply"). This maps 1:1 onto the desk's
   plan→confirm→execute contract. Owner: SC3 (plan generation), desk console (execution).
4. **Discovery** (§6.2–6.3): a catalog page whose filters and sort live entirely in query
   params (volatility, category, min-amount buckets, access type, rebalance frequency,
   constituent type, "include new"), card anatomy (icon, name, badge, pitch line, min
   amount, headline return, volatility chip, watchlist toggle), and collections. Owner:
   SC2 (API), SC5 (UI).
5. **The detail page** (§6.4): Overview / Constituents / Updates tabs; performance chart
   with 1M/1Y/3Y/5Y/MAX ranges, since-inception absolute return, SIP-mode toggle, benchmark
   compare; holdings-distribution bars; constituents table grouped by segment with
   subtotals; about-the-manager card; costs-and-returns card. Owner: SC5.
6. **Watchlist** (§6.10): bookmark from any card; returns measured from the watchlisted
   date; "Moved by X% since watchlisted on {date}". Owner: SC2, SC6.
7. **Investment accounting** (§6.9, §8): Current Value, Money Put In, Current Returns,
   Realized Returns, XIRR (only after 1 year), dividends per instrument, per-constituent
   returns, exited-history. Owner: SC4.
8. **Drift repair** (§4.4, §7.8): holdings sold directly at the broker are detected and
   surfaced as a pending action ("Incorrect holdings — Fix now") whose resolution re-syncs
   the ledger. The desk's reconcile machinery already detects this; SC4 surfaces it.
9. **Market-hours gating** (§4.5): order-shaped actions outside 9:15–15:30 IST on trading
   days get the closed-market state (with the reopen date) instead of a broken attempt.
   Owner: SC3, using the existing trading calendar.
10. **Pending-actions engine** (§6.1): a queue of dismissible cards (drift, unapplied
    rebalances, "that's all" terminator). Owner: SC6.
11. **Disclosure furniture** (§8): performance disclaimers on every performance surface,
    manager registration line, "registration does not guarantee performance". These are
    components, not footers (house rule 9), and they render even in single-tenant mode —
    the habit must exist before the audience does. Owner: SC5.
12. **Fee mathematics** (§8): min(₹100, 1.5%)+18% GST per buy/invest-more, min(₹10, 1.5%)+GST
    per SIP instalment, zero platform fee on rebalance/exit — computed and journaled to a fee
    ledger on every order batch. Computation is replicated now; *collection* is Track B/C.
    Owner: SC4 (ledger), `04-business-rules.md` §1 (the math).
13. **Create/customize** (§6.16): ≥2 instruments, weighting schemes (equal/custom),
    backtested performance preview, save as a private basket that then behaves like any
    other investment. Baskfy's screener backtest machinery does the preview. Owner: SC8.
14. **Trending/ranking + collections** (§6.1): ranked lists (most invested, most watched,
    budget-friendly…) as scheduled jobs; curated collections as data, not code. In
    single-tenant mode most rankings degenerate (n of 1 user) — compute what is computable
    (returns-based, recency-based), stub the rest honestly. Owner: SC9.
15. **Updates feed** (§6.4): manager posts attached to a basket or manager; unread-dot on
    the tab. The momentum engine auto-posts a rebalance note per version (what changed and
    the regime context it already computes). Owner: SC9.

## B. Adapt (same idea, Baskfy's body)

1. **Two-tier auth** (§4.2). smallcase: platform login + broker OAuth per user. Baskfy now:
   the web app's existing auth is tier 1; tier 2 is the **single** Kite session from the
   token bridge, owned by the operator. The *gating matrix* is still implemented (§`02` here)
   so that order-shaped surfaces demand a live broker session — the check exists, the
   multi-user identity behind it comes with D3.
2. **The order path** (§4.3). smallcase places batch market orders directly. Baskfy routes
   every order through `packages/execution` (guards → risk → rate limit → journal → broker)
   and only from the desk console with `confirm=true`. The web app renders plans and order
   history read-only; its "Invest / Apply / Exit" CTAs produce a *plan* and hand off to the
   console. This is stricter than smallcase and stays that way until D3.
3. **Managers** (§6.7). One real manager exists: Baskfy's own engine (strategies as
   sub-profiles), plus Maulik's hand-curated baskets. The manager profile page renders from
   a Manager row so third-party managers are a data problem later, not a schema problem.
   SEBI reg-no fields exist and render "pending registration" honestly.
4. **Performance metrics windows** (§8). Same rules (CAGR window by basket age, XIRR after
   1y) but computed by `packages/core` pure functions against the repo's Postgres price
   history (2011→present, survivorship caveats as documented in NEEDS-MAULIK #12 apply and
   must be disclosed on charts that predate listing coverage).
5. **Notifications** (§8). smallcase: push/email/bell. Baskfy now: in-app bell + the
   existing ops notification channel. Email to self is fine; no marketing infra.
6. **Mutual-fund smallcases** (§2). The schema carries `type` (STOCK|MF|US) so MF baskets
   are representable, but only STOCK baskets are buildable — no MF data source exists.
   Catalog copy must not pretend otherwise.

## C. Skip (out of scope for the SC run — most are separate businesses)

Credit/loans against securities (§6.14), Fixed Deposits, US equities/Tickertape (§6.15),
smallcase Gateway B2B SDK (§3), broker-branded distribution, the mobile app and its
investment score (§6.8), payments/Razorpay activation, GST invoicing activation, PaRRVA
verification workflow (render the disclaimer text, skip the process), multi-broker adapters
(Kite only), "Let AI find your next investment", the app-download upsells. Skipping is a
product decision already taken — do not build stubs for these beyond what the schema
naturally carries, and do not list them in the UI as coming soon unless the spec's own
"Coming soon" treatment is being replicated deliberately (US tile: fine to omit entirely).

## D. The seven flows the run is graded on (from §7)

At SC12, these must work end-to-end in single-tenant mode:

1. Discover → filter → detail → watchlist (web, no broker session needed).
2. Invest (lump sum ≥ min amount) → plan preview in web → confirm in desk console →
   holdings appear in `/investments` with correct accounting. DRY_RUN path fully testable.
3. Rebalance: engine publishes a version → pending action + updates post appear → diff
   preview → apply via console → "applied" state recorded; or skip → "yet to apply".
4. Invest more / partial exit / full exit, with fee-ledger entries and realized-PnL math.
5. SIP: schedule (amount, day) → reminder fires on schedule (auto-order stays dark until
   D3; the desk's non-negotiable #1 forbids it anyway).
6. Create: ≥2 stocks → weights → backtest preview → private basket → investable like any
   other.
7. Drift: sell a constituent directly in Kite (simulated in tests) → pending action →
   fix-flow reconciles the ledger.
