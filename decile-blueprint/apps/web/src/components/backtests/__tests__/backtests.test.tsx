import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AssumptionsPanel } from "@/components/backtests/assumptions-panel";
import { DrawdownChart } from "@/components/backtests/drawdown-chart";
import { EquityChart } from "@/components/backtests/equity-chart";
import { FragilityPanel } from "@/components/backtests/fragility-panel";
import { TradeLog } from "@/components/backtests/trade-log";

/**
 * The four surfaces docs/10 §"What to show the user (honesty features)" and docs/08 §Backtests
 * name by hand: the equity curve against the benchmark, the drawdown chart, the fragility spread,
 * and the assumptions panel with its disclaimer.
 *
 * The assertions are about *honesty*, not layout: that the disclaimer is rendered rather than
 * assumed, that a delisted fill is visibly marked rather than blended into the log, and that a
 * one-day run says why it cannot draw a line instead of drawing a misleading one.
 */
describe("the equity chart", () => {
  const points = [
    { date: "2020-01-01", equity: "1000000", benchmark: "10000" },
    { date: "2020-06-01", equity: "1100000", benchmark: "10500" },
    { date: "2020-12-01", equity: "1250000", benchmark: "11000" },
  ];

  it("rebases both series to 100 and says so", () => {
    render(<EquityChart points={points} benchmarkLabel="NIFTY 500" />);
    // Twice on purpose: once as the caption a sighted reader sees, once inside the SVG's
    // accessible title, which is the only description a screen reader gets.
    expect(screen.getAllByText(/rebased to 100/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/NIFTY 500/).length).toBeGreaterThanOrEqual(1);
  });

  it("describes the run in the accessible title", () => {
    render(<EquityChart points={points} benchmarkLabel="NIFTY 500" />);
    const image = screen.getByRole("img");
    expect(image.getAttribute("aria-labelledby")).toBeTruthy();
    expect(image.textContent).toContain("125.0");
  });

  it("refuses to draw a line through one point", () => {
    render(<EquityChart points={[points[0]!]} benchmarkLabel="NIFTY 500" />);
    expect(screen.getByText(/a line needs two points/i)).toBeTruthy();
  });
});

describe("the drawdown chart", () => {
  it("reports the deepest fall as a number as well as a shape", () => {
    render(
      <DrawdownChart
        points={[
          { date: "2020-01-01", drawdown: 0 },
          { date: "2020-02-01", drawdown: -0.1234 },
          { date: "2020-03-01", drawdown: -0.05 },
        ]}
      />,
    );
    expect(screen.getByText(/-12\.34%/)).toBeTruthy();
  });
});

describe("the fragility readout", () => {
  const runs = [
    { label: "base", description: "the configuration as submitted", cagr: 0.18, total_return: 1.2, max_drawdown: -0.3, final_equity: "2200000", trades: 400, blind_pct: 0, rebalances: 57 },
    { label: "costs_plus_25pct", description: "every cost component +25%", cagr: 0.15, total_return: 1.0, max_drawdown: -0.31, final_equity: "2000000", trades: 400, blind_pct: 0, rebalances: 57 },
  ];

  it("states the spread in words, not only in a table", () => {
    render(<FragilityPanel runs={runs} />);
    expect(screen.getByText(/CAGR spans/i)).toBeTruthy();
  });

  it("says plainly when the probe was not run", () => {
    render(<FragilityPanel runs={[]} />);
    expect(screen.getByText(/without the fragility probe/i)).toBeTruthy();
  });

  /**
   * M45.7. A variant whose screen returned nothing also diverges wildly from the base run — the
   * engine picks no names and the book sits in cash — and this panel rendered that as a wide CAGR
   * spread. docs/10 primes the reader to expect exactly that ("Most won't [survive]. That is the
   * point."), so a hole in the data was camouflaged as the documented finding.
   *
   * Measured against the live database on 2026-08-23: of the rebalance dates the coverage guard
   * passes, 100% have BOTH offsets blind, because factor_daily and index_member_daily are weekly
   * series. Every offset variant this product can currently produce is in this state.
   */
  it("does not let a variant that saw nothing into the spread", () => {
    const withBlind = [
      ...runs,
      { label: "offset_minus_1", description: "every rebalance -1 trading day", cagr: -0.4, total_return: -0.9, max_drawdown: -0.9, final_equity: "100000", trades: 0, blind_pct: 100, rebalances: 57 },
    ];
    render(<FragilityPanel runs={withBlind} />);

    // The number reaches a person, which it never did before this.
    expect(screen.getByText(/decided with an empty screen/i)).toBeTruthy();
    expect(screen.getByText(/not comparable/i)).toBeTruthy();
    // And the spread is still the one over the two runs that saw a full screen: 18% - 15% = 3pp.
    // Including the blind variant would have made it 58pp and called the strategy fragile.
    expect(screen.getByText(/Across the 2 runs that saw a full screen/i)).toBeTruthy();
    expect(screen.getByText(/3\.00%/)).toBeTruthy();
  });

  it("refuses to state a spread when nothing is comparable", () => {
    const allBlind = runs.map((run) => ({ ...run, blind_pct: 100 }));
    render(<FragilityPanel runs={allBlind} />);
    expect(screen.getByText(/no honest spread to state/i)).toBeTruthy();
    expect(screen.queryByText(/CAGR spans/i)).toBeNull();
  });
});

describe("the assumptions panel", () => {
  it("renders the server's disclaimer verbatim", () => {
    render(
      <AssumptionsPanel
        assumptions={["Orders fill at the NEXT trading day's open."]}
        disclaimer="Past backtest results do not predict future results."
      />,
    );
    expect(
      screen.getByText("Past backtest results do not predict future results."),
    ).toBeTruthy();
    expect(screen.getByText(/NEXT trading day/)).toBeTruthy();
  });
});

describe("the trade log", () => {
  it("marks a forced delisting rather than blending it in", () => {
    render(
      <TradeLog
        loading={false}
        truncated={false}
        trades={[
          {
            date: "2021-04-01",
            symbol: "DEADCO",
            side: "sell",
            quantity: 120,
            price: "12.5000",
            notional: "1500.00",
            cost: "4.20",
            reason: "delist",
            realised_pnl: "-40000.00",
          },
        ]}
      />,
    );
    expect(screen.getByText("Delisted")).toBeTruthy();
    expect(screen.getByText("DEADCO")).toBeTruthy();
  });

  it("explains an empty log instead of showing a blank table", () => {
    render(<TradeLog loading={false} truncated={false} trades={[]} />);
    expect(screen.getByText(/never traded/i)).toBeTruthy();
  });
});
