import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ActivityTab } from "@/components/portfolio/detail/activity-tab";
import { PortfolioDetailWorkspace } from "@/components/portfolio/detail/workspace";
import { NOT_A_PNL_EVENT } from "@/components/portfolio/detail-activity";
import { detailSnapshot, movers } from "@/lib/portfolio/detail-tabs";

import { RICH_ACTIVITY, groupedDetail, richDetail, richNav } from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * The Overview tab, and the Activity feed under it.
 *
 * Two rules carry most of the weight here and both are asserted rather than trusted.
 *
 * **The publisher's record is never the reader's.** §11 criterion 5 forbids blending the two, so
 * they sit in two bordered panels with two headings and a sentence saying whose is whose, and no
 * figure anywhere is a difference between them.
 *
 * **A corporate action produces no profit.** A bare "+150 shares" row reads like a windfall, so
 * every row the server flags carries the sentence saying otherwise, and its empty amount cell
 * says "no cash moved" rather than showing a dash a reader would read as a failure to load.
 */

function workspace(detail = richDetail()) {
  return renderWorkspace(
    <PortfolioDetailWorkspace detail={detail} nav={richNav()} activity={RICH_ACTIVITY} />,
  );
}

describe("the overview tab", () => {
  it("states what this portfolio is, including the facts nobody has recorded", () => {
    workspace();
    const identity = within(screen.getByTestId("overview-identity"));
    /* The headline and the strategy type are the server's one sentence, printed once as the
       panel's blurb and once as the labelled fact, so both are expected. */
    expect(identity.getAllByText("Subscribed model by Bramha Research").length).toBe(2);
    expect(identity.getByText("Zerodha, Upstox")).toBeInTheDocument();
    expect(identity.getByText("1 Apr 2025")).toBeInTheDocument();
    expect(identity.getByText("Nifty 500")).toBeInTheDocument();
    expect(screen.getByTestId("overview-identity")).toHaveTextContent(
      "Not recorded. The activity feed has no rebalance event to date.",
    );
  });

  it("keeps the two clocks apart, so a fresh sync never vouches for an old price", () => {
    workspace();
    expect(screen.getByTestId("overview-clocks")).toHaveTextContent(
      /* The month abbreviation is whatever Intl gives en-GB, which is "Sept" for September on
         current ICU. Asserting the prefix keeps the test about the two clocks rather than about
         a locale table that is not ours to pin. */
      /Valued at close of 10 Sept? 2026/,
    );
    expect(screen.getByTestId("overview-clocks")).toHaveTextContent(
      /Holdings synced: 10 Sept? 2026/,
    );
    expect(screen.getByTestId("overview-clocks")).toHaveTextContent(
      "kept apart on purpose",
    );
  });

  it("converts the stored fraction on a P&L percentage into a percentage", () => {
    const snapshot = detailSnapshot(richDetail(), richNav());
    /* `-0.000578` on the wire is a 0.06% down day, not a 0.000578% one. The units live at the
       one seam that builds the metric; a component that renders the raw fraction prints a number
       a thousand times too small and looks like a rounding bug. */
    expect(snapshot.todaysPnl.pct).toBe("-0.06");
    expect(snapshot.totalPnl.pct).toBe("19.20");
    expect(snapshot.headlineReturn.value).toBe("18.42");
    expect(snapshot.xirr.value).toBe("17.12");
    expect(snapshot.benchmarkReturn.value).toBe("13.72");
    expect(snapshot.benchmarkGap.value).toBe("4.70");
  });

  it("puts the model's own record in its own panel, never beside the reader's unlabelled", () => {
    workspace();
    const yours = screen.getByTestId("overview-your-return");
    const model = screen.getByTestId("overview-model-return");
    expect(yours).not.toContainElement(model);
    expect(model).toHaveTextContent("It is measured on the model");
    expect(model).toHaveTextContent("never added together, averaged, or subtracted");
  });

  it("shows no model panel at all for a portfolio that tracks no model", () => {
    workspace(groupedDetail());
    expect(screen.queryByTestId("overview-model-return")).not.toBeInTheDocument();
    expect(screen.getByTestId("overview-split")).toBeInTheDocument();
  });

  it("ranks contributors and detractors on rupees, not on percentages", () => {
    const ranked = movers(richDetail());
    expect(ranked.contributors.map((entry) => entry.row.symbol)).toEqual(["HDFCBANK", "TCS"]);
    expect(ranked.detractors.map((entry) => entry.row.symbol)).toEqual(["INFY"]);
    /* WIPRO has no cost basis, so it has no unrealised figure and enters neither list. It is not
       ranked as a zero, which would place it above every genuine loss. */
    expect(
      [...ranked.contributors, ...ranked.detractors].some((entry) => entry.row.symbol === "WIPRO"),
    ).toBe(false);
  });

  it("says why nothing can be ranked when no holding has a cost basis", () => {
    const noBasis = richDetail({
      holdings: [
        {
          instrument: { instrument_id: 1, symbol: "X", name: "X Ltd" },
          broker: { broker_account_id: 11, broker_id: "zerodha", label: "Zerodha" },
          quantity: "10",
          history_source: "BROKER",
          pending_reconciliation: false,
        },
      ],
    });
    expect(movers(noBasis).unavailable).toContain("nothing can be ranked");
  });

  it("moves the reader to the tab that answers the question they just opened", async () => {
    workspace();
    const user = userEvent.setup();
    /* Both the contributors panel and the detractors panel carry the link; either takes the
       reader to the same place, so the first is the one to press. */
    const [toHoldings] = screen.getAllByText("Every holding, with all sixteen figures");
    await user.click(toHoldings as HTMLElement);
    expect(screen.getByTestId("detail-panel-holdings")).toBeInTheDocument();
  });

  it("lists the last few things that happened, with a way to the whole feed", async () => {
    workspace();
    const recent = within(screen.getByTestId("overview-activity"));
    expect(recent.getByText("Bought 40 HDFCBANK at 1,590.00")).toBeInTheDocument();
    await userEvent.setup().click(recent.getByText("All activity"));
    expect(screen.getByTestId("detail-panel-activity")).toBeInTheDocument();
  });

  it("names the four overview facts Baskfy does not store, with what each would need", () => {
    workspace();
    const blocked = screen.getByTestId("overview-blocked");
    expect(blocked).toHaveTextContent("This portfolio's stated objective");
    expect(blocked).toHaveTextContent("When it was last rebalanced");
    expect(blocked).toHaveTextContent("When it is next due for review");
    expect(blocked).toHaveTextContent("A target equity exposure for this portfolio");
    expect(blocked).toHaveTextContent("a rebalance event written to the activity feed");
  });
});

