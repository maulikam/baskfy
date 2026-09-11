# Fees, brokerage and taxes — the one blocked effect that was never blocked on data

**Where this came from.** Maulik read the attribution panel's six "Not available" entries back to
me. Four need data that exists nowhere: a sector map, benchmark constituents with weights,
per-holding return series, and a record of money moving between cash and shares inside the
account. **Fees is different in kind** — its own entry said so: *"Needs: Wiring the existing cost
model through the portfolio rebalance path."* The model is built, measured and exact.

**What exists.** `baskfy_core.costs.order_cost(value, side)` — six components, reproduced exactly
against 163 real fills on 19 Aug 2026, 11.68 bps of turnover. And `portfolio_cash_flow` carries
`kind ∈ {BUY, SELL}`, `amount`, `quantity`, `instrument_id`, `occurred_on`, `portfolio_id`.

**The honesty this must not lose.** The NAV series is **not** net of these charges. So this is an
ESTIMATE of what the recorded trades cost, not a reconciling term and not a billed amount. Saying
otherwise would turn a helpful figure into a false one, on a product that places live orders.

- [x] G1: A pure function turns a portfolio's BUY/SELL flows into a cost breakdown, calling
      `baskfy_core.costs.order_cost` rather than restating any rate. Law 1: no I/O in core.
  CHECK: cd decile-blueprint && grep -c "order_cost" packages/core/src/baskfy_core/portfolio_costs.py
  EXPECT: /^[1-9]/
  EVIDENCE: `1` — `portfolio_costs.py` calls `order_cost` and restates no rate.

- [x] G2: **The DP charge is per scrip per SELL day, not per order.** It is the one fixed
      component, and charging it per order overstates a day on which one holding was sold in three
      tranches. The model's own docstring is explicit that this is the charge that makes a small
      sell disproportionately expensive, so getting it wrong is not a rounding error.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_portfolio_costs.py -k "dp" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 15 passed. **Proved by sabotage:** replacing the per-scrip-day count with a per-order one
      makes two tests fail (`three_sells_of_one_scrip_on_one_day_are_charged_once` and the one that
      names the naive arithmetic), and restoring it makes 15 pass again.

- [x] G3: Every component of the model is carried through and reported separately — a single
      "fees" total hides that STT is 85% of it and that nothing at all is brokerage.
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_portfolio_costs.py 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: 15 passed — all six components plus brokerage reported separately, and the total asserted
      to equal their sum. Brokerage is ₹0.00 and says so rather than being omitted.

- [x] G4: A portfolio with **no recorded trades** in the window produces no figure and says why —
      not ₹0, which would read as "you were charged nothing".
  CHECK: cd decile-blueprint && uv run pytest packages/core/tests/test_portfolio_costs.py -k "no_trades or empty" 2>&1 | tail -3
  EXPECT: /passed/
  EVIDENCE: no trades → `None`, not a zeroed record. Dividends, deposits and assignments are not trades.
      The reason names broker sync as the cause, because that is the common case.

- [x] G5: The API exposes it on the attribution payload, computed from `portfolio_cash_flow`.
  CHECK: cd decile-blueprint && grep -rc "portfolio_costs\|estimated_costs" services/api/src/baskfy_api/routers/portfolio_overview.py
  EXPECT: /^[1-9]/
  EVIDENCE: `_estimated_costs_out` on `portfolio_overview.py`, computed from `ledger.flow_rows`;
      `EstimatedCostsOut` on the payload; OpenAPI and the TypeScript client regenerated.

- [x] G6: **The screen says it is an estimate and that the return is not net of it.** This is the
      gate that keeps the figure honest; without it the panel implies a net-of-fees return it does
      not have.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/components/portfolio src/lib/portfolio --silent --reporter=basic -t "fees" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: ` Tests  35 passed (35)` — the caveat test asserts both "not a billed amount" and
      "before these charges, not after them". **This is the gate the feature turns on**: the value
      series is still gross, and the old blocked entry's warning was precisely that shipping a
      figure without this sentence would be the gross number under a different name.

- [x] G7: Fees leaves `NOT_DECOMPOSABLE`, which therefore drops to four — and the four that remain
      are exactly the ones needing data that does not exist.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run src/lib/portfolio --silent --reporter=basic -t "decompos" 2>&1 | grep -E "Tests |Test Files "
  EXPECT: /Tests .*passed/
  EVIDENCE: `NOT_DECOMPOSABLE` is 4, asserted by length and by name. The four that remain each need data
      that exists nowhere — a sector map, benchmark constituents with weights, a per-holding return
      series, a record of money moving between cash and shares inside the account.

- [x] G8: Nothing internal reaches the reader, and no bare dash. Both suites green, both lints
      clean.
  CHECK: cd decile-blueprint/apps/web && pnpm exec vitest run --silent 2>&1 | grep -E "Test Files" && pnpm run lint 2>&1 | tail -2
  EXPECT: /0 errors/
  EVIDENCE: web `Test Files 165 passed (165) | Tests 2937 passed (2937)`, `pnpm run lint` **0 errors**;
      core `mypy --strict` Success, `ruff check` clean.

<!-- A checked box whose EVIDENCE reads "pending" is UNMET. ABANDON: G<n> <reason> is the honest exit. -->
