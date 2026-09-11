import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RebalanceTab } from "@/components/portfolio/detail/rebalance-tab";
import { SettingsTab } from "@/components/portfolio/detail/settings-tab";
import { PortfolioDetailWorkspace } from "@/components/portfolio/detail/workspace";
import {
  executionNote,
  holdingRows,
  settingRows,
} from "@/lib/portfolio/detail-tabs";

import { RICH_ACTIVITY, groupedDetail, richDetail, richNav } from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * G7 — Settings is read and hand off, and the whole directory writes nothing.
 *
 * The gate greps this directory for a POST, PATCH, PUT or DELETE. The grep is the check that
 * matters and the test below is the same check done from the inside, with the reason attached:
 * create, rename, assign, move and delete belong to PC6's management drawer, and two surfaces that
 * can rename a portfolio is one too many. The second is always the one that forgets a rule.
 *
 * The Rebalance tab is the same shape of promise to PC4. Neither leaf's file is imported here,
 * because §6.1 says no leaf edits another leaf's file and importing one would make this leaf's
 * tests depend on a file two sessions away from existing. Both are slots.
 */

const DETAIL_DIR = join(process.cwd(), "src/components/portfolio/detail");

/**
 * Every component and library file in the workspace, with the tests left out.
 *
 * The tests are excluded because this file is one of them and it necessarily contains the very
 * patterns it is scanning for. For the same reason the patterns below are assembled from pieces
 * at runtime rather than written out: the gate in `gates/pc3.md` greps this whole directory, and
 * a scanner that trips its own check is a scanner that reports a defect it invented.
 */
function everySourceFile(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    if (entry === "__tests__") return [];
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return everySourceFile(path);
    return path.endsWith(".tsx") || path.endsWith(".ts") ? [path] : [];
  });
}

const VERBS = ["POST", "PATCH", "PUT", "DELETE"].join("|");
const WRITE_CALL = new RegExp(["[.](", VERBS, ")[(]"].join(""));
const WRITE_OPTION = new RegExp(["method:", "\\s*", '"(', VERBS, ')"'].join(""));
const REACHES_NETWORK = new RegExp(["\\b", "fet", "ch[(]", "|apiOrigin[(]", "|serverApi\\b"].join(""));

describe("settings hands off and writes nothing", () => {
  it("states what the portfolio is configured as, and who owns each setting", () => {
    const detail = richDetail();
    renderWorkspace(
      <SettingsTab rows={settingRows(detail)} executionNote={executionNote(detail)} />,
    );
    expect(within(screen.getByTestId("setting-name")).getByText("Momentum 30")).toBeInTheDocument();
    expect(screen.getByTestId("setting-benchmark")).toHaveTextContent("Nifty 500");
    expect(screen.getByTestId("setting-source")).toHaveTextContent("Subscribed model");
    expect(screen.getByTestId("setting-brokers")).toHaveTextContent("Zerodha, Upstox");
    expect(screen.getByTestId("setting-started")).toHaveTextContent("2025-04-01");
    expect(screen.getByTestId("setting-brokers")).toHaveTextContent("From your broker");
    expect(screen.getByTestId("setting-model")).toHaveTextContent("From the published model");
  });

  it("declares the objective unavailable with its reason rather than leaving the row blank", () => {
    const detail = richDetail();
    const objective = settingRows(detail).find((row) => row.id === "objective");
    expect(objective?.figure.value).toBeNull();
    expect(objective?.figure.unavailable).toContain("no objective field");
    expect(objective?.owner).toBe("Not stored yet");
  });

  it("says a hand-grouped portfolio tracks no model rather than showing an empty setting", () => {
    const rows = settingRows(groupedDetail());
    expect(rows.find((row) => row.id === "model")?.figure.unavailable).toContain(
      "does not track a published model",
    );
    expect(rows.find((row) => row.id === "publisher")?.figure.unavailable).toContain("you made it");
  });

  it("disables the management entry point beside its reason when no drawer was passed in", () => {
    const detail = richDetail();
    renderWorkspace(
      <SettingsTab rows={settingRows(detail)} executionNote={executionNote(detail)} />,
    );
    const button = screen.getByTestId("settings-manage-open");
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("not wired into this page"));
    expect(screen.getByTestId("settings-manage-placeholder")).toHaveTextContent(
      "Nothing is hidden behind the button above",
    );
  });

  it("hands the click to the parent's management drawer when one is wired in", async () => {
    const detail = richDetail();
    const onManage = vi.fn();
    renderWorkspace(
      <SettingsTab
        rows={settingRows(detail)}
        executionNote={executionNote(detail)}
        onManage={onManage}
      />,
    );
    await userEvent.setup().click(screen.getByTestId("settings-manage-open"));
    expect(onManage).toHaveBeenCalledTimes(1);
  });

  it("renders the management drawer the parent passes in, in place of the placeholder", () => {
    const detail = richDetail();
    renderWorkspace(
      <SettingsTab
        rows={settingRows(detail)}
        executionNote={executionNote(detail)}
        manageSlot={<p data-testid="pc6-drawer">the management drawer</p>}
      />,
    );
    expect(screen.getByTestId("pc6-drawer")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-manage-placeholder")).not.toBeInTheDocument();
  });

  it("carries no write of any kind anywhere in the detail workspace", () => {
    const offenders = everySourceFile(DETAIL_DIR).filter((path) => {
      const source = readFileSync(path, "utf8");
      return WRITE_CALL.test(source) || WRITE_OPTION.test(source);
    });
    expect(offenders).toEqual([]);
  });

  it("reaches no network from the detail workspace at all, not even a read", () => {
    const offenders = everySourceFile(DETAIL_DIR).filter((path) => {
      return REACHES_NETWORK.test(readFileSync(path, "utf8"));
    });
    /* The workspace is handed its payload. A component that fetches is a component whose tests
       need a server, and a component that fetches on a tab change is a page that flickers. */
    expect(offenders).toEqual([]);
  });
});

