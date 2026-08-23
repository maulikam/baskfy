# Baskfy — Navigation & IA Refactor Spec

**For:** Claude Code (CLI) — implementation brief
**Audited:** 23 Aug 2026, all 10 top-nav routes on `localhost:3000`
**Scope:** (1) Refactor the header menu into a modern, minimal, Gen Z-style navigation. (2) Make every screen (Stock finder result) automatically materialize and display as a basket / smallcase-style card.

---

## 1. Current state (as audited)

Header nav renders 10 flat, ungrouped top-level links:

| # | Nav label | Route | Document title | What the page actually is |
|---|-----------|-------|----------------|---------------------------|
| 1 | Market today | `/dashboard` | Market today | Table of 176 NSE indices, daily moves |
| 2 | Market mood | `/market-health` | Market mood | Breadth dials + history for a chosen index |
| 3 | Stock finder | `/screens` | Screens | Screener: saved searches + 6 templates; results = raw 268-row table |
| 4 | My portfolios | `/portfolios` | **Rebalance Tracker** | Holdings upload + rebalance vs. a screen |
| 5 | Explore | `/explore` | Explore | Curated **basket** catalog (currently 0 baskets shown) |
| 6 | Investments | `/investments` | Investments | **Baskets** you hold (ledger, read-only) |
| 7 | Watchlist | `/watchlist` | Watchlist | **Baskets** you starred (feature not finished — "when the toggle lands") |
| 8 | Today's basket | `/baskets` | Today's basket | The desk's live MomentumScan **basket** (15 stocks, weights) |
| 9 | Time machine | `/backtests` | **Backtests** | Backtest runner keyed to screens |
| 10 | New listings | `/listings` | New listings | Recently listed companies table |

Also in header: logo, stock-only search box, data-date chip, theme toggle, account button, one **unlabeled icon button**, plus a dismissible announcement banner above everything.

---

## 2. Flaws found (with evidence)

### F1 — Ten flat items, zero hierarchy
Ten top-level links with no grouping. This exceeds comfortable scan capacity (~5–7), gives every page equal weight (an unfinished Watchlist sits beside the core Market today), and provides no scent of how pages relate. Nothing is nested, so the product's actual pipeline (screen → backtest → basket → invest → track) is invisible.

### F2 — Nav physically overflows the viewport
At a 1069 px-wide window the last item renders as a truncated "**New listir**" with no overflow affordance — no "More" menu, no scroll indicator, no hamburger. On tablet/mobile widths, items past the fold are effectively unreachable. This alone makes the current markup a rewrite candidate rather than a patch.

### F3 — Three different names for the same thing (label ≠ route ≠ title)
- "Stock finder" → route `/screens` → title "Screens" → H1 "Stock finder"
- "My portfolios" → `/portfolios` → title "**Rebalance Tracker**"
- "Time machine" → `/backtests` → title "Backtests"
- "Market mood" → `/market-health`
- "Market today" → `/dashboard`
- "Today's **basket**" (singular, one specific basket) → `/baskets` (plural, implies the catalog — which actually lives at `/explore`)

Consequences: browser tabs/bookmarks/history don't match what the user clicked, analytics and support conversations use different vocabulary than the UI, and internal names ("screens", "backtests", "rebalance tracker") leak to users.

### F4 — Four separate top-level entries are all "baskets"
Explore (catalog of baskets), Watchlist (baskets watched), Investments (baskets held), Today's basket (one featured basket) are one concept scattered across four menu slots. Each of the four pages even cross-links to the others ("Explore baskets", "Browse the catalog", "See also: Explore / My portfolios / Time machine") — the pages themselves are compensating for the menu's failure to group them.

### F5 — Three separate top-level entries are all "market context"
Market today, Market mood, New listings are sibling read-only market dashboards. They differ only by lens and belong under one "Market" section with tabs.

