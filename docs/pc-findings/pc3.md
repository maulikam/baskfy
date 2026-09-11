# PC3 — the portfolio detail workspace: what was built, what was found, and how to wire it in

**Leaf:** PC3 of the Portfolio Command Center tree. **Gates:** `gates/pc3.md`, 10 of 10.
**Owns:** `decile-blueprint/apps/web/src/lib/portfolio/detail-tabs.ts` and
`decile-blueprint/apps/web/src/components/portfolio/detail/` (new directory, tests included).
**Nothing else was touched.** The existing `components/portfolio/detail-*.tsx` are the parent's
and are imported, never edited.

---

## 1. The prop contract — what the parent must pass

The workspace fetches nothing and writes nothing. It is handed the bundle
`loadPortfolioDetail(portfolioId, range)` already returns.

```tsx
import { PortfolioDetailWorkspace } from "@/components/portfolio/detail/workspace";

<PortfolioDetailWorkspace
  detail={bundle.detail}          // PortfolioDetail            REQUIRED, non-null
  nav={bundle.nav}                // NavSeries | null           REQUIRED (null is a real state)
  activity={bundle.activity}      // readonly ActivityItem[] | null   REQUIRED
  failures={{ nav: bundle.failures.nav, activity: bundle.failures.activity }}
/>
```

| Prop | Type | Required | What it is |
|---|---|---|---|
| `detail` | `PortfolioDetail` | yes | `bundle.detail`. With no summary there is no page; the parent already falls back to `ReducedView` when this is null, and that is still the right call. |
| `nav` | `NavSeries \| null` | yes | `bundle.nav`. `null` renders the no-history state on Overview and Performance rather than an empty chart. |
| `activity` | `readonly ActivityItem[] \| null` | yes | `bundle.activity`. `null` means the read failed and `[]` means nothing has happened; the two render differently and must not be collapsed. |
| `failures` | `{ nav?: string \| null; activity?: string \| null }` | no | `bundle.failures`. The sentence printed in place of the chart or the feed. |
| `initialTab` | `DetailTabId` | no | Defaults to `"overview"`. Use `isDetailTabId()` from `lib/portfolio/detail-tabs` to validate a value out of the URL; an unknown one must not select a panel. |
| `onTabChange` | `(tab: DetailTabId) => void` | no | Fires on every change, for a parent that mirrors the tab into the query string. |
| `onRangeChange` | `(range: NavRange) => void` | no | The chart's range pills. Omit it and the pills do not render, which is correct on a page that cannot refetch. The existing `detail-screen.tsx` has a working `loadNavRange` the parent can reuse. |
| `chartLoading` | `boolean` | no | Dims the chart while a range is in flight. |
| `rebalanceSlot` | `ReactNode` | no | **PC4's drawer.** Rendered inside the Rebalance tab in place of the placeholder. |
| `onOpenRebalance` | `() => void` | no | Alternative to the slot when the parent owns the open state. With neither, the button is disabled beside its reason. |
| `manageSlot` | `ReactNode` | no | **PC6's management drawer**, same contract. |
| `onManage` | `() => void` | no | Same. |
| `children` | `ReactNode` | no | Rendered under the tab panel. The page's existing `ManageSection` and disclaimer go here. |

`DetailTabId` is `"overview" | "holdings" | "performance" | "allocation" | "risk" | "rebalance" |
"activity" | "settings"`.

### Notes for whoever wires it

* **The workspace provides its own `AmountsProvider` and its own show/hide-amounts toggle.**
  Nesting it inside the parent's provider is harmless (the inner one wins), but the parent should
  drop its own toggle to avoid two controls for one setting.
* **It renders its own `<h1>`** (the portfolio name) and its own source badge, so `PageHeader`
  should not also render one.
* It is a client component. The page stays a server component and passes the bundle down.
* It never calls `fetch`, `apiOrigin` or `serverApi`, and a test asserts that.

---

## 2. What §2.2 got wrong, in both directions

Per the brief's rule, a leaf that finds a blocked metric records it here rather than editing the
plan doc. Four findings, of which two say §2.2 was too strict and two add to it.

