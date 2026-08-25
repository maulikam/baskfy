# Tree 6 — Nav / product IA + RSC performance — final report

**Verified:** 23 Aug 2026 · repo `/Users/maulikdave/Documents/projects/baskfy`  
**Spec:** `baskfynavrefactorreport.md` · **Gates:** `gates/node-6.md`, `gates/leaf-6.*.md`

---

## Executive summary

Tree 6 is **fully closed**. All **37** gates across the parent node and five leaves are checked with evidence; vitest re-run on closure verification: **21 passed** (nav 14 + jargon 5 + materialize 2).

Shipped:

1. **Nav IA** — Primary consumer nav collapsed to four items (Market · Baskets · Build · Me): desktop sliding-pill top nav, mobile bottom tab bar with safe-area padding, section tabs inside Market/Me/Baskets/Build hubs, thirteen permanent legacy redirects in `next.config.ts` mirrored by `LEGACY_REDIRECTS` in code.
2. **Basket-first screens** — Shared `BasketCard` / `BasketDetail`, `materializeBasket` (equal-weight + 5% cash), screen results default to basket view with Basket|Table toggle, featured page jargon-free, single SEBI disclaimer from AppShell.
3. **RSC performance (Tree 5, prerequisite)** — Timed server fetch (`server-fetch.ts`, 2500 ms default), Suspense on list pages, warm curl budgets met (explore/investments/watchlist/baskets ≤0.52 s).
4. **E2e + polish (leaf-6.5)** — `e2e/nav.spec.ts`: four-item IA at 768/1024/1280, mobile bottom tabs, legacy redirect walk, screen template defaults to basket view; consumer chrome stripped of user-visible "data version".

No web execute / OrderGateway paths were introduced (node-6 G9, Tree 5 G4).

---

## Gate ledger

| Gate file | Gates | Closed | Open |
|---|---:|---:|---:|
| `gates/node-6.md` | 10 | 10 | 0 |
| `gates/leaf-6.1-nav-shell.md` | 5 | 5 | 0 |
| `gates/leaf-6.2-routes-titles.md` | 4 | 4 | 0 |
| `gates/leaf-6.3-double-render-jargon.md` | 6 | 6 | 0 |
| `gates/leaf-6.4-basket-materialize.md` | 5 | 5 | 0 |
| `gates/leaf-6.5-e2e-nav.md` | 7 | 7 | 0 |
| **Total** | **37** | **37** | **0** |

Spot-check at report time: `pnpm exec vitest run` on nav + jargon-ban + materialize → **21 passed**.

Playwright (leaf-6.5 G7): build + `e2e/nav.spec.ts` → **19 passed** (recorded 2026-08-23 in gate evidence).

---

## Key commits

| Commit | Message | Scope |
|---|---|---|
| `05768be` | perf: green — RSC list pages under budget | Tree 5 — `server-fetch.ts`, timed explore/investments/basket/watchlist, Suspense |
| `1d6a90a` | UI: green — Market·Baskets·Build·Me nav and basket-first screens | Leaves 6.1–6.4 — nav shell, routes, jargon sweep, basket materialize |
| `774d3b6` | docs: record Tree 6 node-6 commit evidence | Gate evidence for node-6 G10 |
| `3ca784e` | UI: fix — single disclaimer on backtest detail | leaf-6.3 follow-up (backtest-result duplicate) |
| `764d03f` | UI: green — nav e2e, redirect registry, jargon polish | leaf-6.5 — `LEGACY_REDIRECTS`, freshness-pill copy, nav tests |
| `ea1b33b` | UI: green — nav e2e gate closed | leaf-6.5 — Playwright green, strict-mode h1 fix for Investing 001 |

---

## What changed (by area)

### Nav / IA (leaves 6.1–6.2)

