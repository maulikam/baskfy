# PC2 findings — the performance workspace

Leaf PC2 of the Portfolio Command Center tree, 11 Sep 2026. Gates: `gates/pc2.md`, 9 of 9.
Files owned and shipped:

| File | What it is |
|---|---|
| `apps/web/src/lib/portfolio/performance.ts` | The pure layer: series, the three views, the read-out, event markers, states, attribution |
| `apps/web/src/components/portfolio/command/performance-workspace.tsx` | The chart, its controls, the brush and the read-out |
| `apps/web/src/components/portfolio/command/attribution.tsx` | The contribution bands and the not-decomposable list |
| `apps/web/src/lib/portfolio/__tests__/performance.test.ts` | 38 tests |
| `apps/web/src/components/portfolio/command/__tests__/performance-workspace.test.tsx` | 31 tests |

---

## 1. The prop contract — what the parent must fetch and pass

The parent mounts **one** component. `Attribution` is rendered by it and needs no wiring.

```tsx
import { PerformanceWorkspace } from "@/components/portfolio/command/performance-workspace";

<PerformanceWorkspace
  chart={overview?.chart ?? null}
  rows={overview?.portfolios ?? []}
  todaysTotal={overview?.hero.todays_pnl.amount ?? null}
  todaysUnavailable={overview?.hero.todays_pnl.unavailable_reason ?? null}
  unallocatedValue={unallocated?.holdings_value ?? null}
  seriesByPortfolio={navByPortfolio}        // optional — see §1.2
  onRangeChange={handleRangeChange}         // optional — see §1.3
  loading={rangeIsInFlight}
/>
```

### 1.1 The props, with types

| Prop | Type | Required | Source |
|---|---|---|---|
| `chart` | `NavSeries \| null` | yes | `OverviewOut.chart`. `null` is "the overview did not load" and renders the empty-series explanation, not an empty frame |
| `rows` | `readonly PortfolioRow[]` | yes | `OverviewOut.portfolios`. **Capital only.** Monitoring views are filtered out again inside `attribution()` as a second guard, but do not pass them: a lens overlaps what it lenses |
| `todaysTotal` | `string \| null \| undefined` | no (default `null`) | `hero.todays_pnl.amount`. The figure the contribution rows reconcile to. Omit it and the panel says there is nothing to reconcile against |
| `todaysUnavailable` | `string \| null \| undefined` | no | `hero.todays_pnl.unavailable_reason`, so a missing total explains itself in the API's own wording |
| `unallocatedValue` | `string \| null \| undefined` | no | `unallocated.holdings_value`. Used only to choose which sentence explains a residual — holdings in no portfolio, versus a position with no previous close |
| `seriesByPortfolio` | `ReadonlyMap<number, NavSeries> \| undefined` | no | See §1.2 |
| `onRangeChange` | `((range: NavRange) => void) \| undefined` | no | See §1.3 |
| `loading` | `boolean \| undefined` | no (default `false`) | True while a range refetch is in flight; the drawn series is still the previous one |

Every money prop is a **decimal string**, never a number — house rule 9, and the workspace does
its arithmetic on scaled integers throughout.

`TooltipProvider` must be in scope. `app/providers.tsx` already mounts it app-wide; only a test
rendering a subtree needs to supply it.

### 1.2 `seriesByPortfolio` — the fetch that makes the period band real, and what happens without it

**Without it, nothing breaks.** The "over the window" band renders no figure and says exactly what
it needs. That is the honest default and the parent may ship it that way.

**With it**, the band becomes real: for each capital portfolio, profit over the window is
`value_last − value_first − Σ net_flow`, which reconciles to the aggregate's own window profit to
the last paisa. To supply it:

```
GET /api/v1/portfolio/{portfolio_id}/nav?range={the same range the overview served}
```
one call per capital portfolio, keyed into a `Map<number, NavSeries>` by `portfolio_id`. The
range **must** match `overview.chart.range`, or the two windows are different questions.

This is N extra requests on a page load and that is a real cost — which is why it is optional
rather than assumed. The alternative was apportioning `hero.total_pnl` by weight, and that is not
a derivation, it is an assumption that every portfolio returned the same thing, printed as a
number. Baskfy places live orders.

### 1.3 `onRangeChange` — and why the pills are not self-serve

