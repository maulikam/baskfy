# Portfolio Command Center — the plan, and what the data will and will not support

**Brief, 11 Sep 2026 (Maulik).** *"Redesign Basqfy's Portfolios screen completely ... a premium,
modern financial-intelligence workspace comparable in quality and information density to Linear,
Stripe, Ramp and institutional portfolio terminals — while remaining approachable for retail
investors."*

The brief is long and specific. This file is the honest reading of it against the product that
actually exists: what is buildable now, what is buildable but large, and what cannot be built at
all because the number does not exist anywhere in Baskfy. The last category is the important one,
and it is not small.

---

## 1. The rule that governs every decision below

The brief supplies its own discipline, and it is the right one:

> **Never display "—" without explaining why the value is unavailable.**

Its twin, from this repo's own house rules: **absent is not zero**. Between them they decide every
ambiguous case in this document. A metric Baskfy cannot compute is not rendered as a dash, not
rendered as a plausible number, and not quietly dropped from the design. It is rendered as
*unavailable, with the reason*, or its panel is absent with the reason recorded here.

This matters more than usual because of what this product is. Baskfy places real orders with real
money — `BASKFY_SWING_AUTO_EXECUTE=true` on the live box since 7 Sep. A Value-at-Risk figure
invented to fill a panel is not a design shortcut; it is a number a person may size a position
against. **Nothing in this build fabricates a financial metric.**

---

## 2. What the data actually supports

Surveyed against `packages/api-client/src/generated/schema.ts` and the live box, 11 Sep 2026.

### 2.1 Real today — every figure below has an API field behind it

| Brief asks for | Source |
|---|---|
| Total net worth | `HeroOut.current_value` |
| Invested value, **and why it is missing** | `hero.invested`, `hero.invested_unavailable_reason` |
| Cash, unallocated capital | `hero.cash`, `UnallocatedOut.{cash,holdings_value,total_value}` |
| Today's P&L, amount and percent | `hero.todays_pnl` (`MoneyMoveOut.amount/pct`) |
| Total, unrealised and realised P&L | `hero.total_pnl`, `hero.secondary.{unrealised_pnl,realised_pnl}` |
| Dividends | `hero.secondary.dividends` |
| XIRR and TWR, each labelled | `hero.xirr`, `hero.twr` (`LabelledRateOut` carries `kind`, `label`, `since`, `unavailable_reason`) |
| Current drawdown, peak value, trough | `NavSeriesOut.drawdown[]` (`drawdown`,`peak`,`on`), `max_drawdown` (`drawdown`,`peak_on`,`trough_on`) |
| Portfolio value over time, invested line, benchmark line | `NavSeriesOut.points[]` (`nav`,`invested`,`cash`,`benchmark_value`) |
| Benchmark-relative return | `NavSeriesOut.benchmark` (`portfolio`, `benchmark`, `difference`) |
| Daily P&L series | `NavSeriesOut.daily_pnl[]` |
| Per portfolio: value, cash, today's P&L, holdings count, brokers, kind, source, benchmark name, reconciliation state | `PortfolioRowOut` |
| Per portfolio labelled return, and the model's own | `row.headline_return`, `row.model_return` |
| Holdings: quantity, price, price age, value, cost basis, first bought, allocation slices, monitoring views, split flag | `AggregatedHoldingOut`, `HoldingBrokerLineOut` |
| Data health: price age, holdings sync age, per-broker sync, unpriced instruments, unallocated count | `HoldingsOut.{prices_as_of,prices_label,holdings_synced_on,holdings_synced_label,unpriced_instrument_ids,unallocated_count}`, `OverviewOut.sync_status[]` |
| Needs-attention items | `OverviewOut.attention[]` (`kind`,`message`,`count`,`since`,`subject_ids`) |
| Market regime, equity exposure vs cap | `/api/v1/swing/market`, `/api/v1/swing/config` (`equity_cap_pct`, `applied_regime_cap`, `capital`, `deployed`, `exposure_level`) |
| Rebalance proposal | `POST /portfolios/{id}/rebalance` |

