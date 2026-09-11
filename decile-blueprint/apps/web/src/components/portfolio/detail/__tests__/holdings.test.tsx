import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { HoldingsTab } from "@/components/portfolio/detail/holdings-tab";
import {
  HISTORY_REASONS,
  HISTORY_REASON_FALLBACK,
  type DetailHoldingRow,
} from "@/lib/portfolio/detail-view";
import {
  availableFilters,
  holdingRow,
  holdingRows,
  holdingsContext,
  sortHoldings,
  summariseSelection,
} from "@/lib/portfolio/detail-tabs";

import { RICH_HOLDINGS, groupedDetail, holding, richDetail } from "./fixtures";
import { renderStrict, renderWorkspace } from "./render";

/**
 * G2, G3 and G4 — the holdings table, the cost-basis cell, and target weight against drift.
 *
 * The assertions are the spec's, not the markup's: a column exists because the payload carries
 * the figure, a missing average price states its cause and is never a zero, and a target weight
 * exists for a basket-backed portfolio and is declared with a reason for a hand-grouped one.
 *
 * Both the desktop table and the phone list render in jsdom, because CSS is not applied there.
 * Every query below is therefore scoped to the row it means, which is also what stops a passing
 * assertion in one of the two from covering for a broken cell in the other.
 */

const CONTEXT = holdingsContext(richDetail());

function table(detail = richDetail()) {
  const rows = holdingRows(detail);
  return renderWorkspace(
    <HoldingsTab rows={rows} basketBacked={holdingsContext(detail).basketBacked} />,
  );
}