describe("the activity tab", () => {
  it("counts the feed by kind before it lists the rows", () => {
    renderWorkspace(<ActivityTab items={RICH_ACTIVITY} unavailableReason={null} />);
    const counts = within(screen.getByTestId("activity-counts"));
    expect(counts.getByTestId("activity-filter-all")).toHaveTextContent("4");
    expect(counts.getByTestId("activity-filter-BUY")).toHaveTextContent("Buys");
  });

  it("narrows the feed to one kind and back", async () => {
    renderWorkspace(<ActivityTab items={RICH_ACTIVITY} unavailableReason={null} />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("activity-filter-DIVIDEND"));
    const list = within(screen.getByTestId("activity-list"));
    expect(list.getByText("Dividend from TCS")).toBeInTheDocument();
    expect(list.queryByText("Bought 40 HDFCBANK at 1,590.00")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("activity-filter-all"));
    expect(screen.getByText("Bought 40 HDFCBANK at 1,590.00")).toBeInTheDocument();
  });

  it("says a corporate action produced no profit, in the wording the other surface uses", () => {
    renderWorkspace(<ActivityTab items={RICH_ACTIVITY} unavailableReason={null} />);
    const row = screen.getByTestId("activity-row-CORPORATE_ACTION");
    expect(row).toHaveTextContent(NOT_A_PNL_EVENT);
    /* And its empty amount reads as "no cash moved" rather than as a dash a reader would take
       for a figure that failed to load. */
    expect(within(row).getByText("no cash moved")).toBeInTheDocument();
    expect(row.textContent ?? "").not.toContain("—");
  });

  it("tells apart a feed that failed to load, an empty one, and a filter that matched nothing", async () => {
    const { unmount } = renderWorkspace(
      <ActivityTab items={null} unavailableReason="The activity feed did not load." />,
    );
    expect(screen.getByTestId("activity-unavailable")).toHaveTextContent("did not load");
    unmount();

    const second = renderWorkspace(<ActivityTab items={[]} unavailableReason={null} />);
    expect(screen.getByTestId("activity-empty")).toHaveTextContent(
      "Nothing has happened in this portfolio yet",
    );
    second.unmount();

    renderWorkspace(
      <ActivityTab
        items={RICH_ACTIVITY.filter((item) => item.kind === "BUY")}
        unavailableReason={null}
      />,
    );
    await userEvent.setup().click(screen.getByTestId("activity-filter-BUY"));
    expect(screen.getByTestId("activity-list")).toBeInTheDocument();
  });
});
