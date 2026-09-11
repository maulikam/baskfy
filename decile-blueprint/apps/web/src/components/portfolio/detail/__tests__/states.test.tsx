import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PortfolioDetailWorkspace } from "@/components/portfolio/detail/workspace";
import { workspaceStates, type WorkspaceStateId } from "@/lib/portfolio/detail-tabs";

import {
  RICH_ACTIVITY,
  RICH_HOLDINGS,
  ZERODHA,
  barrenDetail,
  emptyDetail,
  richDetail,
  richNav,
  richSummary,
} from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * G9 — every state the brief names is designed here, each with exactly one next action.
 *
 * *Exactly* one is the part worth a test. Two buttons on a problem hands the decision back to the
 * person who came to be told what to do, and the second one is always the button nobody presses.
 * The type makes `action` non-optional, and the assertion below makes it singular.
 *
 * None of these states is phrased as advice about a position. They are statements about the data:
 * what is inconsistent, what is unmeasured, what is unreconciled. Baskfy is not registered to say
 * anything else (D3), so the last test in this file scans the rendered copy for the vocabulary of
 * advice, which is a cheaper guard than remembering.
 */

const EVERY_STATE: readonly WorkspaceStateId[] = [
  "empty",
  "no-broker",
  "partial-sync",
  "stale-prices",
  "reconciliation",
  "missing-cost-basis",
  "no-history",
];

describe("the states the workspace is designed for", () => {
  it("detects an empty portfolio as its own state, not as a portfolio worth nothing", () => {
    const states = workspaceStates(emptyDetail(), null);
    const empty = states.find((state) => state.id === "empty");
    expect(empty?.headline).toContain("Nothing is allocated to this portfolio yet");
    expect(empty?.action.label).toBe("Choose holdings for it");
  });

  it("detects a portfolio in the disconnected-broker state", () => {
    const states = workspaceStates(barrenDetail(), null);
    const broker = states.find((state) => state.id === "no-broker");
    expect(broker?.headline).toContain("No broker account is attached");
    expect(broker?.action.href).toBe("/brokers");
  });

  it("detects the partial-sync state, where some holdings could not be priced", () => {
    const states = workspaceStates(richDetail(), richNav());
    const partial = states.find((state) => state.id === "partial-sync");
    expect(partial?.headline).toBe("1 of 5 holdings could not be priced");
    expect(partial?.detail).toContain("Absent is not zero");
  });

  it("does not raise the partial-sync state when nothing at all could be priced", () => {
    /* Everything unpriced is a different problem from some things unpriced, and calling it
       "partial" would be the wrong word for a portfolio that has simply never been valued. */
    const states = workspaceStates(barrenDetail(), null);
    expect(states.some((state) => state.id === "partial-sync")).toBe(false);
  });

  it("detects the stale-prices state when the marks are older than the positions", () => {
    const detail = richDetail({
      summary: richSummary({ prices_as_of: "2026-09-03", holdings_synced_on: "2026-09-10" }),
    });
    const stale = workspaceStates(detail, richNav()).find((state) => state.id === "stale-prices");
    expect(stale?.headline).toBe("Prices are from 2026-09-03, and your positions from 2026-09-10");
    expect(stale?.detail).toContain("older than the positions they are pricing");
  });

  it("leaves the stale-prices state alone when the two clocks agree", () => {
    const states = workspaceStates(richDetail(), richNav());
    expect(states.some((state) => state.id === "stale-prices")).toBe(false);
  });

  it("detects the reconciliation-mismatch state from the holding, not only from the summary", () => {
    const states = workspaceStates(richDetail(), richNav());
    const mismatch = states.find((state) => state.id === "reconciliation");
    expect(mismatch?.headline).toBe("1 holding does not reconcile with the broker");
    expect(mismatch?.severity).toBe("critical");
    expect(mismatch?.action.href).toBe("/reconcile");
  });

  it("detects the missing-cost-basis state and explains why it is not rendered as a zero", () => {
    const blind = workspaceStates(richDetail(), richNav()).find(
      (state) => state.id === "missing-cost-basis",
    );
    expect(blind?.headline).toBe("1 holding has no purchase price on record");
    expect(blind?.detail).toContain("indistinguishable from a free share");
  });

  it("detects the no-history state, and does not raise it once there is a series", () => {
    expect(workspaceStates(richDetail(), null).some((state) => state.id === "no-history")).toBe(
      true,
    );
    expect(
      workspaceStates(richDetail(), richNav()).some((state) => state.id === "no-history"),
    ).toBe(false);
  });

  it("gives every state exactly one next action, never two and never none", () => {
    const detail = richDetail({
      summary: richSummary({ prices_as_of: "2026-09-03", holdings_synced_on: "2026-09-10" }),
      brokers: [],
    });
    const states = workspaceStates(detail, null);
    expect(states.length).toBeGreaterThan(0);
    for (const state of states) {
      expect(state.action.label.length).toBeGreaterThan(0);
      expect(state.action.href.startsWith("/")).toBe(true);
      expect(Object.keys(state).filter((key) => key.startsWith("action"))).toEqual(["action"]);
    }
  });

  it("can raise every one of the seven states the brief names", () => {
    const raised = new Set<WorkspaceStateId>();
    for (const state of workspaceStates(emptyDetail(), null)) raised.add(state.id);
    const messy = richDetail({
      summary: richSummary({ prices_as_of: "2026-09-03", holdings_synced_on: "2026-09-10" }),
      brokers: [],
      holdings: [...RICH_HOLDINGS],
    });
    for (const state of workspaceStates(messy, null)) raised.add(state.id);
    for (const id of EVERY_STATE) expect(raised.has(id)).toBe(true);
  });

  it("says when each state would have been noticed, rather than letting it read as fresh", () => {
    const dated = workspaceStates(richDetail(), null);
    expect(dated.every((state) => state.since.includes("2026-09-10"))).toBe(true);
    const undated = workspaceStates(barrenDetail(), null);
    expect(undated.every((state) => state.since.includes("undated"))).toBe(true);
  });

  it("renders the states on the overview, above the figures they qualify", () => {
    renderWorkspace(
      <PortfolioDetailWorkspace detail={richDetail()} nav={null} activity={RICH_ACTIVITY} />,
    );
    const panel = screen.getByTestId("detail-panel-overview");
    const states = screen.getByTestId("overview-states");
    const snapshot = screen.getByTestId("overview-snapshot");
    expect(
      states.compareDocumentPosition(snapshot) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(panel).getByTestId("state-reconciliation")).toBeInTheDocument();
  });

  it("marks the severity of each state as a word, so colour is never the only signal", () => {
    renderWorkspace(
      <PortfolioDetailWorkspace detail={richDetail()} nav={null} activity={RICH_ACTIVITY} />,
    );
    expect(screen.getByTestId("state-reconciliation")).toHaveTextContent("Action required");
    expect(screen.getByTestId("state-missing-cost-basis")).toHaveTextContent("Worth a look");
  });

  it("phrases no state as investment advice, which Baskfy is not registered to give", () => {
    const detail = richDetail({
      summary: richSummary({ prices_as_of: "2026-09-03", holdings_synced_on: "2026-09-10" }),
      brokers: [ZERODHA],
    });
    const copy = workspaceStates(detail, null)
      .flatMap((state) => [state.headline, state.detail, state.action.label])
      .join(" ")
      .toLowerCase();
    for (const word of [
      "you should buy",
      "you should sell",
      "we recommend",
      "reduce your position",
      "consider buying",
      "consider selling",
      "take profit",
    ]) {
      expect(copy).not.toContain(word);
    }
  });
});
