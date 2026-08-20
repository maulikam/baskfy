# 08 — UI specification

## Design principles

This is a **numbers-dense professional tool**, not a marketing site. The visual job is to make
2,000 rows × 15 columns legible at a glance and to make a 20-field filter form feel calm.

- **Type:** one sans for UI (Inter or Geist), one **tabular-figure** setting for every number
  (`font-variant-numeric: tabular-nums`). Numbers right-aligned, always.
- **Density:** default row height 34px with a comfortable/compact toggle.
- **Colour:** neutral greyscale carries the layout; colour is reserved for meaning only —
  positive/negative, rank emphasis, and one accent for primary actions. Never colour a whole row.
- **Dark mode is first-class** (traders live in it). Test both.
- **Motion:** ≤150 ms, transform/opacity only. No animated tables.
- **Every analytics surface** renders the `<Disclaimer/>` component.

## Routes

| Route | Rendering |
|---|---|
| `/` | marketing landing (SSG) |
| `/pricing`, `/faq`, `/about`, `/blog/*`, legal | SSG |
| `/dashboard` | RSC, revalidate on `data_version` |
| `/market-health` | RSC + client universe switcher |
| `/screens` | RSC list |
| `/screens/new`, `/screens/[id]` | client-heavy form + RSC first paint of results |
| `/screens/[id]/columns` | client |
| `/instruments/[symbol]` | RSC, ISR, SEO-optimised (this is the organic-traffic surface) |
| `/listings` | RSC + cursor pagination |
| `/portfolios`, `/portfolios/[id]/rebalance` | client |
| `/backtests`, `/backtests/[id]` | client + polling/SSE |
| `/profile`, `/invoices`, `/change-password` | RSC + forms |

## App shell

Collapsible left sidebar (matching the reference IA): Dashboard · Market Health · Screens ·
Rebalance Tracker · Backtests · Listings — then Account (Pricing, Invoices, Profile, Change
Password) — then Help (FAQ, Blog, Support). Top bar: global instrument search (`⌘K`),
data-freshness pill (`Data: 19 Aug 2026`), theme toggle, user menu.

A dismissible announcement banner slot at the top (the reference product uses it for the
December 2026 update).

## Screen editor — the core screen

Two-pane layout: **filter form (left, ~380px, scrollable, sticky actions)** + **results (right,
fills)**. On mobile the form becomes a full-screen sheet.

Form structure = accordion groups, in the reference product's order:

1. Always visible: Index Universe · Sort By (Factor) · Sort Direction · `Show More Filters`
2. General Filters · Moving Average Filters · Away from High · Percentage of Positive Days ·
   Circuit Filters · Marketcap Range · P/E Range · Series · Ignore Top Beta/Volatility ·
   Price (CMP) Range · Multi-Factor Combined Ranking · Historical Ranks · Custom Filters

Requirements:

- Each accordion header shows a **count badge** of active filters inside it, so a collapsed
  group never hides state.
- Sentinel values are explained inline exactly as the reference does ("Keep value as 100 if you
  want to ignore…"), and the field renders visually "off" when at its sentinel.
- The `Sort By` select has 62 options → use a searchable combobox grouped by family
  (Absolute / Sharpe / RSI / Beta-adjusted / Skip-month / Other).
- **Debounced live preview** (400 ms) hitting `POST /screens/preview`, with an explicit
  `Update & Apply Filters` button that persists. Show a "unsaved changes" state.
- Entire form state is mirrored into the URL via `nuqs` → shareable, back-button-correct.
- `Reset to defaults` and `Duplicate screen` actions. `Delete` is destructive-confirmed.

### Results panel

- Header strip: `N results` · `Results are shown for <date>` ·
  `Sorting Factor Column's Value = <FACTOR>` · `Edit Columns` · `Export`.
- TanStack Table + TanStack Virtual; **sticky header**; repeat the header row every 16 rows for
  long scrolls (a genuinely good idea borrowed from the reference product — make it optional).
- Client-side re-sort on any visible column (does not re-run the screen; label it clearly).
- Row hover → symbol link to the factsheet; click a row to open a **peek drawer** with the
  factsheet's top blocks without leaving the screen.
- Empty state must explain *why*: "0 results — the 1-year filters exclude instruments listed
  after 19 Aug 2025", with a one-click "loosen this filter" affordance.
- Loading: skeleton rows, never a spinner over stale data. Stale-while-revalidate with a subtle
  "updating" pill.

### Columns editor

34 toggles in the reference's order, grouped by family, with drag-to-reorder and a live preview
of the header row. Save writes `screen.columns`.

## Instrument factsheet

Follow the teardown's block order. Implementation notes:

- **Metric cards with medians** — value, sparkline, and a subdued `Median: x` line. The median
  is the stock's own history; add a tooltip saying so.
- **PROS/CONS** — green/red chips with icons, generated from the rule table in `05 §16`.
- Returns/Sharpe/Volatility/RSI render as a compact 5-column grid, one row per family, with a
  small bar behind each cell showing that value's percentile within the current universe. This
  is a real improvement over the reference product's plain numbers.
- Corporate actions table; flag rows that materially affect adjusted history.
- SEO: server-rendered title `CUPID share price, momentum & factor analysis`, JSON-LD, OG image
  generated per instrument.

## Dashboard (indices)

Virtualised card grid or dense table (user toggle), sortable by %chg / PE / PB / DivYield,
search box, sector grouping, and a sparkline per index (30-day). Handle `-` for indices with no
fundamentals.

## Market Health

Universe selector + four large gauges. Add what the reference lacks: a **history chart** of each
breadth series with the Nifty overlaid, which is the actual analytical use of breadth data.

## Rebalance tracker

Wizard: choose portfolio (or upload CSV, with a downloadable sample) → choose screen → set
`top_n` and `hold_buffer` → results as three columns (Exits / Inside WRH / Entries) each with
copy-to-clipboard and CSV. Show unmatched symbols prominently rather than silently dropping.

## Backtests

Config form (screen, date range, rebalance frequency, top N, weighting, costs) → queued job with
progress → results page: equity curve vs benchmark, drawdown chart, metrics table, per-period
holdings, trade log, and an "assumptions" panel that states costs, slippage and the
survivorship-bias handling in plain English.

## Accessibility & quality bar

- All interactive elements keyboard reachable; the table supports arrow-key navigation.
- Contrast ≥ 4.5:1 in both themes, including the positive/negative number colours.
- `prefers-reduced-motion` respected.
- Every form field has a real `<label>`; sentinel explanations use `aria-describedby`.
- Lighthouse ≥ 95 on the SSG marketing/instrument pages.
