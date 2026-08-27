import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DetailActivity } from "@/components/portfolio/detail-activity";
import { DetailHoldings } from "@/components/portfolio/detail-holdings";
import { PortfolioDetailScreen } from "@/components/portfolio/detail-screen";
import { DetailSourcePanel } from "@/components/portfolio/detail-source-panel";
import { DetailSummary } from "@/components/portfolio/detail-summary";
import {
  HISTORY_REASONS,
  NO_TARGET_WEIGHTS,
  benchmarkGap,
  driftOf,
  isBasketBacked,
  type DetailHoldingRow,
  type PortfolioSummary,
  type SourcePanel,
} from "@/lib/portfolio/detail-view";
import { MONITORING_NOTE } from "@/lib/portfolio/overview";
import type { ActivityItem, NavSeries, PortfolioDetail } from "@/lib/portfolio/overview";

/**
 * `PORTFOLIO_REDESIGN.md` §7 and the acceptance criteria it inherits, asserted as behaviour.
 *
 * These are written against the spec's sentences rather than against the markup: a return number
 * that cannot appear without its label and its start date (criterion 3), an average price that is
 * an em dash and a reason rather than a zero (§5.2/§5.3), a model figure that is never the user's
 * figure and never adjacent to it unlabelled (criterion 5), the four source panels §7 describes,
 * §4.1's monitoring sentence, and the difference between "nothing happened" and "we could not
 * ask".
 *
 * The fixtures are typed from the generated `Schemas`, so a field the router renames breaks these
 * at compile time rather than at runtime in a browser. Rates are **fractions** — `0.1842` is
 * 18.42% — because that is how `baskfy_core.portfolio_nav` stores them.
 */

const ZERODHA = { broker_account_id: 11, broker_id: "zerodha", label: "Zerodha" };
const UPSTOX = { broker_account_id: 12, broker_id: "upstox", label: "Upstox" };

function summary(overrides: Partial<PortfolioSummary> = {}): PortfolioSummary {
  return {
    portfolio_id: 1,
    name: "Momentum 30",
    kind: "CAPITAL",
    source: "SUBSCRIBED",
    source_badge: "Subscribed model by Bramha Research",
    publisher: "Bramha Research",
    started_on: "2025-04-01",
    value: "480000.00",
    cash: "2500.00",
    invested: "400000.00",
    counts_toward_total: true,
    status: "On target",
    pending_reconciliation: false,
    prices_as_of: "2026-08-21",
    holdings_synced_on: "2026-08-25",
    todays_pnl: {
      label: "Change since the previous close",
      amount: "3120.00",
      pct: "0.0065",
      since: "2026-08-20",
    },
    total_pnl: {
      label: "Total P&L since purchase",
      amount: "80000.00",
      pct: "0.2000",
    },
    headline_return: {
      kind: "TWR_SINCE_SUBSCRIBED",
      label: "TWR since you subscribed",
      since: "2025-04-01",
      value: "0.1842",
      is_model: false,
    },
    model_return: {
      kind: "TWR_SINCE_SUBSCRIBED",
      label: "Model TWR since you subscribed",
      since: "2025-04-01",
      value: "0.2130",
      is_model: true,
    },
    xirr: {
      label: "XIRR since your first cash assignment",
      since: "2025-04-01",
      value: "0.1712",
    },
    benchmark: {
      name: "Nifty 500",
      difference: "0.0470",
      portfolio: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Your TWR over this range",
        since: "2025-04-01",
        value: "0.1842",
        is_model: false,
      },
      benchmark: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Nifty 500 over this range",
        since: "2025-04-01",
        value: "0.1372",
        is_model: false,
      },
    },
    ...overrides,
  };
}

function holding(overrides: Partial<DetailHoldingRow> = {}): DetailHoldingRow {
  return {
    instrument: { instrument_id: 501, symbol: "HDFCBANK", name: "HDFC Bank" },
    broker: ZERODHA,
    quantity: "320",
    avg_price: "1420.50",
    price: "1610.00",
    value: "515200.00",
    weight: "0.1200",
    todays_contribution: "960.00",
    total_contribution: "60640.00",
    first_bought_on: "2025-04-02",
    history_source: "CAS",
    pending_reconciliation: false,
    ...overrides,
  };
}

