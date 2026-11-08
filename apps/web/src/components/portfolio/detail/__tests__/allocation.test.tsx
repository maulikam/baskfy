import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AllocationTab } from "@/components/portfolio/detail/allocation-tab";
import { BLOCKED_ALLOCATION, allocationView } from "@/lib/portfolio/detail-tabs";

import type { PortfolioDetail } from "@/lib/portfolio/overview";

import { RICH_HOLDINGS, barrenDetail, emptyDetail, richDetail } from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * G6 — the Allocation tab shows the cuts that exist, scores the concentration, and says plainly
 * that sector, industry, market cap and geography need an instrument sector map.
 *
 * The arithmetic assertions are exact strings rather than rounded comparisons on purpose. A
 * weight is a ratio of two rupee amounts and the column has to add to 100; a float would be
 * invisible on one row and visible in the total, which is precisely the failure `percentOf` is
 * built on `bigint` to avoid. Asserting "49.68" rather than "about 49.7" is what makes that
 * testable at all.
 */

function tab(detail = richDetail()) {
  return renderWorkspace(<AllocationTab allocation={allocationView(detail)} />);
}

/** A book of `count` identical positions, for the concentration bands. */
function evenlySplit(count: number): PortfolioDetail {
  const base = RICH_HOLDINGS[0];
  if (base === undefined) throw new Error("the fixture must carry at least one holding");
  return richDetail({
    holdings: Array.from({ length: count }, (_unused, index) => ({
      ...base,
      instrument: { instrument_id: 700 + index, symbol: `NAME${index}`, name: `Name ${index}` },
      value: "250000.00",
      target_weight: null,
    })),
  });
}

describe("the allocation tab", () => {
  it("splits the allocation by security, largest first, with the unpriced row last", () => {
    const view = allocationView(richDetail());
    expect(view.bySecurity.map((bucket) => bucket.label)).toEqual([
      "HDFCBANK",
      "TCS",
      "INFY",
      "WIPRO",
      "RELIANCE",
    ]);
    expect(view.bySecurity.map((bucket) => bucket.weight.value)).toEqual([
      "49.68",
      "26.61",
      "21.41",
      "2.30",
      null,
    ]);
  });

  it("splits the allocation by broker account, adding each account exactly", () => {
    const view = allocationView(richDetail());
    const zerodha = view.byBroker.find((bucket) => bucket.label === "Zerodha");
    const upstox = view.byBroker.find((bucket) => bucket.label === "Upstox");
    expect(zerodha?.value.value).toBe("791200.00");
    expect(upstox?.value.value).toBe("245850.00");
    /* The two accounts together are the whole priced book, to the paisa. */
    expect(view.holdingsValue.value).toBe("1037050.00");
  });

  it("states the allocation between shares and cash without folding cash into the weights", () => {
    const view = allocationView(richDetail());
    expect(view.cash.value).toBe("42500.00");
    expect(view.cashShare.value).toBe("3.94");
    expect(view.deployedShare.value).toBe("96.06");
    /* The per-security weights are shares of the shares, so the largest is still 49.68 and not
       the 47.7 it would be with cash in the denominator. */
    expect(view.top1.value).toBe("49.68");
  });

  it("scores the allocation concentration at the top 1, 3, 5 and 10", () => {
    const view = allocationView(richDetail());
    expect(view.top1.value).toBe("49.68");
    expect(view.top3.value).toBe("97.70");
    /* Only four of the five could be priced, so top 5 and top 10 are the four there are. */
    expect(view.top5.value).toBe("100.00");
    expect(view.top10.value).toBe("100.00");
  });

  it("gives the allocation a Herfindahl score and a sentence a reader can act on", () => {
    const view = allocationView(richDetail());
    expect(view.herfindahl.value).toBe("3640");
    expect(view.effectiveHoldings.value).toBe("2.7");
    expect(view.herfindahlVerdict).toContain("Concentrated");
    expect(view.herfindahlVerdict).toContain("4 equally weighted holdings would score about 2500");
  });

  it("puts an allocation in the right concentration band, in words as well as a score", () => {
    /* The score is a number without a scale, so the word is the half a reader can act on. The
       thresholds are the ones competition authorities use on the same 0 to 10,000 index. */
    expect(allocationView(evenlySplit(4)).herfindahlVerdict).toContain("Concentrated");
    expect(allocationView(evenlySplit(6)).herfindahlVerdict).toContain("Moderately concentrated");
    expect(allocationView(evenlySplit(10)).herfindahlVerdict).toContain("Spread");

    expect(allocationView(evenlySplit(4)).herfindahl.value).toBe("2500");
    expect(allocationView(evenlySplit(10)).herfindahl.value).toBe("1000");
    /* Ten equal names score the same as ten equal names: the equivalent count is the score read
       back the other way, which is the sentence rather than the index. */
    expect(allocationView(evenlySplit(10)).effectiveHoldings.value).toBe("10.0");
  });

  it("renders the allocation buckets with a bar and a number, never a bar alone", () => {
    tab();
    const security = within(screen.getByTestId("allocation-by-security"));
    expect(security.getByText("49.68%")).toBeInTheDocument();
    expect(security.getByText("₹5,15,200")).toBeInTheDocument();
  });

  it("keeps the unpriced holding out of every allocation total and says why", () => {
    tab();
    expect(screen.getByTestId("allocation-unpriced")).toHaveTextContent(
      "counting them as zero would shrink the denominator",
    );
    const reliance = within(screen.getByTestId("allocation-bucket-505-11"));
    expect(reliance.getAllByText("not priced").length).toBeGreaterThan(0);
  });

  it("does not narrate sector/industry/geo gaps as a product section (AFH 5.2)", () => {
    tab();
    expect(screen.queryByTestId("allocation-blocked")).not.toBeInTheDocument();
    expect(BLOCKED_ALLOCATION).toHaveLength(5);
  });

  it("keeps overlap with other portfolios out of the rendered allocation tab", () => {
    tab();
    expect(screen.queryByTestId("allocation-blocked")).not.toBeInTheDocument();
    expect(BLOCKED_ALLOCATION.some((item) => item.name.includes("Overlap"))).toBe(true);
  });

  it("renders an allocation with nothing priced without a single bare dash", () => {
    tab(barrenDetail());
    expect(screen.getByTestId("allocation-totals").textContent ?? "").not.toContain("—");
    expect(screen.getByTestId("allocation-concentration")).toHaveTextContent(
      "A concentration score needs at least one valued holding",
    );
  });

  it("drill: a security slice leads to that instrument, and a broker slice leads nowhere", () => {
    /* Brief: *"Drill down from every chart into the relevant holdings"*, and *"Allow users to
       click a segment"*. A security slice has a destination — the instrument's own page. A broker
       slice does not, and carries no link rather than a dead one. */
    tab();
    const security = screen.getByTestId("allocation-by-security");
    const links = within(security).getAllByRole("link");
    expect(links.length).toBeGreaterThan(0);
    expect(links[0]!.getAttribute("href")).toMatch(/^\/instruments\//);

    expect(within(screen.getByTestId("allocation-by-broker")).queryAllByRole("link")).toHaveLength(
      0,
    );
  });

  it("says an empty portfolio has no allocation to draw rather than drawing an empty chart", () => {
    tab(emptyDetail());
    expect(screen.getByTestId("allocation-by-security")).toHaveTextContent(
      "Nothing is filed into this portfolio yet",
    );
  });
});
