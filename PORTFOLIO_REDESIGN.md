# Baskfy — Portfolio Redesign Spec (v1)

**Status:** Approved direction, ready for implementation
**Scope:** Replaces the current `Me → Investments | Portfolios | Watchlist` section
**Audience:** Implementation agent / engineers. Every "MUST / MUST NOT / v1 / LATER" marker is normative.

---

## 0. One-sentence summary

Baskfy's core is an **allocation ledger** that reconciles real broker holdings into user-defined, independently measurable portfolio groups; the Portfolio page is a display over that ledger. Build the ledger first — the page is thin once it exists.

---

## 1. Problems with the current UI (all must be fixed)

1. "Investments" and "Portfolios" are two competing mental models for the same thing. Merge them.
2. "Main" appears twice in the nesting list with no differentiation — treat as a bug.
3. Internal jargon leaks into the UI: *book, box, sleeve, nest, divide, file under, spans brokers, run by hand, your rule*. Users never see these words (rename table in §8).
4. Assigned capital is shown with no market value and no performance — a number with no meaning to the user.
5. No combined chart, no per-portfolio contribution, no broker/price sync status, no distinction between actual holdings and theoretical capital.
6. Empty states funnel to "Explore baskets" (catalog-first). The redesign is **holdings-first**: the user's existing demat holdings are the starting material, not the catalog (§6).
7. Delete sits exposed next to primary portfolio actions — move behind an overflow menu with confirmation.

---

## 2. Navigation

Replace `Me → Investments | Portfolios | Watchlist` with:

```
Portfolio → Overview | Portfolios | Holdings | Activity | Watchlist
```