### F6 — The core pipeline is dismembered
The product's real flow is: **build a screen → backtest it → hold it as a basket → invest/track**. Today that's four unrelated menu items with four unrelated names (Stock finder, Time machine, Today's basket, Investments), and the connection only appears in body-copy cross-links. A backtest is even listed by its screen's name ("Investing 001") under a menu item called "Time machine" — the user has to already know the mapping.

### F7 — Screen results are raw quant tables, not products (the smallcase gap)
Running any screen dumps a 268-row table with columns like "Sorting Factor" and "CLOSE RAW". Meanwhile Today's basket page already proves the product knows how to present the same idea as an investable basket (name, weights, amount per stock, cash %, min investment). The two renderers are unconnected. This is the target of requirement #2 (spec in §5).

### F8 — Desk/internal jargon leaks into a consumer UI
"Run f486d55c2d464980, data version 1", "this builds a **desk plan**", "Execution stays in the **desk console**", "the live MomentumScan view **for the desk**". Fine for an internal tool; alienating for a consumer/Gen Z audience.

### F9 — Literal duplicated content (rendering bugs)
- `/portfolios`: the "Main — 0 holdings — Rebalance — Sleeves" card renders **twice**, and the SEBI disclaimer footer renders **twice**.
- `/backtests`: SEBI disclaimer renders **twice**.
Likely a layout component mounted twice (double-wrapped layout or duplicated footer include). Fix while in there.

### F10 — Dead-weight and misleading items
- Watchlist's own empty state admits the feature isn't built ("Star a basket from Explore **when the toggle lands**") — yet it holds a permanent top-level slot.
- New listings shows ~100 companies all "listed" on 17 Aug 2026 — a data-backfill artifact presented as fact. Demote/flag until the data is truthful.

### F11 — Fragmented search
Header search is stock-only ("Search any stock…"); the indices table has its own separate search; baskets and screens aren't searchable at all from the header. One global search should cover stocks, indices, baskets, and screens.

### F12 — Accessibility gaps in the header
One icon button has no accessible name; nav links carry no `aria-current="page"` semantics beyond visual pill styling (verify); the overflow (F2) means keyboard users can tab to links that are invisible.

---

## 3. Target information architecture

Collapse 10 items → **4 + search + account**. One-word, plain-language labels.

```
Baskfy   [ Market ]  [ Baskets ]  [ Build ]  [ Me ]        ⌘K Search   ● date  ☾  👤
```

| New top-level | Contains (as tabs / segmented control inside the page) | Absorbs old routes |
|---------------|--------------------------------------------------------|--------------------|
| **Market** | Today · Mood · New listings | `/dashboard`, `/market-health`, `/listings` |
| **Baskets** | Explore (catalog) · Featured ("Today's basket") · *auto-baskets from screens (§5)* | `/explore`, `/baskets` |
| **Build** | My screens · Templates · Backtests (a tab of a screen, not a separate world) | `/screens`, `/backtests` |
| **Me** | Investments · Portfolios · Watchlist | `/investments`, `/portfolios`, `/watchlist` |

### Routes (canonical, kebab-case, label = route = title)

```
/market            → redirect to /market/today
/market/today      (301 from /dashboard)
/market/mood       (301 from /market-health)
/market/listings   (301 from /listings)
/baskets           → catalog grid   (301 from /explore)
/baskets/featured  (301 from old /baskets "Today's basket")
/baskets/[slug]    → basket detail (shared renderer, §5)
/build             → my screens + templates (301 from /screens)
/build/[screenId]  → screen editor + results-as-basket (§5)
/build/[screenId]/backtests   (301 from /backtests, deep-linked per screen)
/me                → redirect to /me/investments
/me/investments    (301 from /investments)
/me/portfolios     (301 from /portfolios)
/me/watchlist      (301 from /watchlist)
```

Every old URL gets a permanent redirect. Document `<title>` must equal the nav label + section (e.g. "Mood · Market · Baskfy"). Kill the "Rebalance Tracker" title.

### Naming rules
- One word per top-level item; sentence case; no internal names (screen run IDs, "desk", "MomentumScan") anywhere user-facing. Run metadata moves behind an ⓘ "About this data" popover.
- The word **basket** is the single noun for a weighted stock list everywhere (never "plan", "scan", "search result").

---

## 4. Gen Z-style header — design + behavior spec

**Desktop (≥1024px)**
- Slim sticky header, backdrop-blur, single row: logo · 4 nav pills · global search · date chip · theme · avatar.
- Active item = filled pill (keep the current blue pill pattern — it's the one thing that already works); inactive = ghost text, hover = soft pill. Animate the active pill between items (layout animation, e.g. framer-motion `layoutId` or CSS view-transitions) — this "sliding pill" is the signature Gen Z touch.
- Second-level tabs (Today / Mood / Listings etc.) render as a segmented control *inside the page header*, not in the global nav.

**Mobile (<768px) — the important part**
- **Bottom tab bar** with 4 icons + labels: Market, Baskets, Build, Me (app-like; this is what a Gen Z audience expects, not a hamburger of 10 links).
- Header shrinks to: logo · search icon · avatar.
- Safe-area padding (`env(safe-area-inset-bottom)`), active tab tinted + filled icon.

**Tablet / mid widths**
- Nav must never overflow invisibly again: pills compress to icons+tooltips before wrapping; if it still can't fit, last items collapse into a "More" menu. Add a Playwright test asserting all nav items are visible/reachable at 768, 1024, 1280 px.

**Global search (F11)**
- ⌘K / tap-search opens a command palette searching stocks, indices, baskets, and screens, with recent items. Replaces both existing scoped search boxes as the primary entry.

**Accessibility**
- `<nav aria-label="Primary">`, `aria-current="page"` on active link, every icon button gets `aria-label` (fix the unlabeled one), visible focus rings, full keyboard reachability.

**Cleanups while in the header**
- Announcement banner: max one line, dismissal persisted, never pushes nav off-screen.
- Fix double-mounted footer/disclaimer and duplicated portfolio card (F9): render the SEBI disclaimer exactly once per page from the root layout.

---

## 5. Requirement 2 — every screen auto-displays as a basket / smallcase

**Principle:** a screen's output is never a bare table again. Running or saving a screen materializes a **basket** — the same visual object used in Explore and Today's basket — with the table demoted to a secondary "Data" view.

### 5.1 Shared `BasketCard` + `BasketDetail` components
Build once, used by: Explore catalog, Featured basket, screen results, Me→Investments, Me→Watchlist.

Card shows (smallcase-style): basket name + one-line thesis · sparkline (1Y) · CAGR & volatility chips · min. investment amount · top-3 holdings logos + "+N more" · CTAs: **View**, **Backtest**, **Watch**.

Detail shows: weights donut + holdings table (rank, symbol, weight %, price, amount for a chosen investment size) · cash % · rebalance frequency · backtest summary if one exists · the existing data-quality warnings (split/bonus history) as a collapsible note.

### 5.2 Screen → basket materialization rules
- On **Run**: take the screen's ranked result, apply its `top N` (default 20, reuse the screen's existing setting), weighting = equal-weight by default or factor-weighted if the screen defines it, cash buffer default 5%. Render as `BasketDetail` immediately. Toggle: `Basket | Table` (basket is default; table keeps Edit Columns/Export for power users).
- On **Save**: persisting a screen creates/updates its linked basket (`basket.source = screen:{id}`), so `/build` lists **basket cards**, not config rows, and saved screens also surface in `/baskets` under an "Auto — from your screens" section, labeled as auto-generated.
- Re-runs (data refresh) update the linked basket in place; show "as of {date}" like Today's basket does.
- Backtest CTA on the basket runs the existing backtest engine against the source screen — linking F6's dismembered pipeline into one surface.
- Today's basket becomes simply a featured basket rendered by the same components (delete its bespoke page layout).

### 5.3 Acceptance criteria
1. Running any of the 6 templates lands on a basket view (name, weights, min amount, sparkline) with zero extra clicks; table available behind a toggle.
2. Saving a screen makes it appear as a basket card in `/build` and in `/baskets` (auto section) without further action.
3. Explore, Featured, screen-result, Investments, and Watchlist all render the identical `BasketCard`/`BasketDetail` components (one source of truth).
4. No user-visible string contains: run IDs, "data version", "desk", "MomentumScan", "Rebalance Tracker".

---

## 6. Suggested implementation order for the CLI

1. **Header rewrite** — new 4-item nav + sliding pill + mobile bottom tabs + a11y labels (F1, F2, F12).
2. **Route renames + 301 redirects + title normalization** (F3), section-level tab layouts for Market and Me (F4, F5).
3. **Shared BasketCard/BasketDetail** extracted from the Today's-basket page (F7 groundwork).
4. **Screen → basket materialization** per §5 (requirement 2; closes F6, F7).
5. **Global ⌘K search** across stocks/indices/baskets/screens (F11).
6. **Cleanups**: double-rendered disclaimer/cards (F9), jargon sweep (F8), Watchlist star toggle or demotion, New listings data flag (F10).
7. **Tests**: Playwright — nav visible at 768/1024/1280, every old route 301s, run-a-template lands on basket view, exactly one disclaimer per page.

---

*Prepared from a live audit of the running app; all flaw citations reference actual rendered output on 18–23 Aug 2026 data.*
