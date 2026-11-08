# Product

Baskfy — momentum research and rules-based baskets for Indian equities.

Written 24 Aug 2026 from three confirmed answers (positioning, redesign scope, data honesty) plus
direct exploration of this repository. Facts marked _(inferred)_ were not confirmed by the owner
and should be corrected rather than trusted.

## Platform

web

## Stack

Next.js 15 App Router · React 19 · TypeScript 5.6 · Tailwind v4 (no config file; tokens in
`src/app/globals.css`) · shadcn/ui · TanStack Table/Virtual/Query · visx · Auth.js v5 · nuqs.
Backend is FastAPI + SQLAlchemy async + PostgreSQL 16/TimescaleDB + Celery, in the same monorepo.
Marketing pages are SSG with a distinct CSP; the app is dynamic behind a session cookie.

## Users

- **The operator (primary, real today).** One person — the owner — who runs the momentum strategy
  with his own money through the desk on Zerodha Kite. Every surface currently has exactly one
  real user, and the seeded catalog holds one basket.
- **Prospective retail investors in Indian equities** _(inferred)_ who want rules-based portfolios
  they can audit, not a fund they must trust. They already have a demat account and a broker
  login, and they have been burned by "backtested" claims with no visible method.
- **Not** institutional allocators, not options traders (that lab is frozen), not non-India markets.

## Product Purpose

Close one loop that the category leaves open: **research → prove → decide → own.**

Most tools in this space do one stage. A screener ranks and stops. A basket platform sells a
portfolio and hides the method. Baskfy runs the whole loop over one engine, and publishes the
arithmetic at every stage — the factor formulas, the point-in-time membership, the backtest's
assumptions, the constituent diff before a rebalance, and the order plan before anything executes.

## Positioning

Confirmed: the homepage argues **the full loop**, not the screener. Research and backtest are the
evidence; baskets are the output; the broker hand-off is the ending. The engine is the proof, not
the pitch.

Current positioning line (docs/14, verbatim in `lib/site.ts`): "Rank every NSE stock by momentum,
and know exactly which ones still belong in the top ten percent." **This line describes one stage
and is now under-scoped for the confirmed positioning** — the redesign should replace it.

## Operating Context

- Indian market hours, NSE/BSE 09:15–15:30 IST, weekdays. Broker sessions expire ~06:00 IST daily
  and cannot be refreshed — a structural fact, not a bug.
- Desktop-first in practice (a numbers-dense research tool), but the public pages are read on
  phones _(inferred)_.
- The nightly pipeline publishes a `data_version`; every number on the site traces to one.

## Capabilities and Constraints

**Real and working:** 64 ranking factors across 14 universes; saved screens; a 93-column CSV
export; a point-in-time backtest engine with a fragility report and an assumptions panel; market
breadth, regime and a ~117-index dashboard; per-instrument factsheets with percentile bars and
PROS/CONS; a curated-basket catalog with versions and constituent diffs; watchlist, fees ledger,
XIRR and drift; CSV portfolio import with a rebalance tracker; a read-only order plan handed to
the desk.

**Hard constraints — these are product identity, not limitations to design around:**

- **The site never places an order.** Orders fire only from the desk, only on explicit
  confirmation, and plans expire in 30 minutes. `DRY_RUN=true` is the default everywhere.
- **Not a SEBI-registered investment adviser.** No copy may promise or imply an outcome. The
  disclaimer is a component, never a footer afterthought, and a copy lint
  (`src/lib/__tests__/copy-lint.test.ts`) fails the build on banned phrasing.
- **No performance figure is advertised.** Nothing on the marketing surface may state a return the
  product has not measured and published.
- **Every return is a price return** — splits and bonuses adjusted, cash dividends excluded — and
  is roughly 1.2%/yr below a total return, compounding. Any surface showing a return owes the
  reader that sentence.
- Public API is off behind a source constant and a flag. Subscriptions and fee collection are
  flagged off pending a pricing decision.

**Thin today, and the page must not outrun it:** the catalog holds **one** basket
(`momentum-scan`), with metrics as of 2026-08-21 — min ₹2,03,374, volatility LOW, 1Y −3.71%,
since inception +14.53%. Price history starts 2017-01-02, not the intended 2011.
`fundamental_daily` is empty, so P/E is null everywhere and marketcap is sparse.

## Brand Commitments

- Name **Baskfy**; domain **baskfy.com**. `desk.modelbasket.in` remains the operator console.
- The app's live tokens follow **Kite's own convention** deliberately: cool near-white canvas
  `#f7f8fa`, Kite blue `--accent #1a5fc4` / `--brand #4184f3`, green-up/red-down semantics, 34px
  rows, 8px radius, no paper grain ("this one is a screen"). Looking like the broker terminal the
  user already lives in is a decision, not an accident.
- `--brand` is a **fill** colour only, never type; `--accent` is the legible form. A source scan
  (`no-brand-as-text.test.ts`) enforces it — note its docstring still cites the older `#ff4f00`
  orange and is stale, but the rule it enforces is live and correct.
- Product vocabulary is kept deliberately: D1 bucket, decile drift, Market Pulse, Replay, hold band.
- Tone (docs/14): "Copy should never promise outcomes; it should promise **clarity about the
  data**." Density is a virtue — docs/08 opens "This is a numbers-dense professional tool, not a
  marketing site," and a landing page that reads nothing like the product is a promise the product
  then breaks.

## Evidence on Hand

Real material the surface may use, no invention required: a live sample screen from the same API
the app reads; the one curated basket with real computed metrics; the factor-family table with
real counts; three real pricing rows; a real backtest with equity curve, monthly returns,
fragility runs and a 20-line assumptions panel; breadth and regime series; ~10,481 instruments.

## Product Principles

1. Publish the arithmetic. A number the reader cannot trace is a number we should not show.
2. Say what is reconstructed, degraded, or missing, in the row itself.
3. Round once, at write time, so the API, the table and the CSV can never disagree.
4. Never auto-execute. The human confirms, or nothing happens.
5. Honesty outranks polish: an empty state that explains itself beats a filled one that misleads.

## Accessibility & Inclusion

WCAG 2.2 AA is the floor and is enforced by tests, not discipline: 4.5:1 for text, 3:1 for
graphical objects, an axe pass in the e2e suite, and a source scan banning the brand orange as
type. Indian number formatting (lakh/crore) and INR throughout. Content is English (en-IN).