describe("the holdings table", () => {
  it("renders one row per holding, with the columns the payload can actually fill", () => {
    table();
    for (const row of RICH_HOLDINGS) {
      expect(screen.getByTestId(`holding-row-${row.instrument.symbol}`)).toBeInTheDocument();
    }
    const hdfc = within(screen.getByTestId("holding-row-HDFCBANK"));
    expect(hdfc.getByText("HDFC Bank")).toBeInTheDocument();
    expect(hdfc.getByText("320")).toBeInTheDocument();
    expect(hdfc.getByText("₹1,421")).toBeInTheDocument();
    expect(hdfc.getByText("₹1,610")).toBeInTheDocument();
    expect(hdfc.getByText("₹5,15,200")).toBeInTheDocument();
    expect(hdfc.getByText("50.00%")).toBeInTheDocument();
    expect(hdfc.getByText("Zerodha")).toBeInTheDocument();
  });

  it("names the one valuation date behind every holdings price, above the table", () => {
    renderWorkspace(
      <HoldingsTab
        rows={holdingRows(richDetail())}
        basketBacked
        pricedOnLine="Valued at close of 10 Sept 2026"
      />,
    );
    /* One date covers every row, because the payload has no per-row answer. Saying it once above
       the table is the honest shape; a column of the same date on every row is not. */
    expect(screen.getByTestId("holdings-panel")).toHaveTextContent(
      "Valued at close of 10 Sept 2026",
    );
  });

  it("gives the holdings a sortable header for every column, with the sort state announced", async () => {
    table();
    const user = userEvent.setup();
    const header = screen.getByTestId("holdings-sort-weight");
    await user.click(header);
    expect(header.closest("th")).toHaveAttribute("aria-sort", "descending");
    await user.click(header);
    expect(header.closest("th")).toHaveAttribute("aria-sort", "ascending");
  });

  it("flips the holdings sort direction once per click, under StrictMode's double render", async () => {
    const detail = richDetail();
    renderStrict(
      <HoldingsTab rows={holdingRows(detail)} basketBacked={holdingsContext(detail).basketBacked} />,
    );
    const user = userEvent.setup();
    const header = screen.getByTestId("holdings-sort-weight");
    await user.click(header);
    expect(header.closest("th")).toHaveAttribute("aria-sort", "descending");
    await user.click(header);
    /* One click, one flip. A `setDirection` called from inside a `setSortKey` updater flips twice
       here and lands back on descending, which is how this was found. */
    expect(header.closest("th")).toHaveAttribute("aria-sort", "ascending");
    await user.click(header);
    expect(header.closest("th")).toHaveAttribute("aria-sort", "descending");
  });

  it("sorts the holdings and sinks rows with no figure to the bottom in both directions", () => {
    const rows = holdingRows(richDetail());
    const down = sortHoldings(rows, "marketValue", -1).map((row) => row.symbol);
    const up = sortHoldings(rows, "marketValue", 1).map((row) => row.symbol);

    expect(down[0]).toBe("HDFCBANK");
    expect(down[down.length - 1]).toBe("RELIANCE");
    expect(up[0]).toBe("WIPRO");
    /* RELIANCE has no market value at all. Ascending must not promote it to "smallest": an
       absent figure has no place in an ordering by that figure, in either direction. */
    expect(up[up.length - 1]).toBe("RELIANCE");
  });

  it("keeps a sticky header and a sticky first column on the holdings table", () => {
    table();
    const header = screen.getByTestId("holdings-sort-security").closest("th");
    expect(header?.className).toContain("sticky");
    expect(header?.className).toContain("top-0");
    /* jsdom applies no layout, so the class is the only checkable proxy for stickiness here;
       `e2e` is where a scroll can actually be driven. Asserting it still catches the refactor
       that drops the positioning while moving the markup around. */
    const firstCell = within(screen.getByTestId("holding-row-HDFCBANK")).getAllByRole("cell")[1];
    expect(firstCell?.className).toContain("sticky");
  });

  it("offers the quick filters the payload can answer, each with its count", () => {
    table();
    /* Two of the five are worth more than they cost, one is worth less, and two carry no cost
       basis at all. The last two are in neither count: absent is not a loss. */
    expect(screen.getByTestId("holdings-filter-winners")).toHaveTextContent("2");
    expect(screen.getByTestId("holdings-filter-losers")).toHaveTextContent("1");
    expect(screen.getByTestId("holdings-filter-no-cost-basis")).toHaveTextContent("1");
    expect(screen.getByTestId("holdings-filter-no-price")).toHaveTextContent("1");
    expect(screen.getByTestId("holdings-filter-reconciliation")).toHaveTextContent("1");
  });

  it("narrows the holdings to the chosen filter and says the rest are still there", async () => {
    table();
    const user = userEvent.setup();
    await user.click(screen.getByTestId("holdings-filter-losers"));
    expect(screen.getByTestId("holding-row-INFY")).toBeInTheDocument();
    expect(screen.queryByTestId("holding-row-HDFCBANK")).not.toBeInTheDocument();
  });

  it("lets a reader switch a holdings column off and back on", async () => {
    table();
    const user = userEvent.setup();
    expect(screen.getByTestId("holdings-sort-weight")).toBeInTheDocument();
    await user.click(screen.getByTestId("holdings-column-picker-toggle"));
    await user.click(within(screen.getByTestId("holdings-column-picker")).getByText("Weight"));
    expect(screen.queryByTestId("holdings-sort-weight")).not.toBeInTheDocument();
    await user.click(within(screen.getByTestId("holdings-column-picker")).getByText("Weight"));
    expect(screen.getByTestId("holdings-sort-weight")).toBeInTheDocument();
  });

  it("never lets the security column be switched off, because a row without a name is not a row", async () => {
    table();
    const user = userEvent.setup();
    await user.click(screen.getByTestId("holdings-column-picker-toggle"));
    const picker = within(screen.getByTestId("holdings-column-picker"));
    expect(picker.queryByText("Security")).not.toBeInTheDocument();
  });

  it("adds up a selection of holdings exactly and names the rows it had to leave out", async () => {
    table();
    const user = userEvent.setup();
    await user.click(screen.getAllByTestId("holdings-select-HDFCBANK")[0] as HTMLElement);
    await user.click(screen.getAllByTestId("holdings-select-TCS")[0] as HTMLElement);
    const toolbar = within(screen.getByTestId("holdings-selection-toolbar"));
    expect(toolbar.getByText("2")).toBeInTheDocument();
    expect(toolbar.getByText("₹7,91,200")).toBeInTheDocument();

    await user.click(screen.getAllByTestId("holdings-select-RELIANCE")[0] as HTMLElement);
    expect(screen.getByTestId("holdings-selection-excluded")).toHaveTextContent("1");
    /* The unvalued row is counted separately rather than added as zero; the sum is unchanged. */
    expect(
      within(screen.getByTestId("holdings-selection-toolbar")).getByText("₹7,91,200"),
    ).toBeInTheDocument();
  });

  it("says on the holdings selection toolbar that selecting places no order", async () => {
    table();
    const user = userEvent.setup();
    await user.click(screen.getAllByTestId("holdings-select-HDFCBANK")[0] as HTMLElement);
    expect(screen.getByTestId("holdings-selection-toolbar")).toHaveTextContent(
      "It places no order and sends nothing to a broker.",
    );
  });

  it("names the columns the holdings payload cannot fill rather than drawing them empty", () => {
    table();
    const blocked = within(screen.getByTestId("holdings-blocked"));
    expect(blocked.getByText("Exchange and instrument type")).toBeInTheDocument();
    expect(blocked.getByText(/Available quantity, free against pledged/)).toBeInTheDocument();
    expect(blocked.getByText("Price age for one holding")).toBeInTheDocument();
    expect(screen.queryByTestId("holdings-sort-exchange")).not.toBeInTheDocument();
  });

  it("tells an empty portfolio apart from a holdings read that failed", () => {
    const { unmount } = renderWorkspace(<HoldingsTab rows={[]} basketBacked={false} />);
    expect(screen.getByTestId("holdings-empty")).toBeInTheDocument();
    unmount();

    renderWorkspace(
      <HoldingsTab
        rows={[]}
        basketBacked={false}
        unavailableReason="The holdings for this portfolio did not load."
      />,
    );
    expect(screen.getByTestId("holdings-unavailable")).toHaveTextContent("did not load");
  });

  it("derives every holdings percentage from fields the payload actually sent", () => {
    const rows = holdingRows(richDetail());
    const hdfc = rows.find((row) => row.symbol === "HDFCBANK");
    /* value 515200, today +960, so the previous close of this position was 514240 and the move
       is 960/514240 = 0.1867%. Restated from two sent fields, never estimated. */
    expect(hdfc?.todaysPct.value).toBe("0.19");
    /* value 515200, unrealised 60640, so cost was 454560 and the gain is 13.34% of it. */
    expect(hdfc?.unrealisedPct.value).toBe("13.34");
    /* the portfolio cost 870000, so this position has added 6.97 points of the portfolio's own
       return. The denominator is the server's `invested`, never a figure assembled here. */
    expect(hdfc?.contribution.value).toBe("6.97");
  });
});