### 2.1 §2.2 was too strict — these are real

* **Contribution to return per holding is real at the portfolio level.** Already in §6.3, and
  confirmed: `DetailHoldingOut.total_contribution` is `value − cost_basis`
  (`portfolio_overview.py:4104`), i.e. unrealised P&L in rupees. Divided by `summary.invested`,
  the server's own cost figure, it gives each holding's contribution to the portfolio's own
  return in percentage points. Shipped as the `contribution` column.
* **Target weight and drift are real for a basket-backed portfolio.** Already in §6.3. Shipped,
  and the two cases are distinguished by two *different sentences*, not by one blank column: a
  hand-grouped portfolio says it has no model, and a basket-backed one whose model has published
  nothing says that instead. They have different remedies.

### 2.2 Three exact derivations §2.2 did not consider, and which are not estimates

The server's own construction makes each of these a restatement rather than a model. Recorded
because the next session will want to know whether they were invented.

1. **Today's move as a percentage, per holding.** `value = quantity × latest` and
   `todays_contribution = quantity × (latest − previous)`, so `value − todays_contribution` is
   exactly `quantity × previous`. The percentage is the ratio of the two, to the paisa.
2. **Unrealised P&L as a percentage, per holding.** `total_contribution = value − cost_basis`, so
   `value − total_contribution` is exactly the cost basis.
3. **Monthly, calendar and rolling returns.** Read off `NavSeriesOut.drawdown[].index`, the
   flow-adjusted wealth index the NAV job publishes, never off `points[].value`. The two differ
   whenever money moved, and only the first is a return.

The compound annual rate is the one figure on the Performance tab that is a transformation rather
than a restatement, and it is **refused below a window of a year** rather than extrapolated, with
the day count named in the reason.

### 2.3 New to §2.2 — blocked, and not previously listed

