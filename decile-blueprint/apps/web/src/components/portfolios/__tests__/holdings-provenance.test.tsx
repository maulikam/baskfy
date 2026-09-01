import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HoldingsProvenance } from "@/components/portfolios/holdings-provenance";
import { describeProvenance, type SyncHoldingsOut } from "@/lib/portfolios/provenance";

function result(partial: Partial<SyncHoldingsOut> = {}): SyncHoldingsOut {
  return {
    broker_id: "zerodha",
    // M76 added the persistence fields; a fixture that omits them no longer matches the response.
    persisted: false,
    written: 0,
    portfolio_id: null,
    unresolved: [],
    sync_note: "",
    degraded: false,
    dry_run: true,
    holdings: [],
    note: "",
    source: "live",
    ...partial,
  };
}

describe("fixture holdings are visibly marked on screen", () => {
  it("flags a fixture as not the reader's holdings", () => {
    render(<HoldingsProvenance view={describeProvenance(result({ source: "fixture" }), "Zerodha")} />);
    const panel = screen.getByTestId("holdings-provenance");
    expect(panel).toHaveAttribute("data-source", "fixture");
    expect(panel).toHaveAttribute("data-marked", "true");
    expect(screen.getByTestId("provenance-warning")).toHaveTextContent("Not your holdings");
    expect(screen.getByText("Sample data")).toBeInTheDocument();
  });

  it("flags a degraded read as incomplete rather than as fake", () => {
    render(
      <HoldingsProvenance
        view={describeProvenance(result({ source: "live", degraded: true }), "Zerodha")}
      />,
    );
    expect(screen.getByTestId("holdings-provenance")).toHaveAttribute("data-degraded", "true");
    expect(screen.getByTestId("provenance-warning")).toHaveTextContent("Incomplete");
  });

  it("flags an unwired broker as unable to report anything", () => {
    render(
      <HoldingsProvenance
        view={describeProvenance(result({ broker_id: "kotak", source: "unwired" }), "Kotak Neo")}
      />,
    );
    expect(screen.getByText("No adapter yet")).toBeInTheDocument();
    expect(screen.getByTestId("holdings-provenance")).toHaveAttribute("data-marked", "true");
  });
});

describe("live holdings provenance carries no fixture marking", () => {
  it("puts no warning on a clean live read", () => {
    render(<HoldingsProvenance view={describeProvenance(result({ source: "live" }), "Zerodha")} />);
    const panel = screen.getByTestId("holdings-provenance");
    expect(panel).toHaveAttribute("data-source", "live");
    expect(panel).toHaveAttribute("data-marked", "false");
    expect(screen.queryByTestId("provenance-warning")).not.toBeInTheDocument();
    expect(panel).not.toHaveTextContent(/fixture/i);
    expect(panel).not.toHaveTextContent(/sample/i);
  });

  it("says in words that the rows are the reader's own", () => {
    render(<HoldingsProvenance view={describeProvenance(result({ source: "live" }), "Zerodha")} />);
    expect(screen.getByText(/your own holdings/)).toBeInTheDocument();
    expect(screen.getByText("Live from Zerodha")).toBeInTheDocument();
  });

  it("puts no warning on an honest empty answer either", () => {
    render(<HoldingsProvenance view={describeProvenance(result({ source: "empty" }), "Zerodha")} />);
    expect(screen.getByTestId("holdings-provenance")).toHaveAttribute("data-marked", "false");
  });
});