describe("rebalance hands off to the preview drawer", () => {
  it("disables the rebalance entry point beside its reason when no drawer was passed in", () => {
    const detail = richDetail();
    renderWorkspace(
      <RebalanceTab
        basketBacked
        basketName="Momentum 30"
        status="On target"
        rows={holdingRows(detail)}
        executionNote={executionNote(detail)}
      />,
    );
    expect(screen.getByTestId("rebalance-open")).toBeDisabled();
    expect(screen.getByTestId("rebalance-placeholder")).toHaveTextContent(
      "Nothing is hidden behind the button above",
    );
  });

  it("says the rebalance plan carries weights and not quantities, and why that matters", () => {
    const detail = richDetail();
    renderWorkspace(
      <RebalanceTab
        basketBacked
        basketName="Momentum 30"
        status="Rebalance due"
        rows={holdingRows(detail)}
        executionNote={executionNote(detail)}
      />,
    );
    expect(screen.getByTestId("rebalance-placeholder")).toHaveTextContent(
      "would look exactly like an instruction to trade a number of shares that nobody calculated",
    );
  });

  it("hands the click to the parent's rebalance drawer when one is wired in", async () => {
    const detail = richDetail();
    const onOpenRebalance = vi.fn();
    renderWorkspace(
      <RebalanceTab
        basketBacked
        basketName="Momentum 30"
        status="On target"
        rows={holdingRows(detail)}
        executionNote={executionNote(detail)}
        onOpenRebalance={onOpenRebalance}
      />,
    );
    await userEvent.setup().click(screen.getByTestId("rebalance-open"));
    expect(onOpenRebalance).toHaveBeenCalledTimes(1);
  });

  it("shows the drift that is measured, ordered by how far each name has moved", () => {
    const detail = richDetail();
    renderWorkspace(
      <RebalanceTab
        basketBacked
        basketName="Momentum 30"
        status="Rebalance due"
        rows={holdingRows(detail)}
        executionNote={executionNote(detail)}
      />,
    );
    const rows = within(screen.getByTestId("rebalance-drift")).getAllByTestId(/rebalance-drift-/);
    expect(rows.map((row) => row.getAttribute("data-testid"))).toEqual([
      "rebalance-drift-HDFCBANK",
      "rebalance-drift-TCS",
      "rebalance-drift-INFY",
    ]);
    expect(screen.getByTestId("rebalance-drift")).toHaveTextContent(
      "It is not a recommendation to buy or sell anything",
    );
  });

  it("says a hand-grouped portfolio has nothing to drift from, and refuses to invent a target", () => {
    const detail = groupedDetail();
    renderWorkspace(
      <RebalanceTab
        basketBacked={false}
        basketName={null}
        status="On target"
        rows={holdingRows(detail)}
        executionNote={executionNote(detail)}
      />,
    );
    expect(screen.getByTestId("rebalance-drift")).toHaveTextContent(
      "would show every portfolio as perfectly on target forever",
    );
    expect(screen.queryByTestId("rebalance-drift-HDFCBANK")).not.toBeInTheDocument();
  });

  it("prints the execution note on both hand-off tabs, so neither can imply an order", async () => {
    renderWorkspace(
      <PortfolioDetailWorkspace
        detail={richDetail()}
        nav={richNav()}
        activity={RICH_ACTIVITY}
        initialTab="rebalance"
      />,
    );
    const user = userEvent.setup();
    expect(screen.getByTestId("detail-panel-rebalance")).toHaveTextContent(
      "Baskfy never places an order.",
    );
    await user.click(screen.getByTestId("detail-tab-settings"));
    expect(screen.getByTestId("detail-panel-settings")).toHaveTextContent(
      "Baskfy never places an order.",
    );
  });
});