| Metric | Why it cannot be shown | What would unblock it |
|---|---|---|
| **Exchange and instrument type** | `InstrumentRefOut` carries `instrument_id`, `symbol` and `name` only. Nothing says NSE or BSE, equity or ETF. The brief asks for this column on the holdings table. | An exchange and instrument-type column on `instrument`, carried into `InstrumentRefOut`. |
| **Available quantity (free / T1 / pledged)** | `DetailHoldingOut` sends one `quantity`. The desk's non-negotiable 2 splits holdings into `quantity + t1_quantity + collateral_quantity`, and none of that reaches the portfolio payload. | Carry the three broker quantities into `DetailHoldingOut`. |
| **Price age per holding** | One `prices_as_of` covers the whole portfolio, so every row shares a clock. A per-row "stale" filter cannot be built; what *can* be built, and is, is "has no price at all". | A per-instrument price timestamp on the holdings payload. |
| **Realised P&L per holding** | `total_contribution` is `value − cost_basis`, which is unrealised by construction. Realised exists only at the aggregate level (`hero.secondary.realised_pnl`) and not on `PortfolioSummaryOut` at all. | A realised-gain field per holding off the trade ledger. |
| **Overlap between portfolios** | The Allocation tab is asked for it and the detail page reads **one** portfolio. The others' holdings are never fetched, so an overlap computed here would be an overlap against an empty set. | An overlap endpoint, or the parent fetching every portfolio's holdings for this screen. |
| **This portfolio's objective** | No objective or description field exists on a portfolio. | An objective on the portfolio record, set at creation. |
| **When it was last rebalanced** | `ActivityKind` is `BUY \| SELL \| DIVIDEND \| ASSIGN \| RELEASE \| EXTERNAL_DEPOSIT \| EXTERNAL_WITHDRAWAL \| CORPORATE_ACTION \| RECONCILIATION`. A rebalance is not one of them, so there is no event to date. `summary.status` can say "Rebalance due" and cannot say when the last one was. | A rebalance event written to the activity feed when a plan is applied. |
| **When it is next due for review** | No review cadence is stored against a portfolio. | A review schedule on the portfolio record. |
| **A target exposure for this portfolio** | The equity cap is a book-level regime setting on the swing sleeve (PC5's data), not a target held against any one portfolio. The Allocation tab therefore reports deployed-against-cash and says there is nothing to compare it to. | A target allocation model per portfolio, which §2.2 correctly calls a product decision. |

### 2.4 One thing that is arithmetically reachable and was deliberately not built

`NavSeriesOut.daily_pnl[].pct` is in the payload. A realised volatility over it is two lines of
arithmetic, and **it is not computed**, because a volatility with no stated window and no stated
annualisation convention is exactly the unstated model §2.2 refuses. The same goes for a Sharpe
ratio (which would additionally need a risk-free series that is not stored). `risk.test.tsx` has a
test asserting no such label ever appears, so a later session cannot "finish" the tab by adding
one without deleting a test that says why not.

§2.2 says *"no return-series statistics are computed anywhere"*. That is true of the backend and
not quite true of what the browser could do, and this is the note saying so. The right fix is the
statistics job, not a number assembled in a component.

---

## 3. Defects found and fixed while building

1. **A percentage rendered as a fraction, caught from a sibling's note.** `MoneyMoveOut.pct` is a
   stored *fraction*: `portfolio_overview.py` computes `move / base` and quantizes it, so a 0.06%
   day arrives as `-0.000578`. The first version of `detailSnapshot` passed it straight through to
   a component that renders `${pct}%`, which would have printed `-0.000578%`: a figure a thousand
   times too small, which reads as a rounding bug rather than a units bug. PC2 found the identical
   fault in PC1's metric band on the same day. It is now converted at the one seam that builds the
   metric, and `overview-activity.test.tsx` pins `"-0.06"` and `"19.20"`.

2. **A sort control that would have looked dead in development.** `toggleSort` called
   `setDirection` from inside a `setSortKey` updater. React's StrictMode double-invokes updaters
   precisely to surface that, so the direction flipped twice per click and landed back where it
   started — broken in `next dev`, working in production, which is the worst pair of behaviours to
   debug. Fixed, and `holdings.test.tsx` now has a test that renders inside `StrictMode` and
   clicks three times. Baskfy has already shipped one "nothing happens when I click" defect
   (11 Sep, the Add-to-swing button), which is why this got a test rather than a fix.

3. **A drawdown episode dated from the wrong session.** The first version dated each fall from its
   first *down* day. The peak is the last day *at* the high, one session earlier, and the
   difference shows up as every episode being a session short and disagreeing with the server's
   own `max_drawdown.peak_on`. Fixed; the test pins the deepest episode against that field. A fall
   already in progress at the start of the window now reports `peakOn: null` with a sentence,
   rather than dating the high to a session that did not set one.

4. **A scanner that tripped its own check.** The G7 test greps the directory for a write verb and
   lives in the directory it greps. Its patterns are now assembled from pieces at runtime and the
   scan skips `__tests__`, so neither the test nor the gate's own `grep` can report a defect that
   is only the test's own source.

---

## 4. Judgement calls worth knowing about

* **Sixteen columns, six on by default.** All sixteen are real; showing all of them at once is a
  spreadsheet. The picker is the control, and the security column cannot be switched off.
* **Below `md` the table is not rendered at all**, and the same rows render as a stacked list
  sharing one `RowFigures` renderer. PC1 learned this on the comparison table: a sixteen-column
  grid on a 390px screen is a sideways scroll that hides the columns a person came for.
* **A dense cell shows short words, not the full sentence.** "no purchase price" in the cell, the
  whole sentence on `title` and in an `sr-only` span, and every distinct sentence printed once in
  a footnote under the table. A four-line sentence inside a 90px column is not a design, and a
  tooltip alone is invisible to anyone not holding a mouse.
* **Selection adds rows up and copies symbols. It has no fourth action** and no path to a broker.
* **Concentration is reported twice**: the Herfindahl index, which an institution asks for and
  which means nothing on its own, and the count of equally weighted holdings that would score the
  same, which is the version a person can act on.
* **No em dash appears anywhere in this directory**, including in prose, so that the G8 check can
  be a blunt scan of rendered text rather than a judgement call.

---

## 5. Gate ledger

All ten met, evidence in `gates/pc3.md`. 167 tests in
`src/components/portfolio/detail/__tests__/`, nine files.