### 2.2 Not real — no source anywhere in Baskfy

These are named in the brief and **will not be invented**. Each line says what it would take.

| Brief asks for | Why it cannot be shown | What would unblock it |
|---|---|---|
| Sector, industry, market-cap, geography allocation | There is no sector column on `instrument`. The grouping-suggestions panel says so to the user's face today: *"no sectors"* | A sector/industry map on `instrument`, sourced and kept current |
| Beta, annualised volatility, Sharpe, Sortino, downside deviation | No return-series statistics are computed anywhere | A statistics job over `portfolio_nav_daily` + a benchmark series |
| Value at Risk, Conditional VaR, stress tests at −5/−10/−20% | Same, plus a stated model and its assumptions | The above, and a documented model — a VaR with an unstated model is worse than none |
| Correlation matrix | Needs per-holding return series aligned on dates | A factor/returns store |
| Target weights, allocation drift, over/underweight, rebalance drift | **Baskfy has no notion of a target weight for a manually grouped portfolio.** A rebalance is computed against a *screen*, not a stored target | A target-allocation model per portfolio (a real product decision, not a UI one) |
| Risk contribution by security or portfolio | Requires covariance, i.e. the volatility work above | As above |
| 52-week-high distance, 20/50/200-DMA status, momentum score/rank | Exist only for names the **swing** book watches, not for an arbitrary holding | Extend the factor pass to every held instrument |
| Liquidity status, days to liquidate | No ADV/turnover per instrument is stored | A liquidity column on the daily bars |
| Brokerage, taxes, charges | `packages/core` has a cost model for the *weekly rebalancer*, not for arbitrary portfolios | Wire the existing cost model through the portfolio rebalance path |
| Corporate actions pending verification | `corporate_action` rows exist; nothing marks one "pending verification" | A review state on the table |
| Tax-lot / holding-period considerations | `first_bought_on` exists but is NULL for broker-synced rows until a CAS import | CAS import (already a known Phase-3 item, §5.3) |
| Permissions, ownership, audit history | Single-tenant today; P4.2/P4.10 are unshipped | Multi-tenant work, gated on C3 |

> ⚠️ **§2.2 has been corrected by the leaves that built against it — read §7 before relying on a
> row above.** Five of these entries were wrong in one direction or the other, and one row in
> §2.1 describes a field the API does not send at all.

**Design consequence.** Sections whose every metric falls in 2.2 are not built as empty shells.
The Risk tab is the clearest case: of its eighteen requested figures, two exist. It is therefore
scoped as one honest panel — drawdown, concentration and the regime — with the rest listed as
what it will show once the statistics job exists, rather than eighteen dashes.

---

## 3. Decomposition

Depth 4. Leaf **PC1** is this session; the rest are declared here so nothing is silently dropped.

```
Portfolio Command Center
├── PC1  Aggregate command screen            ← this session, GATES.md
│   ├── tokens + primitives
│   ├── command header + mode switch
│   ├── data-health strip
│   ├── executive snapshot band
│   ├── comparison table
│   ├── intelligence rail
│   ├── monitoring-views mode
│   └── states, responsive, dark, a11y
├── PC2  Performance workspace                 chart is reusable (`CombinedChart` already has
│                                              value/return/drawdown, benchmark and drawdown
│                                              overlay); attribution BY PORTFOLIO is real, by
│                                              sector is 2.2-blocked
├── PC3  Detail workspace tabs                 Overview/Holdings/Performance/Allocation real in
│                                              part; Risk mostly 2.2-blocked
├── PC4  Rebalance preview drawer              route exists; costs and tax lots 2.2-blocked
├── PC5  Regime panel                          fully real, distinctive, high value
└── PC6  Management drawer                     create/rename/assign/move real; permissions and
                                               audit are multi-tenant, gated on C3
```