const SUBSCRIBED_PANEL: SourcePanel = {
  source: "SUBSCRIBED",
  headline: "Subscribed model by Bramha Research",
  publisher: "Bramha Research",
  basket_slug: "momentum-30",
  basket_name: "Momentum 30",
  brokers: [ZERODHA],
  execution_note:
    "Baskfy never places an order. A rebalance produces a plan you take to your broker.",
};

const SCREEN_PANEL: SourcePanel = {
  source: "MY_SCREEN",
  headline: "Built from your screen 'High momentum, low debt'",
  screen_public_id: "scr_7Yh2",
  screen_name: "High momentum, low debt",
  brokers: [ZERODHA],
  execution_note:
    "Baskfy never places an order. A rebalance produces a plan you take to your broker.",
};

const STRATEGY_PANEL: SourcePanel = {
  source: "MY_STRATEGY",
  headline: "Driven by your saved strategy rules",
  screen_public_id: "scr_9Kp4",
  screen_name: "Quarterly rank rebalance",
  brokers: [ZERODHA],
  execution_note:
    "Baskfy never places an order. A rebalance produces a plan you take to your broker.",
};

const HOLDING_GROUP_PANEL: SourcePanel = {
  source: "HOLDING_GROUP",
  headline: "Holdings you grouped on 2026-02-14",
  grouped_on: "2026-02-14",
  brokers: [ZERODHA, UPSTOX],
  execution_note:
    "Baskfy never places an order. A rebalance produces a plan you take to your broker.",
};

const NAV: NavSeries = {
  range: "1Y",
  pending_reconciliation: false,
  from_on: "2026-08-19",
  to_on: "2026-08-21",
  total_return: { label: "TWR over this range", since: "2026-08-19", value: "0.0180" },
  points: [
    {
      on: "2026-08-19",
      value: "470000.00",
      cash: "2500.00",
      net_flow: "0.00",
      pending_reconciliation: false,
    },
    {
      on: "2026-08-20",
      value: "476000.00",
      cash: "2500.00",
      net_flow: "0.00",
      pending_reconciliation: false,
    },
    {
      on: "2026-08-21",
      value: "480000.00",
      cash: "2500.00",
      net_flow: "0.00",
      pending_reconciliation: false,
    },
  ],
  drawdown: [
    { on: "2026-08-19", index: "100.000000", peak: "100.000000", drawdown: "0.000000" },
    { on: "2026-08-20", index: "101.276596", peak: "101.276596", drawdown: "0.000000" },
    { on: "2026-08-21", index: "102.127660", peak: "102.127660", drawdown: "0.000000" },
  ],
};

function detail(overrides: Partial<PortfolioDetail> = {}): PortfolioDetail {
  return {
    summary: summary(),
    holdings: [holding()],
    source_panel: SUBSCRIBED_PANEL,
    brokers: [ZERODHA],
    ...overrides,
  };
}

/* ------------------------------------------------------------------ *
 * Criterion 3 — a return carries its label and its start date
 * ------------------------------------------------------------------ */

describe("§7 summary — every return number says what it is and when it started", () => {
  it("labels the headline metric and names its start date", () => {
    render(<DetailSummary summary={summary()} />);

    const headline = screen.getByTestId("detail-headline-return");
    // Visible: the metric's own name, so 18.42% cannot be mistaken for a different measurement.
    expect(headline).toHaveTextContent("TWR since you subscribed");
    expect(headline).toHaveTextContent("+18.42%");
    // "On hover", which is also what a screen reader announces: the start date.
    expect(headline.getAttribute("title")).toContain("TWR since you subscribed");
    expect(headline.getAttribute("title")).toContain("Measured since 1 Apr 2025");
  });

  it("labels the benchmark difference, which arrives on the wire as a bare fraction", () => {
    render(<DetailSummary summary={summary()} />);

    const gap = screen.getByTestId("detail-benchmark-gap");
    expect(gap).toHaveTextContent("Ahead of Nifty 500");
    expect(gap).toHaveTextContent("+4.70%");
    expect(gap.getAttribute("title")).toContain("Measured since 1 Apr 2025");
  });

  it("names the metric it cannot show, instead of showing a zero", () => {
    render(
      <DetailSummary
        summary={summary({
          headline_return: {
            kind: "SINCE_GROUPED",
            label: "Since grouped",
            since: "2026-02-14",
            value: null,
            is_model: false,
            unavailable_reason:
              "We have no purchase prices for these shares yet. Import your account statement to unlock it.",
          },
        })}
      />,
    );

    const headline = screen.getByTestId("detail-headline-return");
    expect(headline).toHaveTextContent("Since grouped");
    expect(headline).toHaveTextContent("—");
    expect(headline).not.toHaveTextContent("0.00%");
    expect(headline).toHaveTextContent(/Import your account statement/);
  });

  it("keeps the price date and the sync time as two separate clocks", () => {
    render(<DetailSummary summary={summary()} />);

    const line = screen.getByTestId("detail-timestamps");
    expect(line).toHaveTextContent("Valued at close of 21 Aug 2026");
    expect(line).toHaveTextContent("Holdings synced: 25 Aug 2026");
  });

  it("says an invested amount is missing, and why, rather than printing zero", () => {
    render(
      <DetailSummary
        summary={summary({
          invested: null,
          invested_unavailable_reason: "No purchase prices on record yet",
        })}
      />,
    );

    const invested = screen.getByTestId("detail-invested");
    expect(invested).toHaveTextContent("—");
    expect(invested).toHaveTextContent("No purchase prices on record yet");
    expect(invested).not.toHaveTextContent("₹0");
  });
});

