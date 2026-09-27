import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SleeveStateBadge, SleeveStateChip } from "@/components/screens/sleeve-state";
import type * as SleeveStateModule from "@/lib/screens/sleeve-state";
import {
  STATE_LABEL,
  parseSleeveStates,
  stateTone,
  type SleeveState,
} from "@/lib/screens/sleeve-state";

/**
 * LV4 — the chip beside each Scan button says what the book is doing and why, and never
 * calls a closed-session re-detection a live or intraday scan.
 */

let current: SleeveState[] = [];

vi.mock("@/lib/screens/sleeve-state", async (importOriginal) => {
  const actual = await importOriginal<typeof SleeveStateModule>();
  return { ...actual, useSleeveStates: () => current };
});

afterEach(() => {
  cleanup();
  current = [];
});

const twt: SleeveState = {
  sleeve: "twt",
  state: "missed_window",
  reason: "the 09:05 MORNING plan expired at 09:35 — TWT enters at the open only",
  scanMeans: "Scan re-detects the last published session; TWT enters at the next open only",
  asOf: "2026-09-28",
  next: "tomorrow's plan, built at 21:20 and re-sized at 09:05",
  updatedAt: "2026-09-28T10:30:00+05:30",
};

describe("SleeveStateChip", () => {
  it("shows the state word with the reason, next step and Scan meaning on hover", () => {
    render(<SleeveStateChip state={twt} />);
    const chip = screen.getByTestId("sleeve-state-twt");
    expect(chip).toHaveTextContent("Entry window missed");
    expect(chip).toHaveAttribute("data-state", "missed_window");
    expect(chip.getAttribute("title")).toContain("expired at 09:35");
    expect(chip.getAttribute("title")).toContain("Next: tomorrow's plan");
    expect(chip.getAttribute("title")).toContain("last published session");
  });

  it("never labels a TWT or VBT scan live or intraday", () => {
    for (const label of Object.values(STATE_LABEL)) {
      expect(label.toLowerCase()).not.toContain("live");
      expect(label.toLowerCase()).not.toContain("intraday");
    }
  });

  it("tones: blocked is negative, a missed window warns, monitoring is positive", () => {
    expect(stateTone("blocked")).toBe("negative");
    expect(stateTone("missed_window")).toBe("warning");
    expect(stateTone("waiting_for_login")).toBe("warning");
    expect(stateTone("monitoring")).toBe("positive");
    expect(stateTone("scanning")).toBe("accent");
    expect(stateTone("idle")).toBe("neutral");
  });
});

describe("SleeveStateBadge", () => {
  it("renders its own sleeve's chip from the shared poll and nothing before the first answer", () => {
    const { rerender } = render(<SleeveStateBadge sleeve="twt" />);
    expect(screen.queryByTestId("sleeve-state-twt")).toBeNull();
    current = [twt, { ...twt, sleeve: "swing", state: "monitoring", reason: "watching 5" }];
    rerender(<SleeveStateBadge sleeve="twt" />);
    expect(screen.getByTestId("sleeve-state-twt")).toHaveTextContent("Entry window missed");
    expect(screen.queryByTestId("sleeve-state-swing")).toBeNull();
  });
});

describe("parseSleeveStates", () => {
  it("reads the wire shape and drops rows it does not understand", () => {
    const rows = parseSleeveStates([
      {
        sleeve: "swing",
        state: "monitoring",
        reason: "r",
        scan_means: "m",
        as_of: null,
        next: "n",
        updated_at: "t",
      },
      { sleeve: "options", state: "monitoring" },
      { sleeve: "vbt", state: "flying" },
    ]);
    expect(rows).toEqual([
      { sleeve: "swing", state: "monitoring", reason: "r", scanMeans: "m", asOf: null, next: "n", updatedAt: "t" },
    ]);
    expect(parseSleeveStates({ not: "an array" })).toEqual([]);
  });
});