### Four header controls the brief names, and why PC1 does not draw them

The brief's header list includes a **date-range selector** (1D…All), a **benchmark selector**, a
**base-currency** control and an **overflow menu** for import/export/settings/reconciliation.
PC1 ships none of them, and that is a decision rather than an omission:

| Control | Why not here |
|---|---|
| Date range | It scopes a chart, and the chart is PC2. On a screen with no chart the control changes nothing a reader can see — a dropdown that does nothing is the exact defect this leaf spent an hour removing from the primary action. It ships **with** the chart. |
| Benchmark | Same: a benchmark is a line on a chart and a column of relative return. `PortfolioRowOut.benchmark_name` exists per portfolio and is shown in the expanded row; a page-level *selector* implies re-measuring against a different index, which nothing behind this screen can do yet. |
| Base currency | Baskfy is India-only equities and every figure is rupees. A currency selector with one option is furniture. It belongs with multi-currency, which is not on any roadmap here. |
| Overflow menu | Import, export, settings and reconciliation are PC6's drawer and the existing activity screen. Duplicating the entry points before the drawer exists means two places to change. |

### Why PC1 first

It is the screen the brief names, it is the only leaf whose every element has data, and it is
where nine of the brief's ten questions get answered. PC2–PC6 are drill-downs from it.

---

## 4. Visual direction — the one deliberate token change

The brief asks for **"Basqfy orange as the primary brand accent"**. Today `--brand` and `--accent`
are both `#0a0a0a`, charcoal. The logo is orange; the interface is not. That is a real gap and PC1
closes it by adding a semantic orange brand token in both themes, used for the primary action and
for brand marks only — never for gain, loss or severity, which keep green/red, and never as a
decorative wash.

Everything else the brief asks for is already true and is kept: Inter, 14px radius (`--radius:
0.875rem`), 1px borders, layered neutral surfaces, tabular numerals, restrained shadows, and a
palette whose own opening comment reads *"positive/negative, rank emphasis, and one accent"*.

One addition: an **info/benchmark** token (blue-violet, used sparingly) because the brief wants
benchmark and informational states distinguishable from both brand and P&L.

---

## 5. Status log

*(append-only)*

- **11 Sep 2026** — Data survey complete; §2.2 established. PC1 gates written to `GATES.md`.
- **11 Sep 2026** — PC1 built: `lib/portfolio/command-center.ts` (the pure layer) plus
  `components/portfolio/command/` — metric band, health strip, command header with the
  Capital/Views switch, comparison table, attention rail, and the screen that assembles them.
  Wired into `/portfolio/portfolios` above the existing grouping forest, which moves into a
  `<details>` until PC6 gives it a drawer.