- **Overview** is the default landing tab: the single-screen consolidated view.
- **Holdings** is the flat, broker-level truth (every share, where it sits, what it's allocated to).
- **Me** becomes profile / settings / subscriptions / security only. No investment data under "Me".

---

## 3. Object model (canonical vocabulary)

| Object | Definition |
|---|---|
| **Broker account** | Where shares are actually held (Zerodha, Upstox, …). Source of physical truth. |
| **Holding** | Actual stock quantity in one broker account. |
| **Portfolio** | A user-created logical grouping of holdings. Two kinds — see §4. |
| **Basket** | Target stocks + target weights (a model). |
| **Screen** | Rules producing a ranked stock list. |
| **Strategy** | Rules that create, rebalance and exit a basket. |
| **Subscription** | A basket/strategy published by someone else on the platform. |

Every portfolio has exactly one **source**, shown as a badge everywhere:

- `Subscribed` — model published by someone else (see §9 for legal wording)
- `My screen` — built from the user's screening output
- `My strategy` — driven by the user's saved strategy rules
- `Holding group` — manually selected positions from one or several brokers

---

## 4. Accounting rules (the heart of the product — get these right before any UI)

### 4.1 Two portfolio kinds

**Capital portfolio**
- A holding belongs to **exactly one** capital portfolio (or to Unallocated).
- Capital portfolios + Unallocated sum exactly to consolidated net worth. No double counting, ever.

**Monitoring view**
- Overlapping lenses ("All defence stocks", "Bought in 2026"). A holding may appear in many.
- **Excluded from consolidated totals.** Always labeled: *"Monitoring view — overlaps with other portfolios, excluded from totals."*
- Visually distinct (muted styling) from capital portfolios in every list.

### 4.2 Whole-holding allocation only (v1)

- v1: a holding is allocated **whole** to one capital portfolio. **No partial-quantity allocation** (no "600 shares to Momentum, 400 to Long-term").
- Rationale: partial allocation breaks sell attribution and corporate-action math (§4.3, §4.5). LATER (Phase 3), behind the ledger being stable.

### 4.3 Sell attribution — the reconciliation inbox

Broker sync only sees quantity changes; it cannot know which logical portfolio a sell came from.

- With whole-holding allocation, attribution is automatic: the sell belongs to the holding's one capital portfolio. Apply it silently.
- If sync detects a change that cannot be auto-attributed (holding was Unallocated, quantity mismatch, unknown inflow), create a **Reconciliation item**: *"We detected a sell of 100 HDFC Bank — which portfolio?"* with the most likely portfolio pre-selected.
- Unresolved reconciliation items freeze that holding's contribution to performance (show as "pending reconciliation") rather than guessing. **Never silently corrupt a portfolio's return series.**

### 4.4 Cash ledger

- One **Unallocated cash** bucket per broker account. External deposits/withdrawals land there.
- Assigning cash to a capital portfolio records an **internal cash inflow** to that portfolio (that is the XIRR cash-flow event). Buying a stock inside the portfolio is an internal cash→stock transfer — no XIRR event.
- Sub-portfolio XIRR is computed only from these internal flows. This keeps per-portfolio XIRR mathematically pure.

### 4.5 Corporate actions

- Listen for splits/bonuses (API or CAS parsing) and update quantity + average price across the affected holding wherever it appears (its capital portfolio and all monitoring views) atomically.
- A corporate action MUST NOT appear as a P&L event.

### 4.6 Data layers (keep separated in the schema)

1. **Broker ledger** — quantities, trades, cash flows (synced).
2. **Market data** — official EOD closes (v1), intraday LATER.
3. **Baskfy portfolio ledger** — which holding belongs to which portfolio, internal cash flows, since-added marks.

---

## 5. Performance rules

### 5.1 EOD-first (v1 decision)

- v1 is **end-of-day only**, presented honestly, like a fund NAV: a nightly job computes an official EOD value per portfolio and for the consolidated total.
- The EOD NAV series powers everything: combined chart, per-day P&L, contribution, drawdown.
- Header always shows: *"Valued at close of {date}"* + broker sync timestamp. No pretend-live numbers.
- LATER: intraday estimates, clearly labeled "estimated".

### 5.2 The return metric differs by source — never one unlabeled column

| Source | Default headline metric | Notes |
|---|---|---|
| Subscribed | **TWR since you subscribed** | Show model TWR separately; NEVER blend model and actual into one number. |
| My strategy | TWR since go-live | Plus strategy-vs-actual drift. |
| My screen | TWR since portfolio creation | |
| Holding group | **"Since grouped" return** (from EOD marks at grouping date) | Upgrade to true XIRR/total P&L only after transaction history exists (CAS import, §5.3). Until then, do NOT display XIRR or since-purchase P&L for these. |

- Consolidated level: show **XIRR** (user's cash-flow-adjusted experience) and **TWR** (strategy quality) as two labeled numbers. Do not use CAGR alone for user holdings.
- Backtested performance is its own labeled category and never mixes with any of the above.

### 5.3 CAS import

- Broker APIs often lack pre-connection buy history. Build **Import CAS** (CDSL/NSDL consolidated account statement, PDF parse) to backfill buy prices/dates.
- After CAS import, holding groups unlock true XIRR and since-purchase P&L. Before it, they show "since grouped" only.

---

## 6. Overview page (the single screen)

Top-to-bottom layout, desktop; cards collapse for mobile.

### 6.1 Header

- Title: **My Portfolio** — *Your complete investment picture across baskets and brokers.*
- Controls: Show/hide amounts · Sync status (per broker, with timestamp) · `+ New portfolio` · Export.
- Timestamps shown separately: *Prices: close of {date}* · *Holdings synced: {time}*.

### 6.2 Hero metrics (max 5)

Current value · Today's P&L (₹ and %, vs previous close) · Total P&L · XIRR · Invested amount.
Secondary (collapsed row): cash, realised/unrealised split, dividends, broker count.

### 6.3 Combined chart

- Ranges: 1M / 3M / 1Y / 3Y / All (1D/1W only after intraday exists).
- Toggles: value (₹) vs return (%) · benchmark overlay (default Nifty 500, per-portfolio override) · drawdown.
- Data source: the EOD NAV series (§5.1).

### 6.4 Needs-attention ribbon

Dismissible, each item deep-links to its resolution flow. v1 items: broker connection expired · N holdings unallocated · reconciliation items pending (§4.3) · rebalance available on a subscribed portfolio · stale price data. LATER: drift threshold, corporate-action review, overlap warnings.

### 6.5 Portfolio table

Compact table (desktop) / cards (mobile). Grouping tabs: All · Subscribed · My strategies · My screens · Holding groups · Monitoring views (muted).

Default columns: Name · Source badge · Value · Today · Return (headline metric per §5.2, labeled) · Brokers · Status (On target / Rebalance due / Pending reconciliation / Synced). Column customization LATER.

Row click opens a **right-side inspector drawer** (~40% width, no page navigation): tabs Performance · Holdings · Activity. Full detail page (§7) reachable from the drawer.

### 6.6 Unallocated section — the centerpiece, not a footer

- Always visible when anything is unallocated: unallocated cash + unassigned holdings with total value.
- Primary CTA: **"Organize into portfolios"** → the grouping flow (§6.7).
- **First-run experience:** connect broker → everything lands in Unallocated → the product actively helps sort it (suggest groupings by sector, by purchase era, by overlap with a subscribed basket). Getting from 40 unallocated holdings to 4 named portfolios IS activation. Do not funnel new users to the catalog first.

### 6.7 New portfolio flow

`+ New portfolio` offers: From a subscribed basket · From my screen · From my strategy · From broker holdings · Empty.

**From broker holdings** (two-panel picker): left = consolidated holdings (filter by broker / sector / stock; "select all unallocated"), right = the new portfolio building up with live total. Then: choose kind (Capital portfolio vs Monitoring view, with the §4.1 explanation) → name → benchmark → confirm. Whole holdings only (§4.2). A stock held at two brokers displays aggregated with broker breakdown preserved on drill-down: *HDFC Bank — 320 (Zerodha 200 · Upstox 120)*.

---

## 7. Portfolio detail page (v1 minimal)

- **Summary:** value, invested, today's P&L, total P&L, headline return metric (per §5.2), benchmark diff, cash, last sync.
- **Performance:** NAV chart + benchmark + drawdown. (Heatmaps, rolling returns LATER.)
- **Holdings:** qty, avg price (if known), current price, weight, today's contribution, total contribution, broker. For basket-backed portfolios add target weight + drift.
- **Activity:** buys/sells, internal cash assignments, dividends, corporate actions, rebalances, reconciliation history.
- **Source panel:** Subscribed → publisher, methodology, model-vs-actual, rebalance instructions. Screen/strategy → rules, last run, entries/exits. Holding group → included brokers, grouped-on date.

---

## 8. Language renames (global find-and-replace in UI strings)

| Current | Replace with |
|---|---|
| Box | Portfolio |
| Book | Portfolio group |
| Sleeve | Allocation |
| Divide | Create sub-portfolio |
| File under | Move to group |
| Spans brokers | Connected to N brokers |
| Your rule | My strategy / My screen |
| Run by hand | Manual holdings |
| Nest / nesting | (drop the concept from v1 UI entirely) |

Also delete repeated boilerplate ("shares stay in your demat…") from every card — say it once, in the footer.

---

## 9. Regulatory wording (India / SEBI)

- Baskfy is not SEBI-registered: NEVER use "managed", "managed portfolio", "advisory", "PMS" for third-party content.
- Use: **"Subscribed model by {publisher}"** / "Published by" / "Curated by".
- Keep the existing footer disclaimer on every page.
- No auto-execution of trades in this scope. Rebalance produces an **order plan** the user takes to their broker (existing "this page never places an order" behavior is correct — keep it).

---

## 10. Build order

**Phase 1 — the spine (do first, UI can stay ugly):**
broker holdings sync → allocation ledger (whole-holding, §4.2) → Unallocated bucket + cash ledger (§4.4) → nightly EOD NAV job (§5.1) → since-grouped return marks → reconciliation inbox (§4.3).

**Phase 2 — the page:** Overview (§6) with hero metrics, combined chart, portfolio table + drawer, unallocated-first onboarding (§6.6), needs-attention ribbon, renames (§8), detail page (§7).

**Phase 3 — depth:** CAS import (§5.3) → true XIRR for holding groups · partial-quantity allocation · monitoring-view suggestions · column customization · intraday estimates · contribution analysis, overlap detection, "explain today's move".

---

## 11. Acceptance criteria (v1)

1. Sum of all capital portfolios + unallocated (stocks + cash) equals consolidated net worth, to the paisa, at all times.
2. A holding can never be in two capital portfolios; monitoring views never affect any total.
3. Every displayed return number carries a label stating what it is (TWR / XIRR / since-grouped) and its start date on hover.
4. A sell detected by sync either auto-attributes (whole-holding case) or creates a reconciliation item — it never silently alters a return series.
5. Model performance and the user's actual performance are never combined into one figure.
6. A split/bonus changes quantity and average price but produces zero P&L.
7. No internal jargon from §8's left column appears anywhere in the UI.
8. With zero connected brokers and zero holdings, the empty state leads to "Connect your broker", not the basket catalog.