describe("a holding with no cost basis", () => {
  const REASONS: readonly { source: string; sentence: string }[] = [
    { source: "NONE", sentence: HISTORY_REASONS.NONE ?? "" },
    { source: "CAS", sentence: HISTORY_REASONS.CAS ?? "" },
    { source: "BROKER", sentence: HISTORY_REASONS.BROKER ?? "" },
    { source: "MANUAL", sentence: HISTORY_REASONS.MANUAL ?? "" },
  ];

  it.each(REASONS)(
    "states the cause of a missing cost basis for history_source $source",
    ({ source, sentence }) => {
      const row = holdingRow(holding({ avg_price: null, history_source: source }), CONTEXT);
      expect(row.avgPrice.value).toBeNull();
      expect(row.avgPrice.unavailable).toBe(sentence);
    },
  );

  it("falls back to a sentence rather than silence when the cost basis reason is unknown", () => {
    const row = holdingRow(holding({ avg_price: null, history_source: "SOMETHING_NEW" }), CONTEXT);
    expect(row.avgPrice.unavailable).toBe(HISTORY_REASON_FALLBACK);
  });

  it("renders the missing cost basis as words in the cell, never as a dash and never as a zero", () => {
    table();
    const wipro = within(screen.getByTestId("holding-row-WIPRO"));
    expect(wipro.getAllByText("no purchase price").length).toBeGreaterThan(0);
    expect(screen.getByTestId("holding-row-WIPRO").textContent ?? "").not.toContain("—");
    expect(screen.getByTestId("holding-row-WIPRO").textContent ?? "").not.toContain("₹0");
  });

  it("prints every distinct cost basis reason once under the table, where a tooltip cannot reach", () => {
    table();
    const footnote = screen.getByTestId("holdings-footnote");
    expect(footnote).toHaveTextContent(HISTORY_REASONS.BROKER ?? "");
    expect(footnote).toHaveTextContent("None of them is a zero.");
  });

  it("refuses a contribution figure for a holding with no cost basis rather than showing zero", () => {
    const row = holdingRow(
      holding({ avg_price: null, total_contribution: null, history_source: "BROKER" }),
      CONTEXT,
    );
    expect(row.unrealisedPnl.value).toBeNull();
    expect(row.contribution.value).toBeNull();
    expect(row.contribution.unavailable).toBe(HISTORY_REASONS.BROKER);
  });

  it("counts the holdings with no cost basis as their own quick filter", () => {
    const rows = holdingRows(richDetail());
    const offered = availableFilters(rows, { basketBacked: true }).find(
      (entry) => entry.filter.id === "no-cost-basis",
    );
    expect(offered?.count).toBe(1);
    expect(offered?.unavailable).toBeNull();
  });
});

