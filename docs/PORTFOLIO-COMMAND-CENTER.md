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
