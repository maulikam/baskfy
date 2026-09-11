import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RiskTab } from "@/components/portfolio/detail/risk-tab";
import { BLOCKED_RISK, riskView } from "@/lib/portfolio/detail-tabs";
import type { NavSeries, PortfolioDetail } from "@/lib/portfolio/overview";

import { barrenDetail, richDetail, richNav } from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * G5 — the Risk tab leads with what IS known, lists the rest as not yet measured, and prints no
 * modelled number anywhere.
 *
 * The last clause is the one worth writing a test for. `nav.daily_pnl[].pct` is in the payload
 * and an annualised volatility is two lines of arithmetic away, so nothing but a test stops a
 * later session from "finishing" the tab by computing one. The check below is deliberately blunt:
 * the not-yet-measured section may not contain a percent sign or a rupee sign at all. A figure
 * cannot appear there without tripping it.
 */

function panel(detail: PortfolioDetail = richDetail(), nav: NavSeries | null = richNav()) {
  return renderWorkspace(<RiskTab risk={riskView(detail, nav)} />);
}

describe("the risk tab", () => {
  it("leads with the three kinds of risk Baskfy actually measures", () => {
    panel();
    const known = within(screen.getByTestId("risk-known"));
    expect(known.getByText("Deepest fall from a high")).toBeInTheDocument();
    expect(known.getByText("Below its high today")).toBeInTheDocument();
    expect(known.getByText("Largest holding")).toBeInTheDocument();
    expect(known.getByText("Concentration score")).toBeInTheDocument();
    expect(known.getByText("Value waiting on reconciliation")).toBeInTheDocument();
  });

  it("states each risk reading in plain language before it states the number", () => {
    panel();
    expect(screen.getByTestId("risk-largest-holding")).toHaveTextContent(
      "How much of this portfolio rides on its single biggest name",
    );
    expect(screen.getByTestId("risk-max-drawdown")).toHaveTextContent(
      "It is what has happened, not what could",
    );
  });

  it("measures the drawdown risk from the series rather than describing it", () => {
    const view = riskView(richDetail(), richNav());
    const deepest = view.known.find((reading) => reading.id === "max-drawdown");
    const current = view.known.find((reading) => reading.id === "current-drawdown");
    expect(deepest?.figure.value).toBe("-5.00");
    expect(deepest?.figure.since).toBe("2026-02-27 to 2026-03-16");
    expect(current?.figure.value).toBe("-1.77");
  });

  it("measures the concentration risk exactly, on weights taken from the values", () => {
    const view = riskView(richDetail(), richNav());
    expect(view.known.find((r) => r.id === "largest-holding")?.figure.value).toBe("49.68");
    expect(view.known.find((r) => r.id === "top-three")?.figure.value).toBe("97.70");
    expect(view.known.find((r) => r.id === "concentration")?.figure.value).toBe("3640");
  });

  it("reports reconciliation exposure as a risk, and says when the frozen rows cannot be valued", () => {
    const view = riskView(richDetail(), richNav());
    const reading = view.known.find((r) => r.id === "reconciliation-exposure");
    expect(reading?.plain).toContain("frozen out of the return series");
    expect(reading?.figure.value).toBeNull();
    expect(reading?.figure.unavailable).toContain("could not be valued");
  });

  it("reports the cost-basis blind spot as a risk, sized against the book", () => {
    const view = riskView(richDetail(), richNav());
    const reading = view.known.find((r) => r.id === "cost-basis-exposure");
    expect(reading?.figure.value).toBe("2.30");
    expect(reading?.plain).toContain("no purchase price on record");
  });

  it("says a portfolio with no benchmark cannot tell its own falls from the market's", () => {
    const view = riskView(barrenDetail(), null);
    const reading = view.known.find((r) => r.id === "no-benchmark");
    expect(reading?.plain).toContain("whether its falls were the market's or its own");
    expect(reading?.figure.value).toBeNull();
  });

  it("lists every risk figure the brief asks for that Baskfy cannot compute", () => {
    panel();
    const blocked = within(screen.getByTestId("risk-blocked"));
    for (const item of BLOCKED_RISK) {
      expect(blocked.getByText(item.name)).toBeInTheDocument();
    }
    expect(BLOCKED_RISK.map((item) => item.name)).toEqual(
      expect.arrayContaining([
        "Beta against the benchmark",
        "Annualised volatility",
        "Sharpe ratio",
        "Sortino ratio and downside deviation",
        "Value at Risk",
        "Conditional VaR, the expected loss beyond VaR",
        "Correlation between your holdings",
        "Stress tests at minus 5, 10 and 20 percent",
      ]),
    );
  });

  it("says what each unmeasured risk figure would actually take, not just that it is missing", () => {
    panel();
    const blocked = screen.getByTestId("risk-blocked");
    expect(blocked).toHaveTextContent(
      "a documented model: historical, parametric or Monte Carlo, with its confidence level and horizon",
    );
    expect(blocked).toHaveTextContent("a statistics job over portfolio_nav_daily");
    for (const item of BLOCKED_RISK) {
      expect(blocked).toHaveTextContent(item.unblockedBy);
    }
  });

  it("prints no modelled risk number: the unmeasured section carries no figure at all", () => {
    panel();
    const blocked = screen.getByTestId("risk-blocked").textContent ?? "";
    expect(blocked).not.toContain("%");
    expect(blocked).not.toContain("₹");
    /* And it never becomes eighteen dashes either, which is the other way to fail this. */
    expect(blocked).not.toContain("—");
  });

  it("never computes a volatility, even though the daily series it would need is right there", () => {
    const nav = richNav();
    expect(nav.daily_pnl?.length).toBeGreaterThan(0);
    const view = riskView(richDetail(), nav);
    const labels = view.known.map((reading) => reading.figure.label.toLowerCase());
    expect(labels.some((label) => label.includes("volatility"))).toBe(false);
    expect(labels.some((label) => label.includes("sharpe"))).toBe(false);
    expect(labels.some((label) => label.includes("beta"))).toBe(false);
  });

  it("says plainly that nothing on the risk tab is a forecast or a recommendation", () => {
    panel();
    expect(screen.getByTestId("risk-known")).toHaveTextContent(
      "None of it is a forecast, and none of it is a recommendation about a position.",
    );
  });

  it("still renders every risk reading, with its reason, for a portfolio with no history", () => {
    panel(barrenDetail(), null);
    const known = screen.getByTestId("risk-known");
    expect(known.textContent ?? "").not.toContain("—");
    expect(known).toHaveTextContent("there is no peak to measure a fall from");
  });
});
