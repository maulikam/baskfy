# The Portfolio Command Center — design tokens and component specification

**Output requirement 8 of the brief, 11 Sep 2026:** *"Reusable component and design-token
specifications."* This file is that, written so a second contributor can build a new panel for
this screen without reading the six leaves that built the first ones.

It is a **specification of what exists**, not a wish list. Every token below is declared in
`decile-blueprint/apps/web/src/app/globals.css` in both themes; every component below is in the
tree with tests. When the two disagree, the code is right and this file is the stale half — the
root `CLAUDE.md` rule *"when the code and a doc disagree, the DECISION wins"* governs here too.

---

## 1. The one rule the palette encodes

Colour on this surface carries **meaning**, and structure is carried by ink, hairlines and space.
That gives three families that never borrow from each other:

| Family | Means | Tokens |
|---|---|---|
| **Brand** | "this is Baskfy" and "this is the primary action" | `--brand`, `--brand-foreground`, `--brand-strong`, `--brand-muted`, `--brand-border` |
| **Meaning** | gain, loss, caution | `--positive`, `--negative`, `--warning` and their `-muted` surfaces |
| **Informational** | benchmark, explanation, a lens rather than a holding | `--info`, `--info-muted` |

Orange is never gain, loss or severity. Green and red are never decoration. This is the brief's
own instruction (*"Green and red only for gains, losses and risk status"*, *"Blue or violet may be
used sparingly for benchmarks"*) and it is asserted by `GATES.md` G2, which fails if
`--positive`, `--negative` or `--warning` is ever set to the brand hex.

**And none of the three is ever the only signal.** Every tone on this screen carries an icon, a
glyph or a word as well — `GATES.md` G11. A red number reads as a loss to someone who cannot
distinguish it from the green one only because it also carries a sign and a label.

---

## 2. Semantic colour tokens, both themes

Contrast figures are measured against the surface each token is actually used on — `--card` for
text tokens, `--brand` for `--brand-foreground`.

### Surfaces and ink

| Token | Light | Dark | Use |
|---|---|---|---|
| `--background` | `#f8fafc` | `#0a0a0a` | the page canvas, a warm off-white as the brief asks |
| `--card` | `#ffffff` | `#141414` | a panel lifted off the canvas |
| `--popover` | `#ffffff` | `#141414` | menus, tooltips, drawers |
| `--muted` | `#f0f0f0` | `#1a1a1a` | a sunken row, a filled chip |
| `--foreground` | `#0a0a0a` | `#f7f7f7` | primary text — deep charcoal, per the brief |
| `--muted-foreground` | `#6f6f6f` | `#a6a6a6` | secondary text, labels, units |
| `--border` | `#e8e8e8` | `#262626` | the 1px hairline that does most of the structural work |
| `--input` | `#d6d6d6` | `#333333` | a control's own edge, one step stronger than `--border` |
| `--ring` | `#0a0a0a` | `#f7f7f7` | focus. Never removed, never faint |
| `--accent` | `#0a0a0a` | `#f7f7f7` | the ink accent: active nav, selected row |
| `--accent-foreground` | `#ffffff` | `#0a0a0a` | on `--accent` |

Dark's `--muted-foreground` is `#a6a6a6` rather than `#8a8a8a` for a measured reason: `#8a8a8a`
clears 4.5:1 on the canvas but only 5.34:1 on a panel, and this screen puts secondary text on
both.

### Brand

| Token | Light | Dark | Use |
|---|---|---|---|
| `--brand` | `#fe7510` | `#fe7510` | the primary action's fill, brand marks. Identical in both themes |
| `--brand-foreground` | `#0a0a0a` | `#0a0a0a` | text **on** `--brand`. **7.32:1 — AAA** |
| `--brand-strong` | `#9a3412` | `#fdba74` | orange **text** on a neutral surface. 6.98:1 light, 11.37:1 dark |
| `--brand-muted` | `#fff3e9` | `#2a1708` | a brand-tinted surface |
| `--brand-border` | `#fcd2ad` | `#5c3210` | a brand-tinted hairline |

**The trap this pairing avoids.** White on `#fe7510` is 2.70:1 and fails AA outright. The reflex
to put white on a coloured button is what would have forced a muddy brown-orange; the pairing was
wrong, not the colour. Charcoal on the mark's own orange is AAA. And `--brand` is never used for
text on `--background` — that is `--brand-strong`'s entire job, which is why it is a token and not
a hex in a component.

### Meaning and information

| Token | Light | Dark | Use |
|---|---|---|---|
| `--positive` / `--positive-muted` | `#1a7f37` / `#ebf8f2` | `#4ecb71` / `#102a1a` | a gain, a healthy status |
| `--negative` / `--negative-muted` | `#a81f14` / `#fef0ef` | `#ff7b72` / `#33161a` | a loss, a failure |
| `--warning` / `--warning-muted` | `#8a6100` / `#fffaeb` | `#e3b341` / `#2b2210` | needs attention, stale |
| `--info` / `--info-muted` | `#4338ca` / `#eef0fd` | `#a5b4fc` / `#1c1f3a` | benchmark series, a monitoring view, an explanation. 7.90:1 on card |

---

## 3. Form, density and motion

| Token | Value | Note |
|---|---|---|
| `--radius` | `0.875rem` = **14px** | the brief's 10–14px band, at its top |
| `--row-height` | `34px` | the default table row |
| `--row-height-compact` | `28px` | a dense sub-table inside an expanded row |
| `--v-elev-1` … `--v-elev-4` | 1px hairline shadow → 48px drawer shadow | restrained; a panel usually gets `1` or none, a drawer gets `3`–`4` |
| `--v-dur-instant` / `-fast` / `-base` | `80ms` / `120ms` / `180ms` | **every interaction transition on this screen uses one of these three.** The brief asks for under 200ms and these are the only values that qualify |
| `--v-dur-slow` / `-slower` | `280ms` / `440ms` | page-level and marketing motion. Not for a filter, a hover or a drawer on this screen |
| `--v-ease-out` | `cubic-bezier(0.16, 1, 0.3, 1)` | the default |
| `--v-tracking-caps` | `0.08em` | the small-caps eyebrow that labels a metric group |

**Type.** Inter, via the app shell. Financial figures use tabular numerals without exception —
`font-variant-numeric: tabular-nums` — because a column of rupee amounts that does not align is a
column you cannot scan. The ramp is `--v-text-11` through `--v-text-56`; this screen lives between
11 and 24, with 32 reserved for net worth.

**Motion is used for four things only**, per the brief: a number updating, a chart transition, a
drawer opening, and a filter changing. Nothing on this screen animates on load.

---

## 4. The primitive every figure passes through

`src/lib/portfolio/command-center.ts` exports `Metric` and `metric()`. **It is not optional and it
is not a convenience.**

```ts
interface Metric {
  value: string | null;        // decimal string, exactly as the API sent it
  unavailable: string | null;  // non-null whenever value is null — the type is the contract
  label: string;               // always shown beside the figure
  definition: string;          // one sentence on how it is calculated, for the tooltip
  pct?: string | null;
  since?: string | null;
}
```

A component cannot render a figure without also holding the explanation for its absence. That is
what makes the brief's rule — *never display "—" without explaining why the value is unavailable*
— structural rather than a thing to remember. `metric()` supplies a truthful fallback reason
rather than allowing a bare dash to escape, and it is tested directly because the integration test
over `commandCenter()` could not reach that branch.

**Money is a decimal string end to end.** House rule 9: money and prices are `numeric`, never
`float`. A figure is parsed for comparison and formatting and is never round-tripped through a
JavaScript number on its way to the screen.

---

## 5. Components

*(Filled in by the integration node once PC2–PC6 land; PC1's are below.)*

### `CommandHeader`
Title, supporting line, the **Capital portfolios | Monitoring views** switch, `Add portfolio`
(secondary) and `Review rebalance` (primary, `--brand`).

*States:* rebalance enabled with one target (links straight to it), enabled with several (opens a
menu naming them), disabled (renders its reason beside the control rather than a silent grey
button). **No control in this header is ever drawn without a destination** — a dead primary action
shipped here once and four tests now hold it.

### `HealthStrip`
The operational line: portfolio and view counts, unique holdings, brokers, price age, last
reconcile, and each named problem. `status` is one of `live` / `stale` / `action-required`, and
each is a chip carrying a word as well as a tone.

*Rule:* a problem names its subject. "One broker holding does not reconcile, in Swing Manual" is a
problem; "Some data may be out of date" is a generic warning and is the thing this component
exists to prevent.

### `MetricBand`
The executive snapshot as one connected band, not a row of isolated cards. Net worth, invested,
cash, unallocated, today's P&L, unrealised, realised, XIRR, TWR, drawdown, peak. Each cell is a
`Metric`, so each carries its own label, its definition on hover, and its reason when absent.

### `ComparisonTable`
One sortable, expandable table replacing the repeated portfolio blocks. Per row: a colour
identifier **and a non-colour cue**, a sparkline, status badges, an expand chevron. Expanding
reveals top holdings, recent activity, contributor and detractor, warnings and the next review.

*Responsive:* below `md` the table is **not rendered at all**. The same rows render as a stacked
list sharing one `RowDetail` renderer, so the phone cannot quietly drift into showing less than
the desktop. A ten-column grid inside a horizontal scroll hides the columns a person opened the
screen for.

### `AttentionRail`
The "Needs attention" panel, ordered by impact: critical, review today, upcoming, informational.
Each item states what changed, why it matters, which portfolio, the suggested next step, and when
it was detected.

*Rule:* never phrased as investment advice — Baskfy is not registered to give it (D3). "Three
holdings contribute 61% of this portfolio's value" is an observation; "reduce your position" is
advice and does not appear.

*Detection time:* Baskfy has no alert table, so there is no moment at which a problem was raised.
The honest answer is the pass that would have seen it — the last holdings reconcile — and when
even that is undated the sentence says so rather than omitting the field and letting a reader
assume the alert is fresh.

### `Explain`
The shared empty/error surface. A title, a sentence that says what happened, and **exactly one**
next action. Every state the brief names routes through it.

### `PortfolioSelector` · `BenchmarkStatement` · `BaseCurrency` · `OverflowMenu`
The header's scope controls, added at integration.

*The rule all four obey:* **a control either does something or says why it cannot.** None is drawn
greyed-out and silent.

* **PortfolioSelector** — navigation, not a filter. Choosing a portfolio *opens* its workspace;
  narrowing this screen to one row would leave a person wondering what happened to the rest. With
  nothing filed it says "No portfolios yet" and why, rather than sitting disabled.
* **BenchmarkStatement** — a statement, not a switch. `/portfolio/overview` takes no parameters,
  so a page-level selector could not re-measure anything; it names the index the comparison
  actually ran against and says where it is changed.
* **BaseCurrency** — `INR`, with the reason there is no other. A dropdown with one option implies
  a second exists.
* **OverflowMenu** — export (real), manage and reconcile (real routes), and two named absences:
  a consolidated-statement import, and a shareable read-only report. Each carries what it would
  take. **The date range is deliberately not here**: it belongs beside the chart it scopes, and on
  this screen the endpoint serves one window, which the workspace's own pills say.

### `PerformanceWorkspace` · `Attribution` (PC2)
The central visual. Value / return / drawdown over one series, an invested-capital line, the
benchmark, a drawdown overlay, event markers, a hover read-out and a brush.

*Two rules in the arithmetic, not the markup:* the return view is the **wealth index**, never a
percentage of the value line — a deposit would otherwise be credited to the strategy. And every
line carries a **direct label and a named stroke** (solid / dotted / dashed), so three lines stay
three lines in greyscale.

Attribution below it says which portfolio moved the number, reconciling to the aggregate exactly
with any residual named rather than folded into the largest row. Each row links to the portfolio
it names. The effects Baskfy cannot decompose — sector, allocation effect, selection effect, cash
drag, fees — are listed with a reason and a "Needs:" line, never beside a digit.

### `RegimePanel` (PC5)
Which tier is applied, what exposure it targets, what the actual exposure is, the gap in
percentage points with its direction **in words**, and the desk's own recorded reasons printed
verbatim under a heading saying whose words they are.

*Three rules:* **R2 is never an exit** — reduced exposure, possibly half-sized entries. The live
cap comes from the API and the ladder is shown only as the desk's configured defaults. A stale
evaluation, a manual-action flag or an unreachable desk changes the panel's **heading** to "Last
recorded stance — not confirmed current", and an unreachable desk shows **no tier at all**.

### `PortfolioDetailWorkspace` (PC3)
Eight tabs over one portfolio, with a roving tabindex and one panel at a time. The holdings table
is the institutional one: sixteen real columns, sorting with rows that have no figure sinking in
both directions, ten quick filters, a sticky header and sticky first column, a column picker, and
a selection toolbar that sums exactly and **places no order**.

*The tab that matters most is Risk*, because it is the one most likely to be filled with plausible
numbers: nine readings that are real, ten named as not-yet-measured with what each would take, and
a test asserting the blocked section contains no `%`, no `₹` and no dash at all.

### `RebalanceDrawer` (PC4)
Five steps — analyse, adjust, impact, confirm, plan — ending in a document, never a submitted
trade.

*The load-bearing invariant is negative and is asserted as such:* **there is no rupee sign anywhere
in the drawer.** Every money figure in this app renders through `formatRupees`, which always
prefixes one, so no `₹` means no quantity, cash figure, turnover or cost was derived. An excluded
name's slice is left **unassigned** rather than re-spread, because re-normalising would be the
preview inventing an allocation nobody proposed.

### `ManagePortfoliosDrawer` (PC6)
Eight panels: create, rename, assign, move, monitoring view, sub-portfolios, broker, delete.
Endpoint paths are typed `keyof paths` from the generated OpenAPI document, so an action naming a
route the API does not serve is a **compile error**.

*Undo is offered for exactly one write.* A move is exactly reversible — the same request with the
portfolios swapped — and it is offered on the confirmation. A rename is reversed by renaming. A
**delete is not reversible at all**, so it is not offered one: the NAV series, the return since
`started_on` and the dated flows cascade, and an Undo button beside that would contradict the
panel that just said so.

---

## 6. Two contracts a new panel must not break

**Units.** A rate on a `Metric` is a **percentage**. The API stores fractions and the conversion
happens once, in the pure layer, on scaled integers. A renderer formats and never scales. This is
not a style note: when a component scaled instead, two panels reading the same field disagreed by
a factor of a hundred, and forty-five tests were green over it because the fixtures used values
the server never sends.

**Ownership.** Pure arithmetic lives in `lib/portfolio/*`, is tested against fixtures, and touches
no clock, no network and no `window`. A component receives the payload and calls one function.
`RegimePanel` takes the raw `RegimeOut` and calls `regimeReading()`; `CommandCenterScreen` takes
the raw `Overview` and calls `commandCenter()`. A new panel should be able to say the same
sentence about itself.