- `decile-blueprint/apps/web/src/lib/nav.ts` — `PRIMARY_NAV` (4 items), section tab maps, `LEGACY_REDIRECTS`, active-section helper.
- `decile-blueprint/apps/web/src/components/shell/top-nav.tsx` — sliding pill, `aria-label="Primary"`, `aria-current="page"`.
- `decile-blueprint/apps/web/src/components/shell/bottom-tab-bar.tsx` — mobile four-tab bar, `safe-area-inset-bottom`.
- `decile-blueprint/apps/web/src/components/shell/section-tabs.tsx` — in-page tabs for Market / Baskets / Build / Me.
- `decile-blueprint/apps/web/src/lib/vocabulary.ts` — canonical hub titles (no "Rebalance Tracker").
- `decile-blueprint/apps/web/next.config.ts` — 13 permanent redirects (`/dashboard`→`/market/today`, `/explore`→`/baskets`, etc.).
- New route tree under `src/app/(app)/market/`, `build/`, `me/`, `baskets/featured/`.

### Double-render + jargon (leaf 6.3)

- Removed page-level `Disclaimer` imports from portfolios/backtests/screens lists and backtest-result; AppShell owns the single footer disclaimer.
- `src/lib/__tests__/jargon-ban.test.ts` — guards desk jargon and duplicate disclaimers.
- Featured basket page free of MomentumScan / run-id / data-version user copy.

### Basket materialize (leaf 6.4)

- `src/components/basket/basket-card.tsx`, `basket-detail.tsx` — shared card/detail.
- `src/lib/basket/materialize.ts` — equal-weight + `DEFAULT_CASH_BUFFER=0.05`.
- `src/components/screens/screen-basket-view.tsx`, `results-panel.tsx` — default `mode=basket`, toggle testids.
- `/baskets` auto section + `/build` screen cards badge "Auto — from your screens".

### E2e + consumer polish (leaf 6.5)

- `e2e/nav.spec.ts` — viewport loop, bottom tabs, legacy redirect table, basket-default screen run.
- `freshness-pill.tsx`, `buffer-explainer.tsx` — plain dates only (no "data version" in user chrome).

### RSC perf (Tree 5 — shipped immediately before Tree 6)

- `src/lib/api/server-fetch.ts` — `SERVER_FETCH_TIMEOUT_MS` (2500), `AbortSignal.timeout`.
- Timed paths in explore/investments/basket fetch modules and `(app)/layout` `fetchMe`.

---

## Remaining open items (from `baskfynavrefactorreport.md`)

These were **explicitly deferred** or only partially addressed; they do **not** block Tree 6 closure.

| Ref | Item | Status |
|---|---|---|
| §4 / F11 | **Global ⌘K search** across stocks, indices, baskets, and screens | **Partial** — palette exists (`command-palette.tsx`) with instrument search + nav "Go to"; baskets/screens/indices not in search index yet |
| §4 tablet | **"More" menu** when pills cannot fit mid-width | **Open** — desktop pills + mobile bottom tabs; no tablet collapse-to-More |
| §5.2 / F10 | **Watchlist star toggle** on Explore/BasketCard | **Open** — empty state still says "Star a basket from Explore when the toggle lands" (`me/watchlist/page.tsx`) |
| §5.2 | **`basket.source = screen:{id}` API persistence** on save | **Deferred** — UI projection only; recorded ⚠ UNREVIEWED in `docs/DECISIONS-MERGE.md` Tree6.2 |
| §5.2 | Re-run updates linked basket in place with "as of {date}" | **Partial** — UI materializes on run; no persisted curated-basket row per screen |
| §5.1 | BasketCard sparkline, top-3 logos, Watch CTA on shared card | **Partial** — card/detail exist; not full smallcase-style polish from report §5.1 |
| F10 | **New listings** data-quality flag (backfill artifact dates) | **Open** — listings page moved under `/market/listings`; no demotion banner |
| F9 (portfolios) | **Duplicated portfolio card** ("Main — 0 holdings…" twice) | **Not verified closed** — disclaimer duplication fixed; card double-mount not re-audited in gates |
| §6 step 5 | Full global search implementation order | **Future tree** |

---

## Tree 6 closure verdict

**Tree 6 is fully closed.** Parent node and all five leaves have zero unchecked gates; evidence is present in gate files; vitest spot-check green at report time.

Related but out of scope for this tree: `gates/leaf-screen-phases-3-6.md` G8 (full `make e2e`) remains open — not part of Tree 6 gate set.
