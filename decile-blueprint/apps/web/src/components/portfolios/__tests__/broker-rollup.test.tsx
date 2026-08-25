import type { PortfolioRollupOut } from "@baskfy/api-client";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BrokerRollup } from "@/components/portfolios/broker-rollup";
import { consolidate, type BrokerListOut } from "@/lib/portfolios/rollup";

function broker(
  id: string,
  name: string,
  holdingsSync: string,
): BrokerListOut["brokers"][number] {
  return {
    id,
    name,
    short_name: name,
    mark: name.slice(0, 1),
    color: "#000000",
    blurb: "",
    api_name: "",
    docs_url: "",
    sort_order: 1,
    connected: holdingsSync === "ready",
    connection_status: "not_connected",
    adapter_wired: holdingsSync === "ready",
    capabilities: { oauth: "planned", holdings_sync: holdingsSync, trading: "planned" },
  };
}

const CATALOG: BrokerListOut = {
  adapters_wired: 1,
  gate: {
    decision_reference: "D3",
    live_oauth_enabled: false,
    requirement: "counsel sign-off",
    signed_off: false,
  },
  brokers: [
    broker("zerodha", "Zerodha", "ready"),
    broker("kotak", "Kotak Neo", "planned"),
    broker("icici", "ICICI Direct", "planned"),
  ],
};

function rollup(partial: Partial<PortfolioRollupOut> = {}): PortfolioRollupOut {
  return {
    portfolio_id: 1,
    by_broker: [
      {
        broker_account_id: 7,
        broker_id: "zerodha",
        label: "Zerodha · main",
        totals: { cost: "100000.5000", quantity: "150", holdings: 3 },
      },
      {
        broker_account_id: 8,
        broker_id: "zerodha",
        label: "Zerodha · family",
        totals: { cost: "50000.2500", quantity: "40", holdings: 1 },
      },
    ],
    declaration_conflicts: false,
    rows: [],
    spans_brokers: true,
    subtree_portfolio_ids: [1, 2, 3],
    total: { cost: "150000.7500", quantity: "190", holdings: 4 },
    unattributed: { cost: "0", quantity: "0", holdings: 0 },
    ...partial,
  };
}

describe("the consolidated view shows each broker's share and the total", () => {
  it("renders one line per broker account, biggest first", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: CATALOG })} />);
    const lines = screen.getAllByTestId("broker-line");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toHaveTextContent("Zerodha · main");
    expect(lines[0]).toHaveTextContent("₹1,00,000.50");
    expect(lines[1]).toHaveTextContent("Zerodha · family");
  });

  it("renders the total the API sent rather than one the browser re-derived", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: CATALOG })} />);
    expect(screen.getByTestId("broker-total")).toHaveTextContent("₹1,50,000.75");
  });

  it("shows unit counts beside the rupee amounts on every line", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: CATALOG })} />);
    expect(screen.getByRole("columnheader", { name: "Units" })).toBeInTheDocument();
    const lines = screen.getAllByTestId("broker-line");
    expect(lines[0]).toHaveTextContent("150");
    expect(screen.getByTestId("broker-total")).toHaveTextContent("190");
  });
});

describe("the consolidated view never presents a total that quietly omits a broker", () => {
  it("names the brokers that cannot sync holdings", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: CATALOG })} />);
    const coverage = screen.getByTestId("rollup-coverage");
    expect(coverage).toHaveTextContent("Kotak Neo");
    expect(coverage).toHaveTextContent("ICICI Direct");
    expect(coverage).toHaveTextContent(/cannot/);

    const badges = within(screen.getByTestId("rollup-cannot-sync")).getAllByRole("listitem");
    expect(badges).toHaveLength(2);
  });

  it("names the brokers that did contribute, so the figure is checkable", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: CATALOG })} />);
    expect(screen.getByTestId("rollup-coverage")).toHaveTextContent(
      /Filed under 2 broker accounts: Zerodha · main and Zerodha · family/,
    );
  });

  it("says the total is money put in and not a valuation", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: CATALOG })} />);
    expect(screen.getByTestId("rollup-coverage")).toHaveTextContent(/not what it is worth today/);
  });

  it("says coverage is unknown when the broker catalog could not be read", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: null })} />);
    expect(screen.getByTestId("rollup-coverage")).toHaveTextContent(/could not be read/);
    expect(screen.queryByTestId("rollup-cannot-sync")).not.toBeInTheDocument();
  });
});

describe("the panel refuses to present arithmetic it cannot reconcile", () => {
  it("says so loudly when the lines do not add up to the total", () => {
    const view = consolidate({
      rollup: rollup({ total: { cost: "999999.0000", quantity: "190", holdings: 4 } }),
      catalog: CATALOG,
    });
    render(<BrokerRollup view={view} />);
    const warning = screen.getByTestId("rollup-invariant-broken");
    expect(warning).toHaveTextContent("₹1,50,000.75");
    expect(warning).toHaveTextContent("₹9,99,999");
    expect(warning).toHaveTextContent(/unreliable/);
  });

  it("shows no warning when it does reconcile", () => {
    render(<BrokerRollup view={consolidate({ rollup: rollup(), catalog: CATALOG })} />);
    expect(screen.queryByTestId("rollup-invariant-broken")).not.toBeInTheDocument();
  });

  it("reports a declaration conflict without rewriting either side", () => {
    const view = consolidate({
      rollup: rollup({ declaration_conflicts: true, declared_broker_account_id: 7 }),
      catalog: CATALOG,
    });
    render(<BrokerRollup view={view} />);
    expect(screen.getByTestId("rollup-declaration-conflict")).toHaveTextContent(
      /Neither side has been rewritten/,
    );
  });
});

describe("holdings with no broker account are shown, not folded away", () => {
  it("gives them their own line and labels them honestly", () => {
    const view = consolidate({
      rollup: rollup({
        unattributed: { cost: "25.2500", quantity: "2", holdings: 1 },
        total: { cost: "150026.0000", quantity: "192", holdings: 5 },
      }),
      catalog: CATALOG,
    });
    render(<BrokerRollup view={view} />);
    const lines = screen.getAllByTestId("broker-line");
    const orphan = lines.find((line) => line.getAttribute("data-unattributed") === "true");
    expect(orphan).toBeDefined();
    expect(orphan).toHaveTextContent("Not attributed to a broker");
    expect(orphan).toHaveTextContent("₹25.25");
  });
});
