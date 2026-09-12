import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PerformanceTab } from "@/components/portfolio/detail/performance-tab";
import {
  bestAndWorst,
  calendarYears,
  drawdownEpisodes,
  flowSummary,
  monthlyReturns,
  returnBasis,
  rollingReturns,
} from "@/lib/portfolio/detail-tabs";
import type { NavSeries } from "@/lib/portfolio/overview";

import { longNav, richNav, richSummary } from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * The Performance tab, and the two disciplines that make its derived figures trustworthy.
 *
 * **Every series reads the wealth index, never the value line.** The fixture moves ₹50,000 in
 * during March and ₹10,000 out during June precisely so that a monthly return taken off the value
 * line would be visibly wrong: March's value rises while its index falls. The test below pins
 * March negative.
 *
 * **A window longer than the series reports that it is.** All four rolling windows are longer than
 * the nine sessions the fixture holds, and all four must say so rather than measure over whatever
 * is there. So must the compound annual rate, which is refused under a year.
 */

function tab(nav: NavSeries | null = richNav()) {
  return renderWorkspace(
    <PerformanceTab summary={richSummary()} nav={nav} unavailableReason={null} />,
  );
}

describe("the performance tab", () => {
  it("takes monthly returns off the wealth index, so a deposit is not read as a gain", () => {
    const months = monthlyReturns(richNav());
    const march = months.find((month) => month.key === "2026-03");
    /* ₹50,000 arrived in March. The value line rose; the index fell 2%, and that is the return. */
    expect(march?.figure.value).toBe("-2.00");
    expect(months.map((month) => month.key)).toEqual([
      "2026-02",
      "2026-03",
      "2026-04",
      "2026-05",
      "2026-06",
      "2026-07",
      "2026-08",
    ]);
  });

  it("leaves the first month of the window out, because a part month is not the month", () => {
    const months = monthlyReturns(richNav());
    expect(months.some((month) => month.key === "2026-01")).toBe(false);
  });

  it("compounds the calendar year from the months it has and marks it partial", () => {
    const years = calendarYears(richNav());
    const year = years.find((entry) => entry.year === 2026);
    expect(year?.partial).toBe(true);
    /* Seven months compounded come back to the range's own time-weighted return, 11.00%, which
       is the check that the grid and the headline figure are reading the same series. */
    expect(year?.figure.value).toBe("11.00");
  });

  it("refuses every rolling window longer than the series rather than measuring a short one", () => {
    const windows = rollingReturns(richNav());
    expect(windows).toHaveLength(4);
    for (const window of windows) {
      expect(window.observations).toBe(0);
      expect(window.latest.value).toBeNull();
      expect(window.latest.unavailable).toContain("The series holds 9 sessions");
      expect(window.latest.unavailable).toContain(`this window needs ${window.sessions + 1}`);
    }
  });

  it("tells a series that is too short apart from a series that does not exist", () => {
    const { drawdown: _drawdown, ...withoutIndex } = richNav();
    expect(rollingReturns(withoutIndex)[0]?.latest.unavailable).toContain(
      "has not been built for this portfolio",
    );
    expect(rollingReturns(richNav())[0]?.latest.unavailable).toContain("The series holds 9");
  });

  it("dates each fall from the last session at the high, matching the server's own peak date", () => {
    const episodes = drawdownEpisodes(richNav());
    const deepest = episodes[0];
    expect(deepest?.depth.value).toBe("-5.00");
    /* `max_drawdown.peak_on` in the payload is 2026-02-27, and the episode found here agrees.
       Dating the fall from its first down day would have said 2026-03-16 and been a session out
       on every episode. */
    expect(deepest?.peakOn).toBe("2026-02-27");
    expect(deepest?.troughOn).toBe("2026-03-16");
    expect(deepest?.recoveredOn).toBe("2026-04-30");
    expect(deepest?.ongoing).toBe(false);
  });

  it("marks a fall that has not come back as ongoing rather than closing it at the last session", () => {
    const episodes = drawdownEpisodes(richNav());
    const ongoing = episodes.filter((episode) => episode.ongoing);
    expect(ongoing).toHaveLength(1);
    expect(ongoing[0]?.troughOn).toBe("2026-08-31");
    expect(ongoing[0]?.recoveredOn).toBeNull();
  });

  it("says so rather than inventing a date when a fall began before the window", () => {
    const opensLow: NavSeries = {
      ...richNav(),
      drawdown: [
        { on: "2026-01-30", index: "97.000000", peak: "100.000000", drawdown: "-0.030000" },
        { on: "2026-02-27", index: "100.000000", peak: "100.000000", drawdown: "0.000000" },
      ],
    };
    const episode = drawdownEpisodes(opensLow)[0];
    expect(episode?.peakOn).toBeNull();
    expect(episode?.toTrough).toBeNull();
    expect(episode?.depth.since).toBe("up to 2026-01-30");
  });

  it("names the best and worst day from the daily series and the months from the index", () => {
    const extremes = bestAndWorst(richNav());
    expect(extremes.bestDay.value).toBe("3.10");
    expect(extremes.bestDay.since).toBe("2026-04-30");
    expect(extremes.worstDay.value).toBe("-5.00");
    expect(extremes.bestMonth.value).toBe("5.83");
    expect(extremes.bestMonth.since).toBe("Jun 2026");
    expect(extremes.worstMonth.value).toBe("-2.83");
  });

  it("separates the money moved from the performance rather than subtracting one from the other", () => {
    const flows = flowSummary(richNav());
    expect(flows.deposits.value).toBe("50000.00");
    expect(flows.withdrawals.value).toBe("-10000.00");
    expect(flows.net.value).toBe("40000.00");
    expect(flows.valueChange.value).toBe("120000.00");
    expect(flows.note).toContain("Money you moved in or out is not performance");
    expect(flows.note).toContain("takes your deposits and withdrawals out by construction");
  });

  it("states each return basis with how it is calculated and what it answers", () => {
    const basis = returnBasis(richSummary(), richNav());
    expect(basis.map((entry) => entry.id)).toEqual(["twr", "xirr", "cagr"]);
    expect(basis[0]?.figure.value).toBe("11.00");
    expect(basis[0]?.how).toContain("wealth index, end against start");
    expect(basis[1]?.figure.value).toBe("17.12");
    expect(basis[1]?.how).toContain("discounts every dated cash movement");
    for (const entry of basis) {
      expect(entry.how.length).toBeGreaterThan(20);
      expect(entry.useFor.length).toBeGreaterThan(20);
    }
  });

  it("refuses a compound annual rate over a window shorter than a year", () => {
    const cagr = returnBasis(richSummary(), richNav()).find((entry) => entry.id === "cagr");
    expect(cagr?.figure.value).toBeNull();
    expect(cagr?.figure.unavailable).toContain("This window covers 213 days");
    expect(cagr?.figure.unavailable).toContain("a rate the portfolio has never actually run for");
  });

  it("annualises the return once the window is long enough to be annualised honestly", () => {
    const cagr = returnBasis(richSummary(), longNav()).find((entry) => entry.id === "cagr");
    expect(cagr?.figure.value).not.toBeNull();
    /* Nineteen months of 11% is less than 11% a year, so the annualised figure must come in
       under the raw one. An exponent applied the wrong way round shows up here immediately. */
    expect(Number(cagr?.figure.value)).toBeLessThan(11);
    expect(Number(cagr?.figure.value)).toBeGreaterThan(0);
  });

  it("renders the monthly grid with the number in every cell, not colour alone", () => {
    tab();
    const heatmap = within(screen.getByTestId("performance-heatmap"));
    expect(heatmap.getByTestId("performance-month-2026-03")).toHaveTextContent("-2.00%");
    expect(heatmap.getByTestId("performance-month-2026-06")).toHaveTextContent("+5.83%");
    expect(screen.getByTestId("performance-heatmap")).toHaveTextContent(
      "the number is in every cell, so the grid is readable without it",
    );
  });

  it("names the months the series does not cover rather than leaving the cell blank", () => {
    tab();
    const row = within(screen.getByTestId("performance-year-2026"));
    /* January is not measurable and September onward has not happened. Both read as words. */
    expect(row.getAllByText("no data").length).toBeGreaterThan(0);
  });

  it("says what it cannot plot rather than drawing an empty chart", () => {
    renderWorkspace(
      <PerformanceTab
        summary={richSummary()}
        nav={null}
        unavailableReason="The valuation series did not load."
      />,
    );
    expect(screen.getByTestId("performance-chart-unavailable")).toHaveTextContent(
      "The valuation series did not load.",
    );
    expect(screen.getByTestId("performance-heatmap")).toHaveTextContent(
      "does not span two month ends yet",
    );
    expect(screen.getByTestId("performance-drawdowns")).toHaveTextContent(
      "has not been below its own high water mark",
    );
  });

  it("does not narrate performance gaps as a product section (AFH 5.2)", () => {
    tab();
    expect(screen.queryByTestId("performance-blocked")).not.toBeInTheDocument();
  });
});
