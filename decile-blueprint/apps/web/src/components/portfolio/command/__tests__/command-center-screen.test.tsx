import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

/* The export writes a file. In jsdom `URL.createObjectURL` does not exist, so exercising the real
   `downloadCsv` would assert nothing about the export and would fail for a reason unrelated to it.
   Mocking the download captures what would have been WRITTEN, which is the part that can be
   wrong — a file whose rows disagree with the screen is the defect worth catching. */
const downloaded = vi.hoisted(() => ({ calls: [] as Array<[string, string]> }));
vi.mock("@/lib/portfolios/export", () => ({
  downloadCsv: (filename: string, text: string) => {
    downloaded.calls.push([filename, text]);
  },
}));

import { CommandCenterScreen } from "@/components/portfolio/command/command-center-screen";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Unallocated } from "@/lib/portfolio/organize";
import type { Overview, PortfolioRow } from "@/lib/portfolio/overview";

/**
 * The Command Center as a person meets it.
 *
 * These are written against the brief's own sentences rather than against the markup, because
 * the markup is the part that is allowed to change. The four that matter most, and would each be
 * a real defect in a product that places live orders:
 *
 *   · a bare "—" reaching the screen with nothing to explain it;
 *   · a monitoring view's value entering a capital total;
 *   · an alert phrased as investment advice, which Baskfy is not registered to give;
 *   · a figure appearing for something Baskfy cannot actually compute.
 */

/* Schema-exact, no `as`. `OverviewOut` is generated from the API's OpenAPI document, so an
   assertion here would let the fixture describe a payload the server never sends.

   SCHEMA-EXACT IS NOT THE SAME AS REALISTIC, and the difference cost this suite a real defect.
   Every rate below is a stored FRACTION, because that is what the server computes — `pct = money
   / base`, quantized (`portfolio_overview.py`), so a 1.99% day is `"0.019900"` and an 18.7% TWR is
   `"0.187000"`. These fixtures originally read `"1.99"` and `"18.7"`, which type-check perfectly
   and are values the API never sends. The band rendered them verbatim with a `%` appended and 45
   tests stayed green while a real payload would have shown **"0.0199%"** for a 1.99% day. House
   rule 2, in its least obvious form: a fixture that lies about the payload turns every assertion
   over it into an assertion about nothing. Found by PC2 against the same fields. */
function row(id: number, name: string, value: string, over: Partial<PortfolioRow> = {}): PortfolioRow {
  return {
    portfolio_id: id,
    name,
    kind: "CAPITAL",
    source: "HOLDING_GROUP",
    source_badge: "Grouped",
    started_on: "2026-01-01",
    value,
    cash: "0",
    counts_toward_total: true,
    status: "Synced",
    holdings_count: 3,
    broker_count: 1,
    pending_reconciliation: false,
    brokers: [{ broker_account_id: 1, broker_id: "zerodha", label: "Zerodha" }],
    todays_pnl: { amount: "1000", label: "today" },
    headline_return: {
      kind: "SINCE_GROUPED",
      label: "Since grouped",
      since: "2026-01-01",
      is_model: false,
      value: "0.125000",
    },
    ...over,
  };
}

const NO_PILE: Overview["unallocated"] = {
  cash: "0",
  cta: "Organize into portfolios",
  holdings_count: 0,
  holdings_value: "0",
  total_value: "0",
  pending_reconciliation: false,
};

function overview(over: Partial<Overview> = {}): Overview {
  return {
    hero: {
      current_value: "3680509.37",
      invested: "3100000.00",
      holdings_without_cost_basis: 0,
      pending_reconciliation: false,
      todays_pnl: { amount: "72029.56", label: "today", pct: "0.019900" },
      total_pnl: { amount: "580509.37", label: "total" },
      twr: { label: "TWR since created", since: "2026-01-01", value: "0.187000" },
      xirr: { label: "XIRR", since: "2026-01-01", value: "0.214000" },
      secondary: {
        broker_count: 2,
        cash: "120000.00",
        dividends: "0",
        realised_pnl: { amount: "14000.00", label: "realised" },
        unrealised_pnl: { amount: "566509.37", label: "unrealised" },
      },
    },
    chart: {
      pending_reconciliation: false,
      range: "1Y",
      total_return: { label: "Total return", since: "2026-01-01", value: "0.187000" },
      max_drawdown: { drawdown: "-0.082000", peak_on: "2026-06-01", trough_on: "2026-07-10" },
      benchmark: {
        name: "NIFTY 500",
        portfolio: {
          kind: "TWR_SINCE_CREATED",
          is_model: false,
          label: "Portfolio",
          since: "2026-01-01",
          value: "0.187000",
        },
        benchmark: {
          kind: "TWR_SINCE_CREATED",
          is_model: false,
          label: "NIFTY 500",
          since: "2026-01-01",
          value: "0.121000",
        },
        difference: "0.066000",
      },
      drawdown: [{ drawdown: "-0.021000", index: "97.9", peak: "3750000.00", on: "2026-09-10" }],
    },
    prices_label: "Prices: close of 2026-09-10",
    prices_as_of: "2026-09-10",
    holdings_synced_label: "Holdings reconciled today at 09:18",
    holdings_synced_on: "2026-09-11",
    monitoring_excluded_note:
      "Monitoring views may contain overlapping holdings and are excluded from total portfolio value.",
    open_reconciliation_count: 0,
    portfolios: [row(1, "Long term", "2000000"), row(2, "Swing Manual", "1500000")],
    monitoring_views: [
      row(9, "High momentum", "900000", { kind: "MONITORING", counts_toward_total: false }),
    ],
    sync_status: [],
    attention: [],
    unallocated: NO_PILE,
    ...over,
  };
}