- **11 Sep 2026 — three defects found while gating PC1, each worth recording because none was
  visible from the unit suite alone:**
  1. *Dead primary action.* `Review rebalance` and `Add portfolio` were wired to optional
     callbacks the page never passed, so both swallowed the click. A rebalance is prepared for
     one portfolio, so the button now links straight to it when there is one, opens a menu naming
     them when there are several, and is disabled beside its reason when there are none. Baskfy
     has already shipped this class of defect once (11 Sep, "nothing is happening when i click
     button add to swing"), which is why there are now four tests holding it.
  2. *The desktop table on a phone.* A ten-column grid inside `overflow-x-auto` is a sideways
     scroll that hides the columns a person opened the screen for. Below `md` the table is not
     rendered at all; the same rows render as a stacked list, sharing ONE `RowDetail` renderer so
     the phone cannot quietly drift into showing less. Below `xl` the attention rail moves above
     the comparison rather than below it — on a phone the question is "does anything need me",
     not "sort ten columns".
  3. *Fixtures that lied about the payload.* Both test files asserted against objects cast with
     `as unknown as Overview`, and three fields in them (`hero.broker_count`, `hero.cash`,
     `hero.dividends`) do not exist in `OverviewOut` at all — the real fields live under
     `hero.secondary`. The casts are gone and the fixtures are schema-exact, so `tsc` now fails
     when a fixture describes a payload the API does not send. House rule 2.
- **11 Sep 2026** — `e2e/command-center.spec.ts` added for the two requirements jsdom cannot
  see: axe in light AND dark (G15), and the responsive collapse (G16).
- **11 Sep 2026 — the browser suite had been dead for six days, and nothing said so.** Running
  G15 revealed that `e2e/auth.setup.ts` still clicked a "Password" tab that M46 (`eccef8d`,
  5 Sep) removed when Google sign-in replaced the password. A failed `setup` project reports as
  *"N did not run"*, not as N failures, so the whole Playwright suite had been failing silently:
  `e2e/nav.spec.ts` was run unchanged here and fails identically, which is how it was confirmed
  as pre-existing rather than caused by PC1. The setup now MINTS the session Auth.js would have
  written — the same `mintAccessToken` the app uses, sealed with the throwaway `AUTH_SECRET`
  `playwright.config.ts` already hands both servers — because there is no honest way back to a
  form when the form belongs to Google. The specs that sign in with a password inside the test
  itself (`account.spec.ts`, `auth-gate.spec.ts`) are still broken by M46 and are NOT fixed here.

---

## 6. PC2–PC6 — the contracts the leaves are built against (11 Sep 2026)

PC1 shipped as `fae98ac`. This section is the integration contract for the remaining five leaves,
written before any of them started so that five parallel sessions cannot disagree about a seam.

### 6.1 File ownership — no leaf edits another leaf's file

| Leaf | Owns, exclusively |
|---|---|
| PC2 | `lib/portfolio/performance.ts`, `components/portfolio/command/performance-workspace.tsx`, `components/portfolio/command/attribution.tsx`, their tests |
| PC3 | `lib/portfolio/detail-tabs.ts`, `components/portfolio/detail/*` (new dir), their tests |
| PC4 | `lib/portfolio/rebalance-preview.ts`, `components/portfolio/rebalance/*` (new dir), their tests |
| PC5 | `lib/portfolio/regime.ts`, `components/portfolio/command/regime-panel.tsx`, their tests |
| PC6 | `lib/portfolio/manage.ts`, `components/portfolio/manage/*` (new dir), their tests |
| **Parent** | `command-center-screen.tsx`, every `page.tsx`, `detail-fetch.ts`, `fetch.ts`, `e2e/command-center.spec.ts`, this doc |

A leaf that needs a page wired states the prop contract; the parent wires it. This is why the
five can run at once.

### 6.2 Rules every leaf inherits

1. **Never a bare "—".** Every figure goes through `Metric` / `metric()` in
   `lib/portfolio/command-center.ts`. A missing value carries its reason or it does not render.
2. **Never fabricate a financial number.** §2.2 is the list; a leaf that finds a new blocked
   metric adds it there rather than deriving something plausible.
3. **Never colour alone** — every tone carries an icon, a glyph or a word.
4. **Never investment advice.** Baskfy is not registered to give it (D3).
5. **Never an order.** Every path ends in a plan a person takes to their broker.
6. Schema-exact fixtures, no `as unknown as`, no `any`, no `type: ignore`.

### 6.3 What each leaf found that §2.2 did not know

* **PC3.** `DetailHoldingOut` carries `todays_contribution`, `total_contribution` and `weight`.
  Contribution-by-holding is therefore **real at the portfolio level**, though not at the
  aggregate level where no holdings payload is fetched. §2.2's "risk contribution" stays blocked;
  *return* contribution does not.
* **PC3.** Target weight and drift are real for **basket-backed** portfolios only —
  `detail-view.ts` already computes them. §2.2's blanket "no target weights" was too strong: it is
  true for a manually grouped portfolio and false for a basket-backed one, and the screen must
  distinguish the two rather than blocking both.
* **PC4.** `RebalanceOut` carries entries, exits, holds, inside-window names and
  `target_weights`. It carries **no quantities, no cash delta, no turnover and no costs** —
  `RebalanceOrderOut.planned_qty` belongs to the desk's weekly plan, not to a portfolio rebalance.
  Quantities are therefore **not derived** from weight × value ÷ price: a derived quantity looks
  exactly like an instruction, and this product places live orders.
* **PC5.** `RegimeOut` covers tier, previous tier, breadth, actual and target equity, new-buy
  policy, reasons, staleness, evaluation dates. It does **not** carry a candidate tier, per-index
  DMA distances, confirmation progress, an algorithm version or a config hash.
* **PC5.** The exposure ladder: `kite-momentum-rebalancer/app/config.py:409` sets
  `{R1: 100, R2: 70, R3: 40, R4: REGIME_R4_EQUITY_PCT}`, and `REGIME_R4_EQUITY_PCT` defaults to
  **10**. The brief's ladder is the desk's live ladder. `baskfy_core/sleeves.py:185` falls back to
  R4 = 0 when no caps table is passed, which is a *fallback*, not the operating value; the panel
  reads the live cap from the API and never hardcodes either.
* **PC6.** Create, update, delete, add-holdings and replace-holdings endpoints all exist, and
  sub-portfolios are **sleeves** (`/portfolios/{id}/sleeves`). Archive, permissions, ownership and
  audit history do not exist and are gated on C3 multi-tenant work.

### 6.4 Status log (continued)

- **11 Sep 2026** — §6 written. PC2–PC6 gates in `gates/pc2.md` … `gates/pc6.md`, integration in
  `gates/pc-integration.md`.

- **11 Sep 2026 — the integration node, while the five leaves ran.**
  * **`GATES.md` G18 could never have passed, and that hid the real answer.** It inspected a
    container called `baskfy-web`; the compose project on the box names it `baskfy-staging-web-1`,
    so the probe failed on a missing object rather than on a stale deploy. Repaired, it reports
    `baskfy-web:bd78529` — the commit *before* PC1. **The box is serving the pre-redesign screen.**
    It cannot be fixed from here (no Docker daemon on this machine, and a Phase-A deploy is
    Maulik's call because that box is the live auto-execute host), so G18 and I12 carry an
    `ABANDON` with the handover written into `NEEDS-MAULIK.md`.
  * **Six of the brief's requirements belonged to no leaf's gates** and are now I13–I18 in
    `gates/pc-integration.md` rather than silently dropped: top-navigation conformance, the
    interaction set (export, shareable report, undo, audit log), copy style, the token and
    component specification, the two remaining states (permission restricted, market closed), and
    chart drill-down. Two more were added after that: I19, which makes somebody actually LOOK at
    the assembled screen in a browser — no suite of 2,300 green tests can report that a screen
    looks cheap — and I20, the adversarial re-read of PC1's own gates once the leaves are in.
  * **A fifth header control the deferral table never listed: the portfolio selector.** §3 above
    explains why PC1 drew neither the date range, the benchmark, the currency nor the overflow
    menu. It does not mention the selector, which the brief names twice, the second time as
    *"Keep the portfolio selector available so users can switch portfolios without returning to
    the aggregate page."* Built now, alongside the base-currency statement and the overflow menu;
    the range and benchmark are slots the performance workspace fills, so one selection cannot
    have two sources of truth.
  * **Export of the current view is real** — `commandCenterCsv` in `lib/portfolio/command-center.ts`,
    following the MODE, so a views export can never leave as capital rows and carries its own
    "these do not sum to net worth" line. A missing figure exports the word, never an empty cell:
    a blank CSV cell is read as zero by every spreadsheet there is, which would launder
    "could not be priced" into "worth nothing".
  * **PC5 found a live defect in a parent-owned file and it is fixed.** `/regime`'s explanation map
    was keyed `full | half | none`; the desk's `NewBuyMode` is `full | half | **blocked**`. So
    `BUYS["blocked"]` was `undefined` and the sentence explaining a blocked portfolio never
    rendered — on exactly the stance a reader most needs explained — while the headline above it
    still read "None" because "blocked" fell into a ternary's else branch. The page looked right
    and said nothing. A null policy also rendered a bare `–`. Fixed, and the page has a test file
    for the first time; reintroducing the original key was confirmed to turn it red, because a
    test that cannot fail is not a guard.

- **11 Sep 2026 — the worst defect the whole tree found, and PC1 shipped it green.**
  Every rate this API sends is a stored **fraction**. `portfolio_overview.py` computes
  `pct = money(move) / base` and quantizes it, so a 1.99% day arrives as `0.019900`, an 18.7% TWR
  as `0.187000` and an 8.2% fall as `-0.082000`. `lib/portfolio/overview.ts`'s `formatRate` is the
  one place in the app that turns a fraction into a percentage, and its own comment says why:
  *"one place in the system where a fraction becomes a percentage means the API, the table and the
  chart axis cannot disagree about whether they were given 12.34 or 0.1234."*

  **PC1's metric band and comparison table both bypassed it**, rendering `{value}%` directly. On a
  real payload the executive snapshot would have read **0.0199%** for a 1.99% day, **0.187%** for
  an 18.7% TWR and **-0.082%** for an 8.2% drawdown, and the comparison table would have shown
  every portfolio returning a tenth of a percent. On a drawdown that is the difference between
  "you are down 8%" and "you are down a tenth of a percent", on a screen this product expects
  people to size positions against.

  **Forty-five tests were green over it**, because the fixtures used `"1.99"` and `"18.7"` —
  values that type-check against `OverviewOut` perfectly and that the server never sends.
  Schema-exact is not the same as realistic, and this is house rule 2 in its least obvious form: a
  fixture that lies about the payload turns every assertion over it into an assertion about
  nothing. Found by PC2 while drawing the chart from the same fields; PC2 did not touch the file,
  it reported it, which is exactly what §6.1's ownership rule is for.

  Fixed at both sites, the fixtures are now the API's own shape, and two new tests cover all four
  rate positions — reverting either fix was confirmed to turn them red. The comparison table's
  return column had **no** assertion on its value at all before this.


---

## 7. Corrections to §2 — what the five leaves found by building against it (11 Sep 2026)

§2 was a survey done before any of this was written. Building against it corrected it in both
directions, and the corrections matter more than the original table: each one is a leaf finding a
claim it could not honour, or one it could better.

### 7.1 §2.1 is wrong about the invested line (PC2)

§2.1 lists *"Portfolio value over time, invested line, benchmark line"* against
`NavSeriesOut.points[]`. **There is no `invested` field on it.** The portfolio-overview point
carries `on`, `value`, `cash`, `net_flow` and `pending_reconciliation`; the `invested` and
`benchmark_value` names belong to the *desk's* own `NavPointOut`, a different schema on a
different route, and the two were conflated in the survey.

So the chart draws a **flow-derived "Capital in play"** line — the running sum of `net_flow` —
labelled as what it is rather than as cost basis, which is not the same thing and is not
available. A cost-basis line is a new §2.2 entry, not an existing capability.

### 7.2 Four things §2.2 called impossible are real (PC3, PC4)

| §2.2 said | Actually |
|---|---|
| "No target weights, no drift" | True only for a **manually grouped** portfolio. A **basket-backed** one has both, and `detail-view.ts` already computed them. A **screen-backed rebalance** has them too — `RebalanceOut.target_weights` is an equal weight over the post-rebalance set, summing to exactly 1. §2.2's claim was about a *stored* target and read as a claim about all of them |
| "No return contribution" (under risk contribution) | `DetailHoldingOut` carries `todays_contribution`, `total_contribution` and `weight`. Contribution to *return* is real per portfolio. Contribution to *risk* stays blocked, and they are not the same figure |

### 7.3 Six regime figures are missing from the RESPONSE, not the product (PC5)

§6.3 said `RegimeOut` "does not carry" a candidate tier, per-index DMA distances, confirmation
progress, an algorithm version or a config hash. True, and it understates the situation usefully:
**the desk computes and stores every one of them** — `regime_evaluations.raw_candidate_tier`,
`.input_snapshot_json.index_diagnostics`, `MaSignal.confirming_closes`, `.breadth_coverage_pct`,
`.algorithm_version`, `.config_hash`, and `regime_exposure`'s three execution columns.
`GET /api/v1/desk/regime` simply does not select them.

**Each is one route change away, not a product gap**, and the panel names the column that holds
each one. The first to add is `reason_codes_json`: the route returns only rendered English, so the
momentum sentinel's 50-DMA veto has to be read from the desk's own sentences, and the panel
degrades to "Not stated" rather than to "clear" — it can under-report a veto it cannot see, and
can never invent one or read silence as reassurance.

### 7.4 New to §2.2 — blocked metrics the survey never listed

| Metric | Why | What would unblock it |
|---|---|---|
| **Exchange and instrument type** (PC3) | `InstrumentRefOut` carries `instrument_id`, `symbol` and `name` and nothing else. The brief names it as a holdings column | Widen `InstrumentRefOut` |
| **Available quantity** — free / T1 / pledged (PC3) | Not on `DetailHoldingOut`. The desk's own non-negotiable 2 defines the split, so the data exists at the broker | Carry the three components through the holdings read |
| **Overlap between portfolios** (PC3) | The detail page reads one portfolio and never fetches the others. The brief asks for it on the Allocation tab | An overlap endpoint, or the parent fetching every portfolio's holdings |
| **"Last rebalanced"** (PC3) | No `ActivityKind` records one, so the date the brief asks for in the Overview tab cannot be read | An activity kind for a rebalance |
| **Cost-basis line on the chart** (PC2) | §7.1 — `net_flow` is money entering and leaving the ACCOUNT, never cost | A per-day invested figure on the overview series |
| **Cash drag** (PC2) | Every mark carries `cash`, so what cash *weighed* is known; what it *cost* is not, because `net_flow` never records money moving between cash and shares inside the account | An internal-flow record |
| **Turnover** (PC4) | It is a share of *value*, and the rebalance response carries no values. Counting names instead would be a different number under the same label | Quantities and prices on the rebalance response |
| **Portfolio objective** (PC6) | `PortfolioPatchIn` carries `name`, `parent_id` and `broker_account_id`. **No write body anywhere has an objective field**, so the brief's "set objective" has no endpoint at all | An `objective` column and a patch field |
| **Changing a benchmark** (PC6) | Settable at *creation* (`NewPortfolioIn.benchmark_index_id`) and not afterwards. The drawer therefore has a third availability case, `create-only` | `benchmark_index_id` on `PortfolioPatchIn` |
| **Returning holdings to Unallocated** (PC6) | There is no route that removes a holding from a portfolio. Deleting the portfolio is the only path today, and the delete panel says what that costs | A holdings-removal route |
| **A shareable read-only report** (integration) | A link another person can open needs a share token, a route that serves a portfolio to an unauthenticated reader, and a decision about what a net worth may be shown to. The first two are C3; the third is not an engineering question | Multi-tenant sharing, gated on C3 |

### 7.5 One thing deliberately NOT computed, and a test that keeps it that way (PC3)

`nav.daily_pnl[].pct` makes a realised volatility two lines of arithmetic away. It is not computed.
A volatility with no stated window and no annualisation convention is exactly the unstated model
§2.2 refuses, and `risk.test.tsx` asserts no such label ever appears — so a later session cannot
quietly "finish" the Risk tab by adding the easy half of a number whose meaning was never agreed.
