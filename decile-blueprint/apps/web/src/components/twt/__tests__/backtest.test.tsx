import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BacktestCard } from "@/components/twt/backtest-card";
import { backtest, backtestRun } from "@/lib/twt/__tests__/fixtures";

/**
 * `docs/twt/05` §3, and the two things it is emphatic about: the caveats come **before** the
 * numbers, and the two sources are never mixed.
 */

describe("the twt backtest card puts the conditions before the result", () => {
  /**
   * House rule 9's second half — a disclaimer is a component, not a footer — and `05` §3 restates
   * it for this page by name. Asserted by **document order**, not by presence: a caveat block
   * that exists at the bottom of the page satisfies "is present" and fails the actual rule, which
   * is that a reader must meet it before they meet the number it qualifies.
   */
  it("renders the caveats as a component above every figure", () => {
    render(<BacktestCard backtest={backtest()} />);

    const caveats = screen.getByTestId("twt-caveats");
    /* The annual return appears twice — in the metric list and in the gate comparison —
       so the first occurrence is the one that has to follow the caveats. */
    const figure = screen.getAllByText("20.9%")[0] as HTMLElement;
    expect(caveats.compareDocumentPosition(figure)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("states every caveat `01` §8 requires, with its own numbers intact", () => {
    render(<BacktestCard backtest={backtest()} />);

    expect(screen.getByTestId("twt-caveat-trades")).toHaveTextContent("164 trades");
    expect(screen.getByTestId("twt-caveat-trades")).toHaveTextContent("53% of the gross profit");
    expect(screen.getByTestId("twt-caveat-trades")).toHaveTextContent(
      "fifteen independent observations",
    );
    expect(screen.getByTestId("twt-caveat-giveback")).toHaveTextContent(
      "a 20% give-back from the peak is routine",
    );
    expect(screen.getByTestId("twt-caveat-giveback")).toHaveTextContent("₹50,000 open loss");
    expect(screen.getByTestId("twt-caveat-capital")).toHaveTextContent(
      "this strategy at ₹25 lakh",
    );
    expect(screen.getByTestId("twt-caveat-capital")).toHaveTextContent("₹10 lakh");
  });

  it("keeps the caveats on the page when there is no result to qualify", () => {
    render(<BacktestCard backtest={null} />);

    expect(screen.getByTestId("twt-caveats")).toBeInTheDocument();
    expect(screen.getByTestId("twt-backtest-empty")).toHaveTextContent(
      /No completed run has been recorded yet/i,
    );
  });
});

describe("the twt backtest card never mixes two runs", () => {
  it("shows each source in its own column and averages nothing", () => {
    render(
      <BacktestCard
        backtest={backtest([
          backtestRun(),
          backtestRun({
            id: 2,
            source: "RESEARCH_EXPORT",
            stats: { ...backtestRun().stats, cagr_pct: "18.30" },
          }),
        ])}
      />,
    );

    expect(screen.getByText("Measured on our own price history")).toBeInTheDocument();
    expect(screen.getByText("Measured on the research data")).toBeInTheDocument();
    expect(screen.getAllByText("20.9%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("18.3%").length).toBeGreaterThan(0);
  });

  /**
   * `03` §9: the latest **finished** run, never the latest started. A run in flight must not
   * displace the last good number, and neither must a re-run that failed — the figure that was on
   * the page when the decision was taken has to survive a recalibration that produces another.
   */
  it("ignores a run that is still going and keeps the last finished one", () => {
    render(
      <BacktestCard
        backtest={backtest([
          backtestRun({ id: 1, finished_at: "2026-09-01T20:00:00+05:30" }),
          backtestRun({ id: 2, finished_at: null, stats: null }),
        ])}
      />,
    );

    expect(screen.getAllByText("20.9%").length).toBeGreaterThan(0);
  });

  it("shows the gate comparison, which is the only argument for the gate", () => {
    render(<BacktestCard backtest={backtest()} />);

    const comparison = screen.getAllByTestId("twt-gate-comparison")[0];
    expect(comparison).toHaveTextContent("With the gate");
    expect(comparison).toHaveTextContent("20.9%");
    expect(comparison).toHaveTextContent("Ignoring the gate");
    expect(comparison).toHaveTextContent("14.2%");
  });

  /** A silent drift is the failure this card exists to prevent, so it names both numbers. */
  it("warns when a run disagrees with the published result, naming both figures", () => {
    render(
      <BacktestCard
        backtest={backtest([
          backtestRun({
            drift: {
              flagged: true,
              cagr_pct_delta: "-4.30",
              published_cagr_pct: "20.90",
              run_cagr_pct: "16.60",
              threshold_cagr_points: "1.0",
            },
          }),
        ])}
      />,
    );

    const drift = screen.getByTestId("twt-drift");
    expect(drift).toHaveTextContent("16.6%");
    expect(drift).toHaveTextContent("20.9%");
    expect(drift).toHaveTextContent(/treat neither as settled/i);
  });

  it("says a source has no completed run rather than borrowing the other one's figures", () => {
    render(<BacktestCard backtest={backtest([backtestRun()])} />);

    expect(screen.getByTestId("twt-run-unavailable")).toHaveTextContent(/No completed run yet/i);
  });
});