describe("target weight and drift", () => {
  it("shows a target weight and a drift for a basket-backed portfolio", () => {
    table();
    const hdfc = within(screen.getByTestId("holding-row-HDFCBANK"));
    expect(hdfc.getByText("40.00%")).toBeInTheDocument();
    /* Held at 50.00 against a target of 40.00: ten points overweight, computed on exact
       decimals rather than on two floats subtracted. */
    expect(hdfc.getByText("+10.00 pts")).toBeInTheDocument();
  });

  it("declares the target unavailable with its reason for a hand-grouped portfolio", () => {
    table(groupedDetail());
    expect(screen.queryByTestId("holdings-sort-targetWeight")).not.toBeInTheDocument();
    expect(screen.getByTestId("holdings-filter-unavailable")).toHaveTextContent(
      "grouped by hand rather than tracking a published model",
    );
  });

  it("distinguishes a portfolio with no target from one whose model has published none", () => {
    const grouped = holdingRow(holding({ target_weight: null }), {
      ...CONTEXT,
      basketBacked: false,
    });
    const unpublished = holdingRow(holding({ target_weight: null }), {
      ...CONTEXT,
      basketBacked: true,
    });
    expect(grouped.targetWeight.unavailable).toContain("grouped by hand");
    expect(unpublished.targetWeight.unavailable).toContain("has not published a target weight");
    expect(grouped.targetWeight.unavailable).not.toBe(unpublished.targetWeight.unavailable);
  });

  it("disables the above-target and below-target filters with a reason rather than hiding them", () => {
    const rows = holdingRows(groupedDetail());
    const offered = availableFilters(rows, { basketBacked: false });
    const above = offered.find((entry) => entry.filter.id === "above-target");
    expect(above?.unavailable).toContain("grouped by hand");

    const withTargets = availableFilters(holdingRows(richDetail()), { basketBacked: true });
    expect(withTargets.find((entry) => entry.filter.id === "above-target")?.count).toBe(2);
    expect(withTargets.find((entry) => entry.filter.id === "below-target")?.count).toBe(1);
  });

  it("never invents a target weight, even where every other figure on the row is present", () => {
    const complete: DetailHoldingRow = holding({ target_weight: null });
    const row = holdingRow(complete, { ...CONTEXT, basketBacked: true });
    expect(row.targetWeight.value).toBeNull();
    expect(row.drift.value).toBeNull();
  });

  it("computes drift on exact decimals, so a column of them still adds up", () => {
    const row = holdingRow(holding({ weight: "0.333333", target_weight: "0.300000" }), CONTEXT);
    expect(row.weight.value).toBe("33.33");
    expect(row.targetWeight.value).toBe("30.00");
    expect(row.drift.value).toBe("3.33");
  });
});

describe("what a selection of holdings comes to", () => {
  it("returns a reason rather than a zero when nothing in the selection carries the figure", () => {
    const rows = holdingRows(richDetail()).filter((row) => row.symbol === "RELIANCE");
    const summary = summariseSelection(rows);
    expect(summary.marketValue.value).toBeNull();
    expect(summary.marketValue.unavailable).toContain("nothing to add up");
    expect(summary.excluded).toBe(1);
  });

  it("adds the priced holdings exactly and excludes the rest from the count", () => {
    const rows = holdingRows(richDetail());
    const summary = summariseSelection(rows);
    expect(summary.marketValue.value).toBe("1037050.00");
    expect(summary.excluded).toBe(1);
  });
});
