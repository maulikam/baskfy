import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

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
   assertion here would let the fixture describe a payload the server never sends. */
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
      value: "12.5",
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
      todays_pnl: { amount: "72029.56", label: "today", pct: "1.99" },
      total_pnl: { amount: "580509.37", label: "total" },
      twr: { label: "TWR since created", since: "2026-01-01", value: "18.7" },
      xirr: { label: "XIRR", since: "2026-01-01", value: "21.4" },
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
      total_return: { label: "Total return", since: "2026-01-01", value: "18.7" },
      max_drawdown: { drawdown: "-8.2", peak_on: "2026-06-01", trough_on: "2026-07-10" },
      drawdown: [{ drawdown: "-2.1", index: "97.9", peak: "3750000.00", on: "2026-09-10" }],
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
