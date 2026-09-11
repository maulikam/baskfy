import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { PortfolioDetailWorkspace } from "@/components/portfolio/detail/workspace";
import { DETAIL_TABS, isDetailTabId } from "@/lib/portfolio/detail-tabs";

import { RICH_ACTIVITY, richDetail, richNav } from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * G1 — the eight tabs exist, are reachable by keyboard, and each carries an accessible name.
 *
 * These are written against the ARIA tabs pattern rather than against the markup. The pattern is
 * the spec here: a `tablist` whose tabs own a roving tabindex, arrow keys that move and select,
 * Home and End, and a panel bound to its tab in both directions. A test that asserted class names
 * would pass over a strip nobody can drive from a keyboard, which is the half of this component a
 * mouse never exercises.
 */

function workspace() {
  return (
    <PortfolioDetailWorkspace
      detail={richDetail()}
      nav={richNav()}
      activity={RICH_ACTIVITY}
      failures={{ nav: null, activity: null }}
    />
  );
}

describe("the detail workspace tabs", () => {
  it("renders all eight tabs the brief names, in the brief's order", () => {
    renderWorkspace(workspace());
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(8);
    expect(tabs.map((tab) => tab.textContent?.replace(/\d.*$/, "").trim())).toEqual([
      "Overview",
      "Holdings",
      "Performance",
      "Allocation",
      "Risk",
      "Rebalance",
      "Activity",
      "Settings",
    ]);
  });

  it("gives every one of the tabs an accessible name that begins with its own label", () => {
    renderWorkspace(workspace());
    for (const tab of DETAIL_TABS) {
      const element = screen.getByTestId(`detail-tab-${tab.id}`);
      const name = element.getAttribute("aria-label") ?? element.textContent ?? "";
      expect(name.trim().startsWith(tab.label)).toBe(true);
    }
  });

  it("keeps exactly one of the tabs in the tab order, so Tab reaches the panel not the strip", () => {
    renderWorkspace(workspace());
    const reachable = screen.getAllByRole("tab").filter((tab) => tab.tabIndex === 0);
    expect(reachable).toHaveLength(1);
    expect(reachable[0]).toHaveAttribute("aria-selected", "true");
  });

  it("binds each panel to its tab in both directions", async () => {
    renderWorkspace(workspace());
    const user = userEvent.setup();
    for (const tab of DETAIL_TABS) {
      await user.click(screen.getByTestId(`detail-tab-${tab.id}`));
      const panel = screen.getByRole("tabpanel");
      const control = screen.getByTestId(`detail-tab-${tab.id}`);
      expect(control).toHaveAttribute("aria-controls", panel.id);
      expect(panel).toHaveAttribute("aria-labelledby", control.id);
    }
  });

  it("shows exactly one panel at a time across all eight tabs", async () => {
    renderWorkspace(workspace());
    const user = userEvent.setup();
    for (const tab of DETAIL_TABS) {
      await user.click(screen.getByTestId(`detail-tab-${tab.id}`));
      expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
      expect(screen.getByTestId(`detail-panel-${tab.id}`)).toBeInTheDocument();
    }
  });

  it("moves between tabs with the arrow keys and wraps at both ends", async () => {
    renderWorkspace(workspace());
    const user = userEvent.setup();
    const overview = screen.getByTestId("detail-tab-overview");
    overview.focus();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByTestId("detail-tab-holdings")).toHaveFocus();
    expect(screen.getByTestId("detail-panel-holdings")).toBeInTheDocument();

    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(screen.getByTestId("detail-tab-settings")).toHaveFocus();
    expect(screen.getByTestId("detail-panel-settings")).toBeInTheDocument();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByTestId("detail-tab-overview")).toHaveFocus();
  });

  it("jumps to the first and last of the tabs with Home and End", async () => {
    renderWorkspace(workspace());
    const user = userEvent.setup();
    screen.getByTestId("detail-tab-overview").focus();

    await user.keyboard("{End}");
    expect(screen.getByTestId("detail-tab-settings")).toHaveFocus();

    await user.keyboard("{Home}");
    expect(screen.getByTestId("detail-tab-overview")).toHaveFocus();
  });

  it("tells the parent which of the tabs was chosen, so a URL can mirror it", async () => {
    const seen: string[] = [];
    renderWorkspace(
      <PortfolioDetailWorkspace
        detail={richDetail()}
        nav={richNav()}
        activity={RICH_ACTIVITY}
        onTabChange={(id) => seen.push(id)}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("detail-tab-risk"));
    await user.click(screen.getByTestId("detail-tab-allocation"));
    expect(seen).toEqual(["risk", "allocation"]);
  });

  it("opens on the tab the parent asks for rather than always on the first", () => {
    renderWorkspace(
      <PortfolioDetailWorkspace
        detail={richDetail()}
        nav={richNav()}
        activity={RICH_ACTIVITY}
        initialTab="risk"
      />,
    );
    expect(screen.getByTestId("detail-tab-risk")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("detail-panel-risk")).toBeInTheDocument();
  });

  it("recognises only the eight tab ids, so a stale URL cannot select a panel that is not there", () => {
    for (const tab of DETAIL_TABS) expect(isDetailTabId(tab.id)).toBe(true);
    expect(isDetailTabId("sectors")).toBe(false);
    expect(isDetailTabId("")).toBe(false);
  });

  it("counts nothing on the Activity tab when the feed could not be read at all", () => {
    const { unmount } = renderWorkspace(
      <PortfolioDetailWorkspace detail={richDetail()} nav={richNav()} activity={[]} />,
    );
    expect(screen.getByTestId("detail-tab-activity")).toHaveTextContent("0");
    unmount();

    renderWorkspace(
      <PortfolioDetailWorkspace detail={richDetail()} nav={richNav()} activity={null} />,
    );
    /* "Nothing happened here" and "we could not ask" are opposite messages, and a 0 on the tab
       would render the second as the first. */
    expect(screen.getByTestId("detail-tab-activity")).not.toHaveTextContent("0");
  });

  it("names the question each of the tabs answers, so a label carries its purpose", async () => {
    renderWorkspace(workspace());
    const user = userEvent.setup();
    await user.click(screen.getByTestId("detail-tab-risk"));
    expect(screen.getByTestId("detail-tab-question")).toHaveTextContent(
      "What could hurt, and what do we not yet measure?",
    );
  });
});