/* ------------------------------------------------------------------ *
 * Criterion 5 — the model's record is never the user's
 * ------------------------------------------------------------------ */

describe("§7 summary — model and actual are two figures, never one", () => {
  it("renders them as two separately labelled numbers in two separate panels", () => {
    render(<DetailSummary summary={summary()} />);

    const yours = screen.getByTestId("detail-your-return");
    const model = screen.getByTestId("detail-model-return-panel");
    expect(yours).not.toContainElement(model);

    const actualFigure = within(yours).getByTestId("detail-headline-return");
    const modelFigure = within(model).getByTestId("detail-model-return");

    expect(actualFigure).toHaveAttribute("data-model", "false");
    expect(modelFigure).toHaveAttribute("data-model", "true");

    expect(actualFigure).toHaveTextContent("TWR since you subscribed");
    expect(modelFigure).toHaveTextContent("Model TWR since you subscribed");
    expect(modelFigure).toHaveTextContent("model, not yours");

    // The two values are both present and neither is a blend of the other.
    expect(actualFigure).toHaveTextContent("+18.42%");
    expect(modelFigure).toHaveTextContent("+21.30%");
  });

  it("does not draw a model panel when the publisher has no record", () => {
    render(<DetailSummary summary={summary({ model_return: null })} />);
    expect(screen.queryByTestId("detail-model-return-panel")).toBeNull();
    expect(screen.getByTestId("detail-headline-return")).toHaveAttribute("data-model", "false");
  });

  it("keeps the source panel's model-vs-actual as two labelled figures too", () => {
    render(<DetailSourcePanel panel={SUBSCRIBED_PANEL} summary={summary()} />);

    const comparison = screen.getByTestId("detail-model-vs-actual");
    const actual = within(comparison).getByTestId("detail-actual-figure");
    const model = within(comparison).getByTestId("detail-model-figure");

    expect(actual).toHaveAttribute("data-model", "false");
    expect(model).toHaveAttribute("data-model", "true");
    expect(actual).toHaveTextContent("TWR since you subscribed");
    expect(model).toHaveTextContent("Model TWR since you subscribed");
    expect(comparison).toHaveTextContent(/never one/i);
  });
});

/* ------------------------------------------------------------------ *
 * §4.1 — a monitoring view says so
 * ------------------------------------------------------------------ */