The workspace does not fetch. When the parent passes `onRangeChange`, the five servable pills
(1M / 3M / 1Y / 3Y / All) call it and the parent refetches `overview.chart` at the new range.
When it is omitted, the pills are disabled **and the reason is on the screen** ("This screen was
also served a single window…"), because a dim control with no explanation is the dead-primary-
action defect PC1 spent an hour removing.

1D and 1W render **no control at all**, with a visible sentence naming them and the reason. They
are not in `NavRange` — the server's own docstring argues that a member which cannot honestly be
served is worse than a missing one — and `servableRanges()` is typed so a pill physically cannot
ask for one.

### 1.4 Where it belongs on the screen

Below the executive band and the health strip, above (or beside) the comparison table. It is a
`<div>` with `data-testid="performance-workspace"` containing two `<section>`s — the chart panel
and the attribution — and needs no wrapper. It is responsive on its own: the SVGs are `viewBox`-
scaled with `className="w-full"`, the control rows wrap, and the brush drops to one column below
`sm`.

---

## 2. What §2.2 of the plan doc got wrong, in both directions

**This leaf does not edit `docs/PORTFOLIO-COMMAND-CENTER.md`. These are for the parent to fold in.**

### 2.1 §2.1 overstates what the NAV series carries — the invested line does not exist

§2.1 lists:

> | Portfolio value over time, invested line, benchmark line | `NavSeriesOut.points[]` (`nav`,`invested`,`cash`,`benchmark_value`) |

Those four field names belong to a **different schema**. `baskfy_api__routers__desk__NavPointOut`
has `nav`, `invested`, `benchmark_value` and `index_value` — it is the *desk's* series. The
portfolio overview's `baskfy_api__routers__portfolio_overview__NavPointOut`, which is what
`NavSeriesOut.points[]` actually holds, carries exactly `on`, `value`, `cash`, `net_flow` and
`pending_reconciliation`. **There is no `invested` on it, and no per-day cost-basis series
anywhere in Baskfy** — `hero.invested` is one scalar for today.

So the brief's "invested capital line" as literally asked for is a §2.2 item, not a §2.1 one.

What is exactly derivable is the **capital line**: the window's opening value carried forward by
`net_flow`. The chart draws that, labels it "Capital in play", and its tooltip says in so many
words that it is not cost basis. It is the line that makes the chart readable — the gap between
it and the value line is market movement with transfers removed — and a deposit lifts both lines
by the same rupee, which is the property the test holds.

**Suggested §2.2 row:**
> | A cost-basis line on the chart | `NavPointOut` (portfolio overview) carries no `invested`; only a single scalar for today exists | A daily cost-basis series on `portfolio_nav_daily` |

### 2.2 §2.2 is right about attribution but misses cash drag, and it looks computable

§2.2 does not list cash drag, and somebody will try to build it, because every NAV mark carries
`cash` so the cash *weight* is known day by day. It is still blocked: cash drag is the difference
between the total return and the equity-only return, and the equity-only return cannot be
computed because `net_flow` records money entering and leaving the **account** and never money
moving between cash and shares **inside** it. A buy changes no flow. The figure would have to be
guessed. The reason is carried in `NOT_DECOMPOSABLE` and asserted by a test.

**Suggested §2.2 row:**
> | Cash drag | `net_flow` is external flow only, so the equity-only return cannot be separated from the total | Recording internal buys and sells as transfers on the NAV series |

### 2.3 §2.2 is right about "risk contribution", and PC3's amendment extends to this level too

PC3 (§6.3) found `DetailHoldingOut.todays_contribution` / `total_contribution`, so *return*
contribution by holding is real **inside a portfolio**. At the aggregate level it still is not —
the overview payload stops at the portfolio row and fetches no holdings. The workspace names that
as a blocked effect whose "Needs:" line reads *"Nothing. Open a portfolio; the figure is already
there."*, which is the useful form of an unavailable.

### 2.4 A unit hazard that is not in the doc at all, and PC1 is currently rendering it wrong

**Every rate the API sends is a ratio, not a percentage.** `portfolio_nav._quantise_return`
quantises to `RETURN_PRECISION`, so a 1.99% day arrives as `0.019900`. This holds for
`MoneyMoveOut.pct`, `DayPnlOut.pct`, `LabelledRateOut.value`, `DrawdownPointOut.drawdown`,
`MaxDrawdownOut.drawdown` and `BenchmarkOut.difference` alike. `overview.formatRate` knows this
and multiplies by 100; `lib/portfolio/performance.ts` does the same conversion in exactly one
place, `ratioAsPercent`, on scaled integers.

**`components/portfolio/command/metric-band.tsx` (PC1) does not.** `MetricCell` renders
`{metric.value}%` and `{metric.pct}%` raw. On a real payload, a 1.99% day shows as `0.0199%`, an
18.7% TWR shows as `0.187%`, and an 8.2% drawdown shows as `0.082%`. PC1's own tests do not catch
it because its fixtures use `"1.99"`, `"18.7"` and `"-8.2"` — percentages the server never sends —
which is the same class of defect PC1 itself recorded as its third finding: *"fixtures that lied
about the payload"*, caught in the shape of the object and missed in the units of the values.

**PC2 does not touch that file** (not owned). It works around it by converting to percent in the
pure layer *before* handing a `Metric` to `MetricCell`, so every PC2 figure is correct today. Two
things follow for the parent:

1. `metric-band.tsx` needs the fix — either `MetricCell` converts, or every caller does. PC2 has
   already done the latter, so if `MetricCell` starts converting, **PC2's percent metrics will be
   multiplied twice**. Coordinate the change; do not do it silently.
2. PC1's snapshot fixtures should carry ratios (`"0.0199"`, `"0.187"`, `"-0.082"`), which is what
   would have failed the assertions and surfaced this.

This is worth recording in `docs/DECISIONS-MERGE.md` rather than only here: it is a wrong number
on the most-read surface of the screen.

---

## 3. `CombinedChart` — what it would need to become the one shared chart

`components/portfolio/combined-chart.tsx` was read, its reasoning inherited, and it was **not
edited** (not owned, and it is still mounted on the existing `/portfolio` screen, so a change
there changes that screen too). PC2 reuses the same locked visx stack — `@visx/curve`,
`@visx/scale`, `@visx/shape` — and installs nothing new.

Its two best decisions are carried over verbatim in reasoning: the return view is the **wealth
index** and never a percentage of the value line, and the benchmark is **rebased to the window's
left edge** rather than given a second axis.

For whoever consolidates the two, `CombinedChart` would need:

1. **A cursor.** It has no read-out at all. This is the largest change: state, a pointer handler,
   a keyboard-reachable equivalent, and a panel.
2. **A brush.** Its range control refetches; it cannot narrow the window it already has.
3. **A third line.** No capital / flow-adjusted baseline, because no `net_flow` is read.
4. **Direct labels.** It uses a figcaption key; the brief asks for labels on the lines.
5. **Event markers.** `net_flow`, `pending_reconciliation` and the max-drawdown peak/trough are
   all in the payload it already receives and none is drawn.
6. **A third view.** Drawdown is an optional panel beneath rather than a peer of value and return.
7. **Ratio-to-percent on the axis.** It formats the return axis with `formatNumber(tick)` where
   the underlying values came from a wealth index — correct as written, but the same care is
   needed anywhere `formatRate` is not used.

None of these is a defect in `CombinedChart`; it is a smaller component answering a smaller
question. The consolidation is a real piece of work, not a merge.

---

## 4. Decisions taken under the autonomy charter

1. **Total-P&L attribution is computed from per-portfolio NAV series, not apportioned by weight.**
   `gates/pc2.md` G5 asks for "share of ... total P&L". `PortfolioRowOut` has no cost basis and no
   total-P&L field, so the literal reading had two honest endings: a reasoned unavailable, or a
   real computation from a series the parent can fetch. The second was taken because it is exact,
   testable and reversible (the prop is optional). Rejected: `hero.total_pnl × weight`, which
   looks like an answer and is an assumption.
2. **The read-out is state with three inputs, not a hover.** A panel that exists only under a
   mouse pointer does not exist on a phone or for a keyboard. Pointer, a labelled range input, and
   the brush all move the same cursor. This is also what makes the gate testable off a browser.
3. **The share bar is a share of the gross move.** Against a net denominator, a day of
   +₹10,000 and −₹9,000 reads 1,000% and −900%. Gross is the only denominator comparable on every
   day, the label says gross, and when the signs are mixed the panel says that too.
4. **1D and 1W render no control.** The gate says "never a dead pill". A pill that is merely dim
   is a dead pill; the honest form is the name, the absence, and the reason in a sentence.
5. **The em-dash rule is scoped to figure-bearing regions.** "Never a bare —" means no dash stands
   where a value would, not that the character is banned from prose, where it is punctuation. The
   test scans the headline strip, the read-out and the attribution rows — exactly the regions
   where `formatRupees` could return `NO_FIGURE` — and additionally asserts every "Not available"
   is accompanied by its reason.

---

## 5. Defects found and fixed inside this leaf

1. **The day's change rendered as a percentage.** `ReadoutPanel` chose rupees-or-percent from the
   cell's *label*, so ₹6,000 rendered as "6000%". Units are now declared per cell, with a comment
   saying why the heuristic is gone. Found by the G3 test on its first run.
2. **Fixtures that `exactOptionalPropertyTypes` correctly rejected.** `{ benchmark: undefined }`
   is not the same type as a payload with no `benchmark` key, and the API sends the latter. A
   `stripped()` helper deletes the key instead. Four `tsc` errors, no cast used to silence any.
3. **Banned vocabulary.** `src/lib/__tests__/no-jargon.test.ts` enforces `PORTFOLIO_REDESIGN.md`
   §8, and "Book" is on its left column. All three PC2 files used it freely in visible strings —
   nine occurrences — and all nine are gone. This rule is not mentioned in
   `docs/PORTFOLIO-COMMAND-CENTER.md` §6.2 and probably should be: four of the five parallel
   leaves tripped it.
4. **A gate that was passing for the wrong reason would have been impossible to spot later.** G1's
   `grep -E "window\."` matched the English word ten times over. Rather than widen the pattern,
   the prose now says "period" or "stretch of days" at a full stop, with a comment at the top of
   the module recording why — so the check keeps testing what it claims to test.