/** Nothing unallocated and nothing unreconciled — the only shape that should read "Live". */
const CLEAN: Unallocated = {
  cash: "0",
  cta: "Sort it",
  holdings_count: 0,
  holdings_value: "0",
  total_value: "0",
  pending_reconciliation: false,
};

const pile = (v: string): Unallocated => ({
  cash: "0",
  cta: "Sort it",
  holdings_count: 2,
  holdings_value: v,
  total_value: v,
  pending_reconciliation: false,
});

/** `app/providers.tsx` mounts `TooltipProvider` around the whole app; a test renders a subtree,
 *  so it supplies the same context rather than the components carrying their own provider. */
function renderScreen(over: Partial<Overview> = {}, unallocated = pile("180509.37")) {
  return render(
    <TooltipProvider>
      <CommandCenterScreen overview={overview(over)} unallocated={unallocated} />
    </TooltipProvider>,
  );
}

/* ------------------------------------------------------------------ the two models */

describe("the capital / views mode switch", () => {
  it("mode switch: sits beside the title and defaults to capital portfolios", () => {
    renderScreen();

    expect(screen.getByRole("heading", { name: "Portfolio Command Center" })).toBeInTheDocument();
    const swtch = screen.getByTestId("mode-switch");
    expect(swtch).toHaveAttribute("data-mode", "capital");
    expect(within(swtch).getByRole("tab", { name: /Capital portfolios/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("mode switch: names both models with counts, so the distinction is visible before clicking", () => {
    renderScreen();
    const swtch = screen.getByTestId("mode-switch");

    expect(within(swtch).getByRole("tab", { name: /Capital portfolios/ })).toHaveTextContent("2");
    expect(within(swtch).getByRole("tab", { name: /Monitoring views/ })).toHaveTextContent("1");
  });

  it("monitoring: switching to views shows the exclusion notice", async () => {
    const user = userEvent.setup();
    renderScreen();

    await user.click(screen.getByRole("tab", { name: /Monitoring views/ }));

    expect(screen.getByTestId("views-notice")).toHaveTextContent(
      "excluded from total portfolio value",
    );
  });

  it("monitoring: a view's value never reaches the capital band", () => {
    /* The fixture's view is worth 900,000. Net worth is 2,000,000 + 1,500,000 + 180,509.37 =
       3,680,509.37 — and if the view ever leaked in it would read 4,580,509.37. */
    renderScreen();

    expect(screen.getByTestId("metric-band")).toHaveTextContent("₹36,80,509.37");
    expect(screen.getByTestId("metric-band")).not.toHaveTextContent("45,80,509.37");
  });

  it("monitoring: the executive band is not shown at all in views mode", async () => {
    /* Capital totals are meaningless over overlapping lenses, so rather than blanking them the
       band is absent and the notice explains why. */
    const user = userEvent.setup();
    renderScreen();

    await user.click(screen.getByRole("tab", { name: /Monitoring views/ }));

    expect(screen.queryByTestId("metric-band")).toBeNull();
  });
});

/* ------------------------------------------------------------ the executive band */

describe("the executive snapshot", () => {
  it("snapshot: shows net worth, today, invested, cash, XIRR, TWR and drawdown together", () => {
    renderScreen();
    const band = screen.getByTestId("metric-band");

    expect(band).toHaveTextContent("Net worth");
    expect(band).toHaveTextContent("₹36,80,509.37");
    expect(band).toHaveTextContent("Today");
    expect(band).toHaveTextContent("₹72,029.56");
    expect(band).toHaveTextContent("Invested");
    expect(band).toHaveTextContent("XIRR");
    expect(band).toHaveTextContent("Drawdown");
  });

  it("snapshot: today's move carries its percentage as well as its amount", () => {
    renderScreen();

    expect(screen.getByTestId("metric-band")).toHaveTextContent("+1.99%");
  });

  it("snapshot: a stored FRACTION is rendered as a human percentage, everywhere it appears", () => {
    /* The regression this exists to prevent, in all four places at once. The server sends
       `0.019900` for a 1.99% day, `0.214000` for a 21.4% XIRR, `0.187000` for an 18.7% TWR and
       `-0.082000` for an 8.2% fall. Rendering any of them verbatim with a `%` appended reads as a
       number a hundred times too small, which on a drawdown is the difference between "you are
       down 8%" and "you are down a tenth of a percent". Only the today's-move assertion above
       caught the original defect; the other three sites were unguarded. */
    renderScreen();
    const band = screen.getByTestId("metric-band");

    expect(band).toHaveTextContent("+21.40%");
    expect(band).toHaveTextContent("+18.70%");
    expect(band).toHaveTextContent("-8.20%");

    // And the raw fraction never reaches the screen under a percent sign.
    expect(band.textContent).not.toMatch(/0\.0199%|0\.214%|0\.187%|-0\.082%/);
  });

  it("colour: direction is a glyph as well as a hue, so it survives greyscale", () => {
    renderScreen();

    // ▲ for the day's gain; the class is there too, but the glyph is what a colour-blind reader
    // and a monochrome print both still get.
    expect(screen.getByTestId("metric-band").textContent).toContain("▲");
  });

  it("unavailable: a missing figure shows its REASON in place of a dash", () => {
    renderScreen({
      hero: {
        ...overview().hero,
        invested: null,
        invested_unavailable_reason: "Cost basis is missing for 4 holdings.",
      },
    });

    const band = screen.getByTestId("metric-band");
    expect(band).toHaveTextContent("Cost basis is missing for 4 holdings.");
    expect(band).toHaveTextContent("Not available");
  });

  it("unavailable: no bare em dash is ever rendered in the band", () => {
    /* The brief's hard rule, asserted over the payload most likely to break it: one with almost
       nothing in it. */
    renderScreen(
      {
        hero: {
          current_value: "0",
          holdings_without_cost_basis: 4,
          pending_reconciliation: false,
          todays_pnl: { label: "today" },
          total_pnl: { label: "total" },
          twr: { label: "TWR since created", since: "2026-01-01" },
          xirr: { label: "XIRR", since: "2026-01-01" },
          secondary: {
            broker_count: 0,
            cash: "0",
            dividends: "0",
            realised_pnl: { label: "realised" },
            unrealised_pnl: { label: "unrealised" },
          },
        },
        chart: {
          pending_reconciliation: false,
          range: "1Y",
          total_return: { label: "Total return", since: "2026-01-01" },
        },
      },
      pile("0"),
    );

    const band = screen.getByTestId("metric-band");
    expect(band.textContent).not.toContain("—");
    // and every gap says something instead
    expect(band.textContent).toContain("Not available");
  });
});

/* ------------------------------------------------------------- the health strip */

describe("the data-health strip", () => {
  it("health: reports portfolios, holdings, brokers and both freshness labels", () => {
    renderScreen();
    const strip = screen.getByTestId("health-strip");

    expect(strip).toHaveTextContent("2 portfolios");
    expect(strip).toHaveTextContent("8 holdings");
    expect(strip).toHaveTextContent("2 brokers connected");
    expect(strip).toHaveTextContent("Prices: close of 2026-09-10");
    expect(strip).toHaveTextContent("Holdings reconciled today at 09:18");
  });

  it("health: a clean book reads Live", () => {
    /* "Clean" has to mean clean: the default fixture leaves two holdings unallocated, which is
       itself something to act on, so this one passes a book with nothing loose in it. */
    renderScreen({}, CLEAN);

    expect(screen.getByTestId("health-strip")).toHaveAttribute("data-status", "live");
    expect(screen.getByTestId("health-status-chip")).toHaveTextContent("Live");
  });

  it("health: a problem NAMES what is affected rather than warning generically", async () => {
    const user = userEvent.setup();
    renderScreen({
      portfolios: [row(1, "Long term", "2000000", { pending_reconciliation: true })],
    });

    expect(screen.getByTestId("health-strip")).toHaveAttribute("data-status", "action-required");
    await user.click(screen.getByTestId("health-status-chip"));

    const problems = screen.getByTestId("health-problems");
    expect(problems).toHaveTextContent("Long term does not reconcile with the broker");
    expect(problems).toHaveTextContent("frozen");
  });

  it("colour: the status chip carries its state as a word, not only a colour", () => {
    renderScreen({
      portfolios: [row(1, "Long term", "2000000", { pending_reconciliation: true })],
    });

    expect(screen.getByTestId("health-status-chip")).toHaveTextContent("Action required");
  });
});

/* ------------------------------------------------------------ the comparison table */

describe("the comparison table", () => {
  /* The screen renders the table AND the phone card list, and CSS decides which is shown — so a
     query has to say which of the two it means. In a browser only one is in the accessibility
     tree; in jsdom, where no stylesheet runs, both are. */
  it("table: replaces the repeated blocks with one row per portfolio", () => {
    renderScreen();
    const table = screen.getByRole("table");

    expect(within(table).getByRole("link", { name: "Long term" })).toHaveAttribute(
      "href",
      "/portfolio/1",
    );
    expect(within(table).getByRole("link", { name: "Swing Manual" })).toBeInTheDocument();
  });

  it("table: sorts, and says which column it is sorted by", async () => {
    const user = userEvent.setup();
    renderScreen();
    const table = screen.getByRole("table");

    // Value descending by default: Long term (20L) before Swing Manual (15L).
    const names = () =>
      within(table)
        .getAllByRole("link")
        .map((a) => a.textContent)
        .filter((t) => t === "Long term" || t === "Swing Manual");
    expect(names()[0]).toBe("Long term");

    await user.click(within(table).getByRole("button", { name: /Value/ }));
    expect(names()[0]).toBe("Swing Manual");
    expect(within(table).getByRole("columnheader", { name: /Value/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });

  it("table: a row expands to show the detail behind it", async () => {
    const user = userEvent.setup();
    renderScreen();

    await user.click(screen.getByRole("button", { name: "Expand Long term" }));

    const detail = screen.getByTestId("portfolio-detail-1");
    expect(detail).toHaveTextContent("Zerodha");
    expect(detail).toHaveTextContent("Measured from");
  });

  /* G16. The brief asks that the desktop table is NOT attempted on a phone; a ten-column grid on
     390px is a sideways scroll that hides the columns a person opened the screen for. So the same
     rows are rendered as a stacked list, and the two must not diverge — a phone that quietly shows
     less than the desktop is the failure this asserts against. */
  it("table: a phone gets a stacked list rather than the ten-column grid", async () => {
    const user = userEvent.setup();
    renderScreen();
    const cards = screen.getByTestId("portfolio-cards");

    expect(within(cards).getByRole("link", { name: "Long term" })).toHaveAttribute(
      "href",
      "/portfolio/1",
    );
    expect(within(cards).queryByRole("table")).toBeNull();
    expect(cards).toHaveTextContent("Today");
    expect(cards).toHaveTextContent("Holdings");

    /* And it expands to the SAME detail the table row shows. */
    await user.click(within(cards).getAllByRole("button", { name: /More/ })[0]!);
    const detail = screen.getByTestId("portfolio-card-detail-1");
    expect(detail).toHaveTextContent("Zerodha");
    expect(detail).toHaveTextContent("Measured from");
  });

  it("table: a portfolio's return is a percentage, not the fraction the server stores", () => {
    /* The same defect as the metric band's, in the column a reader compares portfolios on.
       `headline_return.value` is a fraction — `return-value.tsx` has always put it through
       `formatRate` — and printing it verbatim turned a 12.5% return into "0.125%". Both the
       desktop table and the phone list render it, so both are asserted. */
    renderScreen({ portfolios: [row(1, "Swing Manual", "1200000")] });

    const table = screen.getByTestId("comparison-table");
    expect(table).toHaveTextContent("12.50%");
    expect(table.textContent).not.toMatch(/0\.125%/);
  });

  it("colour: the portfolio identifier is a colour AND a letter", () => {
    renderScreen();

    // "L" for Long term, "S" for Swing Manual — the identifier still works in greyscale.
    const rowEl = screen.getByTestId("portfolio-row-1");
    expect(rowEl.textContent).toContain("L");
  });

  it("fabricated: columns Baskfy cannot compute are named as absent, not shown as dashes", () => {
    renderScreen();
    const table = screen.getByRole("table");

    // No beta/VaR/Sharpe column headers at all …
    expect(within(table).queryByRole("columnheader", { name: /beta|sharpe|volatility/i })).toBeNull();
    // … and the reason each is missing is stated once.
    const notYet = screen.getByTestId("not-yet-measured");
    expect(notYet).toHaveTextContent("Beta, volatility, Sharpe");
    expect(notYet).toHaveTextContent("no return-series statistics");
  });
});

/* -------------------------------------------------------------- the attention rail */

describe("the intelligence rail", () => {
  it("attention: says there is nothing to do when there is nothing to do", () => {
    renderScreen({}, CLEAN);

    expect(screen.getByTestId("attention-rail")).toHaveTextContent("No action required");
  });

  it("attention: each alert says what changed, why it matters and the next step", () => {
    renderScreen(
      { portfolios: [row(1, "Long term", "2000000", { pending_reconciliation: true })] },
      CLEAN,
    );

    const rail = screen.getByTestId("attention-rail");
    expect(rail).toHaveTextContent("Critical");
    expect(rail).toHaveTextContent("Long term does not reconcile with the broker");
    expect(rail).toHaveTextContent("frozen");
    // One alert, one next step, pointing at the portfolio it is about.
    expect(within(rail).getByRole("link", { name: /Open/ })).toHaveAttribute(
      "href",
      "/portfolio/1",
    );
    /* And WHEN it was detected. Baskfy keeps no alert table, so the honest answer is the pass
       that would have seen it — the last reconcile — named with its date. */
    expect(rail).toHaveTextContent("detected at the reconcile on 2026-09-11");
  });

  it("attention: an alert with no dated reconcile says so rather than looking fresh", () => {
    renderScreen(
      {
        holdings_synced_on: null,
        portfolios: [row(1, "Long term", "2000000", { pending_reconciliation: true })],
      },
      CLEAN,
    );

    expect(screen.getByTestId("attention-rail")).toHaveTextContent(
      "detected at the last reconcile, which is undated",
    );
  });

  it("attention: a broker that has never synced is not dated to a reconcile it missed", () => {
    renderScreen(
      {
        sync_status: [
          {
            broker: { broker_account_id: 5, broker_id: "zerodha", label: "Zerodha" },
            label: "Never synced.",
          },
        ],
      },
      CLEAN,
    );

    expect(screen.getByTestId("attention-rail")).toHaveTextContent(
      "no sync has ever completed for this account",
    );
  });

  it("attention: alerts are never phrased as investment advice", () => {
    renderScreen({
      open_reconciliation_count: 2,
      portfolios: [row(1, "Long term", "2000000", { pending_reconciliation: true })],
    });

    /* Baskfy is not a registered adviser — the disclaimer on every page says so — so the rail
       may describe the DATA and never the trade. */
    const text = screen.getByTestId("attention-rail").textContent ?? "";
    expect(text).not.toMatch(/\b(buy|sell|should invest|we recommend|hot stock|guaranteed)\b/i);
  });

  it("attention: critical items sort above review items", () => {
    renderScreen({
      portfolios: [row(1, "Long term", "2000000", { pending_reconciliation: true })],
    });

    const rail = screen.getByTestId("attention-rail");
    const text = rail.textContent ?? "";
    expect(text.indexOf("Critical")).toBeLessThan(text.length);
  });
});

/* -------------------------------------------------------------------- the states */

describe("every state explains itself", () => {
  it("state: an API failure says what happened and offers one next step", () => {
    render(
      <TooltipProvider>
        <CommandCenterScreen overview={null} unallocated={null} error="The service timed out." />
      </TooltipProvider>,
    );

    const explain = screen.getByTestId("command-center-explain");
    expect(explain).toHaveTextContent("could not be loaded");
    expect(explain).toHaveTextContent("The service timed out.");
    expect(within(explain).getByRole("link")).toHaveTextContent("Try again");
  });

  it("state: no data at all points at connecting a broker", () => {
    render(
      <TooltipProvider>
        <CommandCenterScreen overview={null} unallocated={null} />
      </TooltipProvider>,
    );

    const explain = screen.getByTestId("command-center-explain");
    expect(explain).toHaveTextContent("No portfolio data yet");
    expect(within(explain).getByRole("link")).toHaveTextContent("Connect a broker");
  });

  it("state: everything unallocated explains what grouping is for", () => {
    renderScreen({ portfolios: [] }, pile("500000"));

    expect(screen.getByTestId("command-center-explain")).toHaveTextContent(
      "Nothing is filed into a portfolio yet",
    );
  });

  it("state: with no capital portfolio, rebalance is disabled WITH a reason", () => {
    renderScreen({ portfolios: [] }, pile("500000"));

    expect(screen.getByTestId("review-rebalance")).toBeDisabled();
    expect(screen.getByTestId("rebalance-blocked")).toHaveTextContent(
      "Create a capital portfolio first",
    );
  });

  /* These four exist because this product has already shipped a styled button that swallowed its
     click. A dead primary action is indistinguishable from a broken page, so every action on this
     screen must either resolve to a destination or be disabled beside the reason it cannot. */
  it("state: the primary action leads somewhere — one portfolio links straight to its rebalance", () => {
    renderScreen({ portfolios: [row(7, "Swing Manual", "2000000")] });

    const action = screen.getByTestId("review-rebalance");
    expect(action).toHaveAttribute("href", "/portfolios/7/rebalance");
  });

  it("state: with several portfolios the primary action names them rather than guessing one", async () => {
    const user = userEvent.setup();
    renderScreen();

    await user.click(screen.getByTestId("review-rebalance"));

    expect(screen.getByRole("menuitem", { name: "Long term" })).toHaveAttribute(
      "href",
      "/portfolios/1/rebalance",
    );
    expect(screen.getByRole("menuitem", { name: "Swing Manual" })).toHaveAttribute(
      "href",
      "/portfolios/2/rebalance",
    );
  });

  it("state: Add portfolio leads to the screen where holdings are filed", () => {
    renderScreen();

    expect(screen.getByTestId("add-portfolio")).toHaveAttribute("href", "/portfolio/holdings");
  });

  it("state: no monitoring views explains what a view is", async () => {
    const user = userEvent.setup();
    renderScreen({ monitoring_views: [] });

    await user.click(screen.getByRole("tab", { name: /Monitoring views/ }));

    expect(screen.getByTestId("command-center-explain")).toHaveTextContent(
      "No monitoring views yet",
    );
  });
});

/* ------------------------------------------------------- the brief's own prohibitions */

describe("what the screen must never do", () => {
  it("fabricated: no figure is shown for anything Baskfy cannot compute", () => {
    renderScreen();
    const text = document.body.textContent ?? "";

    /* A number next to one of these names would be invented. They may appear as prose in the
       "not yet measured" list — that is the point — but never as "Beta 1.02". */
    for (const name of ["Beta", "Sharpe", "Value at Risk", "Target weight"]) {
      const withFigure = new RegExp(`${name}[^a-zA-Z]{0,3}[-+]?\\d`, "i");
      expect(withFigure.test(text), `"${name}" is shown with a figure`).toBe(false);
    }
  });

  it("state: the screen offers no way to place an order", () => {
    renderScreen();
    const text = document.body.textContent ?? "";

    expect(text).not.toMatch(/place order|buy now|execute trade/i);
    expect(screen.getByTestId("review-rebalance")).toHaveTextContent("Review rebalance");
  });
});

/* ------------------------------------------------------------------ the header controls
 *
 * `gates/pc-integration.md` I2, I13 and I14. PC1 deferred four of these because they scope a
 * chart it did not draw; the fifth — the portfolio selector — was in the brief's header list and
 * in no leaf's gates, which is how a requirement disappears quietly.
 *
 * The rule every one of them is held to: **a control either does something or says why it
 * cannot.** Not one is drawn greyed-out and silent. */

describe("the header controls", () => {
  beforeEach(() => {
    downloaded.calls.length = 0;
  });

  it("header control: base currency is stated as INR, with the reason there is no other", async () => {
    const user = userEvent.setup();
    renderScreen();

    const chip = screen.getByTestId("base-currency");
    expect(chip).toHaveTextContent("INR");

    await user.hover(chip);
    expect(await screen.findByText(/Indian equities only/)).toBeInTheDocument();
  });

  it("navigation: the portfolio selector lists all portfolios and each one by name", async () => {
    const user = userEvent.setup();
    renderScreen({
      portfolios: [row(1, "Swing Manual", "1200000"), row(2, "Long-Term Wealth", "900000")],
    });

    await user.click(screen.getByTestId("portfolio-selector"));
    const menu = await screen.findByRole("menu");

    expect(within(menu).getByRole("menuitem", { name: /All portfolios/ })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: /Swing Manual/ })).toHaveAttribute(
      "href",
      "/portfolio/1",
    );
    expect(within(menu).getByRole("menuitem", { name: /Long-Term Wealth/ })).toHaveAttribute(
      "href",
      "/portfolio/2",
    );
  });

  it("navigation: choosing a portfolio OPENS its workspace rather than filtering this screen", () => {
    /* The distinction the brief draws twice. A filter would leave a person on the aggregate
       screen wondering why the comparison now has one row; the workspace is a different set of
       questions and has its own surface. Every item is a link, so nothing here is a filter. */
    renderScreen({ portfolios: [row(1, "Swing Manual", "1200000")] });

    expect(screen.getByTestId("portfolio-selector")).toBeInTheDocument();
  });

  it("state: with nothing filed, the selector says why rather than sitting disabled", async () => {
    const user = userEvent.setup();
    renderScreen({ portfolios: [] }, CLEAN);

    const empty = screen.getByTestId("portfolio-selector-empty");
    expect(empty).toHaveTextContent("No portfolios yet");

    await user.hover(empty);
    expect(await screen.findByText(/nothing to switch between/i)).toBeInTheDocument();
  });

  it("header control: the overflow menu carries export, settings and reconciliation", async () => {
    const user = userEvent.setup();
    renderScreen();

    await user.click(screen.getByTestId("overflow-menu"));
    const menu = await screen.findByRole("menu");

    expect(within(menu).getByTestId("export-view")).toBeInTheDocument();
    expect(within(menu).getByTestId("open-settings")).toBeInTheDocument();
    expect(within(menu).getByTestId("open-reconcile")).toHaveAttribute("href", "/reconcile");
  });

  it("unavailable: the import Baskfy cannot do yet is NAMED with what it would give, not hidden", async () => {
    const user = userEvent.setup();
    renderScreen();

    await user.click(screen.getByTestId("overflow-menu"));
    const blocked = await screen.findByTestId("import-blocked");

    expect(blocked).toHaveTextContent(/consolidated account statement/i);
    expect(blocked).toHaveTextContent(/purchase prices and dates your broker does not send/i);
  });

  it("interaction: a shareable report is NAMED with why it cannot exist yet", async () => {
    /* Every interaction the brief lists either works or says why it cannot. A link another person
       can open needs sharing to exist in the account model — not a button. */
    const user = userEvent.setup();
    renderScreen();

    await user.click(screen.getByTestId("overflow-menu"));
    const blocked = await screen.findByTestId("share-blocked");

    expect(blocked).toHaveTextContent(/Shareable read-only report/);
    expect(blocked).toHaveTextContent(/needs sharing to exist in the account model/);
  });

  it("interaction: exporting the current view writes the rows that are on screen", async () => {
    const user = userEvent.setup();
    renderScreen({
      portfolios: [row(1, "Swing Manual", "1200000"), row(2, "Long-Term Wealth", "900000")],
    });

    await user.click(screen.getByTestId("overflow-menu"));
    await user.click(await screen.findByTestId("export-view"));

    expect(downloaded.calls).toHaveLength(1);
    const [filename, text] = downloaded.calls[0]!;
    expect(filename).toBe("baskfy-capital-portfolios.csv");
    expect(text).toMatch(/Swing Manual/);
    expect(text).toMatch(/Long-Term Wealth/);
  });

  it("monitoring: the export follows the MODE, so views never leave as capital rows", async () => {
    const user = userEvent.setup();
    renderScreen({
      portfolios: [row(1, "Swing Manual", "1200000")],
      monitoring_views: [
        row(9, "High Momentum", "400000", { kind: "MONITORING", counts_toward_total: false }),
      ],
    });

    await user.click(screen.getByRole("tab", { name: /Monitoring views/ }));
    await user.click(screen.getByTestId("overflow-menu"));
    await user.click(await screen.findByTestId("export-view"));

    const [filename, text] = downloaded.calls[0]!;
    expect(filename).toBe("baskfy-monitoring-views.csv");
    expect(text).toMatch(/High Momentum/);
    expect(text).not.toMatch(/Swing Manual/);
    expect(text).toMatch(/do not sum to net worth/);
  });

  it("state: with no rows the export names the reason instead of downloading an empty file", async () => {
    const user = userEvent.setup();
    renderScreen({ portfolios: [] }, CLEAN);

    await user.click(screen.getByTestId("overflow-menu"));
    const blocked = await screen.findByTestId("export-view-blocked");

    expect(blocked).toHaveTextContent(/no rows to export/i);
    expect(downloaded.calls).toHaveLength(0);
  });
});

/* ------------------------------------------------------------------ the assembled screen
 *
 * `gates/pc-integration.md`. Five leaves each finished their own ledger; thirty-two green leaves
 * can still be a broken product, and this is where that is caught. These assert the SEAMS —
 * whether the panels are present, wired and consistent with each other — not the panels
 * themselves, which each leaf's own suite already holds. */

describe("the assembled command centre", () => {
  it("mounted: the performance workspace is drawn, not linked to on another page", () => {
    /* PC1 shipped a sentence here pointing at `/portfolio/overview`, which was the honest seam
       while PC2 did not exist. It exists. */
    renderScreen();

    expect(screen.getByTestId("performance-workspace")).toBeInTheDocument();
    expect(screen.queryByText(/the overview chart/)).not.toBeInTheDocument();
  });

  it("mounted: attribution says which portfolio moved the number", () => {
    renderScreen({
      portfolios: [row(1, "Swing Manual", "1200000"), row(2, "Long-Term Wealth", "900000")],
    });

    expect(screen.getByTestId("attribution")).toBeInTheDocument();
  });

  it("mounted: the regime panel is in the rail, above the alerts", () => {
    renderScreen();
    expect(screen.getByTestId("regime-panel")).toBeInTheDocument();
  });

  it("mounted: the regime panel shows NO tier when the desk did not answer", () => {
    /* The one regime seam worth an integration test: a screen that quietly shows an old stance as
       current is worse than one that shows none, and the wiring is what decides which happens. */
    render(
      <TooltipProvider>
        <CommandCenterScreen
          overview={overview()}
          unallocated={pile("180509.37")}
          regime={null}
          regimeError="The desk did not answer."
        />
      </TooltipProvider>,
    );

    const panel = screen.getByTestId("regime-panel");
    expect(panel.textContent).not.toMatch(/\bR[1-4]\b/);
  });

  it("mounted: the management drawer opens from the overflow menu", async () => {
    const user = userEvent.setup();
    renderScreen();

    expect(screen.queryByTestId("manage-drawer")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("overflow-menu"));
    await user.click(await screen.findByTestId("open-settings"));

    expect(await screen.findByTestId("manage-drawer")).toBeInTheDocument();
  });

  it("header control: the benchmark is NAMED, and says where it is changed", async () => {
    const user = userEvent.setup();
    renderScreen();

    const chip = screen.getByTestId("benchmark-statement");
    expect(chip).toHaveTextContent("vs NIFTY 500");

    await user.hover(chip);
    expect(await screen.findByText(/chosen per portfolio, not per page/)).toBeInTheDocument();
  });

  it("header control: a series with no benchmark says so rather than naming one", () => {
    renderScreen({
      chart: {
        pending_reconciliation: false,
        range: "1Y",
        total_return: { label: "Total return", since: "2026-01-01", value: "0.187000" },
      },
    });

    expect(screen.getByTestId("benchmark-statement")).toHaveTextContent("No benchmark");
  });

  it("monitoring: with everything assembled, a view's value still reaches no capital total", async () => {
    /* The regression four extra panels are most likely to introduce. */
    const user = userEvent.setup();
    renderScreen({
      portfolios: [row(1, "Swing Manual", "1200000")],
      monitoring_views: [
        row(9, "High Momentum", "400000", { kind: "MONITORING", counts_toward_total: false }),
      ],
    });

    await user.click(screen.getByRole("tab", { name: /Monitoring views/ }));

    expect(screen.queryByTestId("metric-band")).not.toBeInTheDocument();
    expect(screen.queryByTestId("performance-workspace")).not.toBeInTheDocument();
    expect(screen.getByTestId("views-notice")).toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ the last two states
 *
 * `gates/pc-integration.md` I17. `GATES.md` G10 covers empty, no brokers, stale prices,
 * reconciliation mismatch, missing cost basis, no history, the loading skeleton and an API error.
 * The brief names two more, and neither had a gate. */

describe("permission and session states", () => {
  it("restricted: a refusal is a different sentence, and a different next step, from an outage", () => {
    /* Offering "Try again" to somebody who has lost access is a loop they cannot get out of. */
    render(
      <TooltipProvider>
        <CommandCenterScreen overview={null} unallocated={null} failure="restricted" />
      </TooltipProvider>,
    );

    const explain = screen.getByTestId("command-center-explain");
    expect(explain).toHaveTextContent(/do not have access/i);
    expect(explain).toHaveTextContent(/ask the account owner/i);
    expect(explain).not.toHaveTextContent(/Try again/);
    expect(screen.getByRole("link", { name: /signed-in account/i })).toBeInTheDocument();
  });

  it("restricted: an outage still says try again, so the two are not collapsed", () => {
    render(
      <TooltipProvider>
        <CommandCenterScreen
          overview={null}
          unallocated={null}
          failure="unreachable"
          error="The portfolio service did not answer."
        />
      </TooltipProvider>,
    );

    expect(screen.getByRole("link", { name: /Try again/ })).toBeInTheDocument();
  });

  it("market closed: says today's P&L is final rather than still moving", () => {
    render(
      <TooltipProvider>
        <CommandCenterScreen
          overview={overview()}
          unallocated={pile("180509.37")}
          marketOpen={false}
        />
      </TooltipProvider>,
    );

    expect(screen.getByTestId("market-session")).toHaveTextContent(/final for the session/);
  });

  it("market closed: an open market says the figures are still moving", () => {
    render(
      <TooltipProvider>
        <CommandCenterScreen
          overview={overview()}
          unallocated={pile("180509.37")}
          marketOpen
        />
      </TooltipProvider>,
    );

    expect(screen.getByTestId("market-session")).toHaveTextContent(/live and will keep moving/);
  });

  it("state: with no answer about the session, no session label is guessed", () => {
    /* The component takes no clock. A label invented from the browser's zone would be wrong for
       half the day, on a screen whose whole subject is which clock a figure is on. */
    renderScreen();
    expect(screen.queryByTestId("market-session")).not.toBeInTheDocument();
  });
});

describe("drilling down", () => {
  it("drill: a portfolio's name in the comparison opens its workspace", () => {
    renderScreen({ portfolios: [row(1, "Swing Manual", "1200000")] });

    const link = screen.getAllByRole("link", { name: /Swing Manual/ })[0];
    expect(link).toHaveAttribute("href", "/portfolio/1");
  });

  it("drill: an attribution row leads to the portfolio it says moved the number", () => {
    /* A row that names a cause and cannot be followed is a picture you cannot ask a question of. */
    renderScreen({
      portfolios: [row(1, "Swing Manual", "1200000"), row(2, "Long-Term Wealth", "900000")],
    });

    expect(screen.getByTestId("attribution-link-1")).toHaveAttribute("href", "/portfolio/1");
    expect(screen.getByTestId("attribution-link-2")).toHaveAttribute("href", "/portfolio/2");
  });
});
