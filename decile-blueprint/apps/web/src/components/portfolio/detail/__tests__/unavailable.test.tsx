import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { MetricValue } from "@/components/portfolio/detail/primitives";
import { PortfolioDetailWorkspace } from "@/components/portfolio/detail/workspace";
import { metric, type Metric } from "@/lib/portfolio/command-center";
import {
  DETAIL_TABS,
  allocationView,
  detailSnapshot,
  holdingRows,
  riskView,
  settingRows,
  type DetailTabId,
} from "@/lib/portfolio/detail-tabs";
import type { ActivityItem, NavSeries, PortfolioDetail } from "@/lib/portfolio/overview";

import {
  RICH_ACTIVITY,
  barrenDetail,
  emptyDetail,
  groupedDetail,
  richDetail,
  richNav,
} from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * G8 — nothing in this workspace is unavailable without saying why. No tab, no fixture, no cell.
 *
 * > *"Never display '—' without explaining why the value is unavailable."* — the brief
 *
 * The check is the bluntest one that can be written and that is the point: the em dash may not
 * appear in the rendered text of any of the eight tabs, under any of four payloads, including one
 * where every optional field is absent at once. There is no allowance for it in prose either,
 * which is why nothing in `components/portfolio/detail/` uses an em dash in its own copy. A rule
 * you can only enforce by reading is a rule that drifts.
 *
 * The dash is only half of the failure, so the second half is checked too: the workspace must not
 * print a zero in place of an absent figure. `₹0` where a cost basis is missing cannot be told
 * apart from a genuinely free share, which is the more dangerous of the two renderings and the
 * one `detail-view.ts` was written to prevent.
 */

/** The em dash `lib/format` returns for an absent value, and `decimal.ts` returns for one too. */
const BARE_DASH = "—";

interface Payload {
  readonly name: string;
  readonly detail: PortfolioDetail;
  readonly nav: NavSeries | null;
  readonly activity: readonly ActivityItem[] | null;
}

const PAYLOADS: readonly Payload[] = [
  { name: "a fully populated portfolio", detail: richDetail(), nav: richNav(), activity: RICH_ACTIVITY },
  { name: "a portfolio grouped by hand", detail: groupedDetail(), nav: richNav(), activity: RICH_ACTIVITY },
  {
    name: "a brand-new portfolio with nothing priced, synced or bought",
    detail: barrenDetail(),
    nav: null,
    activity: null,
  },
  { name: "a portfolio with nothing in it", detail: emptyDetail(), nav: null, activity: [] },
];

async function openEveryTab(id: DetailTabId): Promise<HTMLElement> {
  const user = userEvent.setup();
  await user.click(screen.getByTestId(`detail-tab-${id}`));
  return screen.getByTestId(`detail-panel-${id}`);
}

describe("nothing is unavailable without its reason", () => {
  for (const payload of PAYLOADS) {
    for (const tab of DETAIL_TABS) {
      it(`renders no bare dash on the ${tab.label} tab for ${payload.name}`, async () => {
        const { unmount } = renderWorkspace(
          <PortfolioDetailWorkspace
            detail={payload.detail}
            nav={payload.nav}
            activity={payload.activity}
            failures={{
              nav: payload.nav === null ? "The valuation series did not load." : null,
              activity: payload.activity === null ? "The activity feed did not load." : null,
            }}
          />,
        );
        const panel = await openEveryTab(tab.id);
        expect(panel.textContent ?? "").not.toContain(BARE_DASH);
        unmount();
      });
    }
  }

  it("renders no bare dash in the workspace header or the tab strip either", () => {
    renderWorkspace(
      <PortfolioDetailWorkspace detail={barrenDetail()} nav={null} activity={null} />,
    );
    expect(screen.getByTestId("detail-tablist").textContent ?? "").not.toContain(BARE_DASH);
    expect(screen.getByTestId("detail-workspace").textContent ?? "").not.toContain(BARE_DASH);
  });

  it("puts a reason in every unavailable figure the view model produces", () => {
    for (const payload of PAYLOADS) {
      const snapshot = detailSnapshot(payload.detail, payload.nav);
      /* Named one by one rather than through `Object.values`, which types as `any[]` and would
         quietly stop checking anything the day a field changed shape. */
      const figures: Metric[] = [
        snapshot.value,
        snapshot.invested,
        snapshot.cash,
        snapshot.todaysPnl,
        snapshot.totalPnl,
        snapshot.headlineReturn,
        snapshot.modelReturn,
        snapshot.xirr,
        snapshot.benchmarkReturn,
        snapshot.benchmarkGap,
        snapshot.drawdown,
        snapshot.deployed,
        snapshot.cashShare,
        snapshot.holdingsCount,
        ...riskView(payload.detail, payload.nav).known.map((reading) => reading.figure),
        ...settingRows(payload.detail).map((row) => row.figure),
        allocationView(payload.detail).herfindahl,
        allocationView(payload.detail).top1,
        allocationView(payload.detail).cashShare,
      ];
      for (const figure of figures) {
        if (figure.value === null) {
          expect(figure.unavailable, `${payload.name}: ${figure.label}`).toBeTruthy();
          expect((figure.unavailable ?? "").length).toBeGreaterThan(10);
        } else {
          expect(figure.unavailable).toBeNull();
        }
      }
    }
  });

  it("puts a reason in every unavailable cell of every holding row", () => {
    for (const payload of PAYLOADS) {
      for (const row of holdingRows(payload.detail)) {
        const cells = [
          row.quantity,
          row.avgPrice,
          row.price,
          row.marketValue,
          row.weight,
          row.targetWeight,
          row.drift,
          row.todaysPnl,
          row.todaysPct,
          row.unrealisedPnl,
          row.unrealisedPct,
          row.contribution,
          row.heldFor,
          row.broker,
          row.pricedOn,
        ];
        for (const cell of cells) {
          if (cell.value === null) expect(cell.unavailable, `${row.symbol}/${cell.label}`).toBeTruthy();
        }
      }
    }
  });

  it("renders a reason rather than a zero when a figure is unavailable", () => {
    renderWorkspace(
      <MetricValue metric={metric("Avg price", "what it cost", null, "Your broker did not send one.")} />,
    );
    expect(screen.getByText("Your broker did not send one.")).toBeInTheDocument();
    expect(screen.queryByText("₹0")).not.toBeInTheDocument();
    expect(screen.queryByText(BARE_DASH)).not.toBeInTheDocument();
  });

  it("keeps the reason reachable in a dense cell, where the full sentence will not fit", () => {
    renderWorkspace(
      <MetricValue
        metric={metric("Avg price", "what it cost", null, "Your broker did not send one.")}
        compact
        short="no purchase price"
      />,
    );
    /* Words in the cell, the sentence on `title`, and the sentence again for a screen reader.
       A tooltip alone is invisible to anyone not holding a mouse. */
    const cell = screen.getByText("no purchase price").parentElement;
    expect(cell).toHaveAttribute("title", "Your broker did not send one.");
    expect(screen.getByText(/Avg price: Your broker did not send one\./)).toBeInTheDocument();
  });

  it("supplies a sentence even when the caller forgot to give a reason for the missing value", () => {
    const careless = metric("Something", "a definition", null, undefined);
    expect(careless.unavailable).toBe("Not available for this period.");
  });
});