describe("§4.1 — a monitoring view carries its label", () => {
  it("prints the API's own sentence", () => {
    render(
      <DetailSummary
        summary={summary({
          kind: "MONITORING",
          counts_toward_total: false,
          excluded_note: MONITORING_NOTE,
        })}
      />,
    );

    expect(screen.getByTestId("detail-monitoring-note")).toHaveTextContent(
      "Monitoring view — overlaps with other portfolios, excluded from totals.",
    );
  });

  it("falls back to the spec's sentence when an older payload omits the note", () => {
    render(<DetailSummary summary={summary({ kind: "MONITORING", counts_toward_total: false })} />);
    expect(screen.getByTestId("detail-monitoring-note")).toHaveTextContent(MONITORING_NOTE);
  });

  it("says nothing of the sort on a capital portfolio", () => {
    render(<DetailSummary summary={summary()} />);
    expect(screen.queryByTestId("detail-monitoring-note")).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * §5.2 / §5.3 — an unknown average price is a reason, never a zero
 * ------------------------------------------------------------------ */

describe("§7 holdings — an unknown average price", () => {
  it("renders an em dash and the reason, and never a zero", () => {
    render(
      <DetailHoldings
        holdings={[
          holding({
            avg_price: null,
            total_contribution: null,
            first_bought_on: null,
            history_source: "NONE",
          }),
        ]}
      />,
    );

    const cell = screen.getByTestId("detail-avg-price-HDFCBANK");
    expect(cell).toHaveTextContent("—");
    expect(cell.textContent).not.toMatch(/0\.00/);
    expect(cell.textContent).not.toMatch(/₹\s*0/);
    // The reason travels with the cell: on `title` for a mouse, in the DOM for a screen reader.
    expect(cell.querySelector("[title]")?.getAttribute("title")).toBe(HISTORY_REASONS.NONE);
    expect(within(cell).getByText(HISTORY_REASONS.NONE ?? "")).toBeInTheDocument();

    // And once more under the table, where a reader who is not hovering will find it.
    expect(screen.getByTestId("detail-holdings-footnote")).toHaveTextContent(
      /No purchase price has ever been recorded/,
    );
  });

  it("picks the reason that applies, not one generic sentence", () => {
    render(
      <DetailHoldings
        holdings={[
          holding({ avg_price: null, history_source: "BROKER" }),
          holding({
            instrument: { instrument_id: 502, symbol: "INFY", name: "Infosys" },
            avg_price: null,
            history_source: "MANUAL",
          }),
        ]}
      />,
    );

    const footnote = screen.getByTestId("detail-holdings-footnote");
    expect(footnote).toHaveTextContent(/Your broker did not send a purchase price/);
    expect(footnote).toHaveTextContent(/entered by hand/);
  });

  it("suppresses profit since purchase when there is no purchase price (§5.2)", () => {
    render(
      <DetailHoldings
        holdings={[holding({ avg_price: null, total_contribution: null, history_source: "NONE" })]}
      />,
    );

    const cell = screen.getByTestId("detail-total-contribution-HDFCBANK");
    expect(cell).toHaveTextContent("—");
    expect(cell.textContent).not.toMatch(/₹\s*0/);
  });

  it("shows the average price, and the date it starts from, when it is known", () => {
    render(<DetailHoldings holdings={[holding()]} />);

    const cell = screen.getByTestId("detail-avg-price-HDFCBANK");
    expect(cell).toHaveTextContent("₹1,420.50");
    expect(cell).toHaveTextContent("from 2 Apr 2025");
    expect(screen.queryByTestId("detail-holdings-footnote")).toBeNull();
  });
});

/* ------------------------------------------------------------------ *
 * §7 — target weight and drift on a basket-backed portfolio
 * ------------------------------------------------------------------ */

describe("§7 holdings — target weight and drift", () => {
  it("adds the two columns for a basket-backed portfolio only", () => {
    const { rerender } = render(<DetailHoldings holdings={[holding()]} basketBacked />);
    expect(screen.getByRole("columnheader", { name: "Target weight" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Drift" })).toBeInTheDocument();

    rerender(<DetailHoldings holdings={[holding()]} />);
    expect(screen.queryByRole("columnheader", { name: "Target weight" })).toBeNull();
  });

  it("shows the drift when the payload carries a target", () => {
    render(
      <DetailHoldings holdings={[holding({ weight: "0.1200", target_weight: "0.1000" })]} basketBacked />,
    );

    expect(screen.getByTestId("detail-target-HDFCBANK")).toHaveTextContent("10.00%");
    expect(screen.getByTestId("detail-drift-HDFCBANK")).toHaveTextContent("+2.00%");
    expect(screen.queryByTestId("detail-no-targets")).toBeNull();
  });

  it("says the targets are missing rather than inventing a drift of zero", () => {
    render(<DetailHoldings holdings={[holding()]} basketBacked />);

    expect(screen.getByTestId("detail-target-HDFCBANK")).toHaveTextContent("—");
    expect(screen.getByTestId("detail-drift-HDFCBANK")).toHaveTextContent("—");
    expect(screen.getByTestId("detail-drift-HDFCBANK").textContent).not.toMatch(/0\.00%/);
    expect(screen.getByTestId("detail-no-targets")).toHaveTextContent(NO_TARGET_WEIGHTS);
  });

  it("computes drift only when both sides are known", () => {
    expect(driftOf(holding({ weight: "0.1200", target_weight: "0.1000" }))).toBeCloseTo(0.02, 6);
    expect(driftOf(holding({ weight: "0.1200" }))).toBeNull();
    expect(driftOf(holding({ weight: null, target_weight: "0.1000" }))).toBeNull();
  });

  it("calls a portfolio basket-backed only when a model is actually linked", () => {
    expect(isBasketBacked(SUBSCRIBED_PANEL)).toBe(true);
    expect(isBasketBacked(HOLDING_GROUP_PANEL)).toBe(false);
    expect(isBasketBacked({ ...SUBSCRIBED_PANEL, basket_slug: null, basket_name: null })).toBe(
      false,
    );
  });
});

/* ------------------------------------------------------------------ *
 * §7 — the source panel, all four of §3's sources
 * ------------------------------------------------------------------ */

describe("§7 source panel — one panel per §3 source", () => {
  it("subscribed: publisher, methodology, model-vs-actual, and what a rebalance produces", () => {
    render(<DetailSourcePanel panel={SUBSCRIBED_PANEL} summary={summary()} />);

    const panel = screen.getByTestId("detail-source-panel");
    expect(panel).toHaveAttribute("data-source", "SUBSCRIBED");
    expect(screen.getByTestId("detail-source-subscribed")).toBeInTheDocument();
    expect(screen.getByTestId("detail-source-headline")).toHaveTextContent(
      "Subscribed model by Bramha Research",
    );
    expect(panel).toHaveTextContent("Bramha Research");
    expect(screen.getByTestId("detail-methodology-link")).toHaveAttribute(
      "href",
      "/basket/momentum-30",
    );
    expect(screen.getByTestId("detail-model-vs-actual")).toBeInTheDocument();
    expect(screen.getByTestId("detail-execution-note")).toHaveTextContent(
      /never places an order/i,
    );
  });

  it("subscribed: never says managed, advisory or PMS (§9)", () => {
    render(<DetailSourcePanel panel={SUBSCRIBED_PANEL} summary={summary()} />);
    const text = screen.getByTestId("detail-source-panel").textContent ?? "";
    expect(text).not.toMatch(/\bmanaged\b/i);
    expect(text).not.toMatch(/\badvisory\b/i);
    expect(text).not.toMatch(/\bPMS\b/);
    expect(text).toMatch(/Subscribed model by Bramha Research/);
  });

  it("my screen: the rules, and where its entries and exits are", () => {
    render(<DetailSourcePanel panel={SCREEN_PANEL} summary={summary({ source: "MY_SCREEN" })} />);

    expect(screen.getByTestId("detail-source-panel")).toHaveAttribute("data-source", "MY_SCREEN");
    expect(screen.getByTestId("detail-source-rules")).toBeInTheDocument();
    expect(screen.getByTestId("detail-rules-link")).toHaveAttribute("href", "/build/scr_7Yh2");
    expect(screen.getByTestId("detail-rules-link")).toHaveTextContent("High momentum, low debt");
    expect(screen.getByTestId("detail-source-rules")).toHaveTextContent(/entry and exit/i);
    expect(screen.queryByTestId("detail-model-vs-actual")).toBeNull();
  });

  it("my strategy: the same panel, driven by the saved rules", () => {
    render(
      <DetailSourcePanel panel={STRATEGY_PANEL} summary={summary({ source: "MY_STRATEGY" })} />,
    );

    expect(screen.getByTestId("detail-source-panel")).toHaveAttribute("data-source", "MY_STRATEGY");
    expect(screen.getByTestId("detail-source-headline")).toHaveTextContent(
      "Driven by your saved strategy rules",
    );
    expect(screen.getByTestId("detail-rules-link")).toHaveAttribute("href", "/build/scr_9Kp4");
  });

  it("holding group: the included brokers and the date it was grouped on", () => {
    render(
      <DetailSourcePanel
        panel={HOLDING_GROUP_PANEL}
        summary={summary({ source: "HOLDING_GROUP", model_return: null })}
      />,
    );

    expect(screen.getByTestId("detail-source-panel")).toHaveAttribute(
      "data-source",
      "HOLDING_GROUP",
    );
    const body = screen.getByTestId("detail-source-holding-group");
    expect(body).toHaveTextContent("Zerodha");
    expect(body).toHaveTextContent("Upstox");
    expect(screen.getByTestId("detail-grouped-on")).toHaveTextContent("14 Feb 2026");
    expect(screen.queryByTestId("detail-model-vs-actual")).toBeNull();
  });

  it("says so rather than linking nowhere when the model or the rules are not linked", () => {
    render(
      <DetailSourcePanel
        panel={{ ...SUBSCRIBED_PANEL, basket_slug: null, basket_name: null }}
        summary={summary()}
      />,
    );
    expect(screen.queryByTestId("detail-methodology-link")).toBeNull();
    expect(screen.getByTestId("detail-source-subscribed")).toHaveTextContent(/is not linked yet/);
  });
});

/* ------------------------------------------------------------------ *
 * §7 activity, and criterion 6
 * ------------------------------------------------------------------ */

describe("§7 activity", () => {
  const items: ActivityItem[] = [
    {
      on: "2026-08-20",
      kind: "BUY",
      description: "Bought 20 HDFC Bank",
      amount: "-32200.00",
      quantity: "20",
      is_pnl_event: true,
      broker: ZERODHA,
    },
    {
      on: "2026-08-18",
      kind: "ASSIGN",
      description: "Assigned cash to this portfolio",
      amount: "50000.00",
      is_pnl_event: true,
    },
    {
      on: "2026-08-14",
      kind: "CORPORATE_ACTION",
      description: "1:1 bonus in Infosys",
      quantity: "60",
      is_pnl_event: false,
    },
    {
      on: "2026-08-10",
      kind: "RECONCILIATION",
      description: "A sell of 100 HDFC Bank needs a portfolio",
      is_pnl_event: false,
      reconciliation_state: "OPEN",
    },
  ];

  it("lists what happened, newest first, with kinds named", () => {
    render(<DetailActivity items={items} />);

    const feed = screen.getByTestId("detail-activity");
    expect(within(feed).getAllByRole("listitem")).toHaveLength(4);
    expect(feed).toHaveTextContent("Bought 20 HDFC Bank");
    expect(feed).toHaveTextContent("Cash assigned");
    expect(feed).toHaveTextContent("Corporate action");
    expect(feed).toHaveTextContent("Reconciliation");
  });

  it("says a corporate action produced no profit or loss (§4.5, criterion 6)", () => {
    render(<DetailActivity items={items} />);

    const corporate = screen.getByTestId("detail-activity-CORPORATE_ACTION");
    expect(corporate).toHaveTextContent("No profit or loss comes from this.");
    expect(corporate).toHaveTextContent("—");
  });

  it("marks a reconciliation entry as bookkeeping and shows its state", () => {
    render(<DetailActivity items={items} />);

    const row = screen.getByTestId("detail-activity-RECONCILIATION");
    expect(row).toHaveTextContent("Waiting on your answer");
    expect(row).toHaveTextContent("No profit or loss comes from this.");
  });
});

/* ------------------------------------------------------------------ *
 * Empty, and missing, are two different things
 * ------------------------------------------------------------------ */

describe("§7 — an absence is reported, never rendered as an empty success", () => {
  it("distinguishes an empty portfolio from a holdings read that failed", () => {
    const { rerender } = render(<DetailHoldings holdings={[]} />);
    expect(screen.getByTestId("detail-holdings-empty")).toHaveTextContent(
      /Nothing is allocated to this portfolio yet/,
    );

    rerender(<DetailHoldings holdings={null} unavailableReason="The API did not answer." />);
    expect(screen.queryByTestId("detail-holdings-empty")).toBeNull();
    expect(screen.getByTestId("detail-holdings-unavailable")).toHaveTextContent(
      "The API did not answer.",
    );
  });

  it("distinguishes a quiet portfolio from an activity read that failed", () => {
    const { rerender } = render(<DetailActivity items={[]} />);
    expect(screen.getByTestId("detail-activity-empty")).toHaveTextContent(/Nothing has happened/);

    rerender(<DetailActivity items={null} unavailableReason="The activity feed did not load." />);
    expect(screen.queryByTestId("detail-activity-empty")).toBeNull();
    expect(screen.getByTestId("detail-activity-unavailable")).toHaveTextContent(
      "The activity feed did not load.",
    );
  });

  it("says the chart is missing rather than drawing an empty one", () => {
    render(
      <PortfolioDetailScreen
        detail={detail()}
        nav={null}
        activity={[]}
        failures={{ nav: "The end-of-day valuation series did not load." }}
        loadRange={vi.fn()}
      />,
    );

    expect(screen.getByTestId("detail-nav-unavailable")).toHaveTextContent(
      "The end-of-day valuation series did not load.",
    );
    expect(screen.queryByTestId("combined-chart")).toBeNull();
  });

  it("reports a benchmark it cannot compare against instead of a zero difference", () => {
    render(
      <DetailSummary
        summary={summary({
          benchmark: {
            name: "Nifty 500",
            difference: null,
            portfolio: {
              kind: "TWR_SINCE_SUBSCRIBED",
              label: "Your TWR over this range",
              since: "2025-04-01",
              value: "0.1842",
              is_model: false,
            },
            benchmark: {
              kind: "TWR_SINCE_SUBSCRIBED",
              label: "Nifty 500 over this range",
              since: "2025-04-01",
              value: null,
              is_model: false,
            },
          },
        })}
      />,
    );

    const gap = screen.getByTestId("detail-benchmark-gap");
    expect(gap).toHaveTextContent("—");
    expect(gap).toHaveTextContent(/not enough overlap with Nifty 500/);
    expect(gap.textContent).not.toMatch(/0\.00%/);
  });

  it("keeps the gap unavailable in the helper too, so no component can print a bare zero", () => {
    const withoutDifference = benchmarkGap({
      name: "Nifty 500",
      difference: null,
      portfolio: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Yours",
        since: "2025-04-01",
        value: "0.18",
        is_model: false,
      },
      benchmark: {
        kind: "TWR_SINCE_SUBSCRIBED",
        label: "Nifty 500",
        since: "2025-04-01",
        value: null,
        is_model: false,
      },
    });
    expect(withoutDifference.value).toBeNull();
    expect(withoutDifference.unavailableReason).toMatch(/not enough overlap/);
    expect(withoutDifference.since).toBe("2025-04-01");
  });
});

/* ------------------------------------------------------------------ *
 * The page, assembled
 * ------------------------------------------------------------------ */

describe("§7 — the five blocks, on one page", () => {
  it("renders summary, performance, holdings, activity and the source panel", () => {
    render(
      <PortfolioDetailScreen
        detail={detail()}
        nav={NAV}
        activity={[]}
        loadRange={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { level: 1, name: "Momentum 30" })).toBeInTheDocument();
    expect(screen.getByTestId("detail-summary")).toBeInTheDocument();
    expect(screen.getByTestId("combined-chart")).toBeInTheDocument();
    expect(screen.getByTestId("detail-holdings")).toBeInTheDocument();
    expect(screen.getByTestId("detail-activity-empty")).toBeInTheDocument();
    expect(screen.getByTestId("detail-source-panel")).toBeInTheDocument();
    expect(screen.getByTestId("source-badge")).toHaveTextContent(
      "Subscribed model by Bramha Research",
    );
  });

  it("turns on §7's extra columns from the source panel, not from a guess", () => {
    const { rerender } = render(
      <PortfolioDetailScreen detail={detail()} nav={NAV} activity={[]} loadRange={vi.fn()} />,
    );
    expect(screen.getByRole("columnheader", { name: "Target weight" })).toBeInTheDocument();

    rerender(
      <PortfolioDetailScreen
        detail={detail({
          source_panel: HOLDING_GROUP_PANEL,
          summary: summary({ source: "HOLDING_GROUP", model_return: null }),
        })}
        nav={NAV}
        activity={[]}
        loadRange={vi.fn()}
      />,
    );
    expect(screen.queryByRole("columnheader", { name: "Target weight" })).toBeNull();
  });

  it("carries the monitoring sentence onto the assembled page", () => {
    render(
      <PortfolioDetailScreen
        detail={detail({
          summary: summary({
            kind: "MONITORING",
            counts_toward_total: false,
            excluded_note: MONITORING_NOTE,
          }),
        })}
        nav={NAV}
        activity={[]}
        loadRange={vi.fn()}
      />,
    );

    expect(screen.getByTestId("detail-monitoring-note")).toHaveTextContent(MONITORING_NOTE);
  });
});
