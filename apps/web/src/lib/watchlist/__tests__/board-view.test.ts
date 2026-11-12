import { describe, expect, it } from "vitest";

import type { LiveQuote } from "@/lib/screens/live-marks";
import type { WatchBoardItem } from "@/lib/watchlist/board-types";
import {
  addErrorMessage,
  boardRows,
  defaultAscending,
  formatMove,
  formatVolumeRatio,
  moveTone,
  normaliseSymbol,
  sortBoard,
} from "@/lib/watchlist/board-view";

function item(symbol: string, over: Partial<WatchBoardItem> = {}): WatchBoardItem {
  return {
    symbol,
    name: null,
    sector: null,
    watched_at: null,
    last_date: "2026-11-11",
    close: "100.00",
    day_change_fraction: "0.0100",
    week_change_fraction: null,
    month_change_fraction: null,
    three_month_change_fraction: null,
    day_volume: null,
    week_volume: null,
    month_volume: null,
    volume_vs_avg20: null,
    doubling_score: null,
    doubling_rank: null,
    doubling_scope: null,
    swing_score: null,
    swing_rank: null,
    swing_scope: null,
    swing_picked: false,
    ...over,
  };
}

const quote = (over: Partial<LiveQuote> = {}): LiveQuote => ({
  lastPrice: 110,
  prevClose: 100,
  changePct: 10,
  stale: false,
  asOf: null,
  ...over,
});

describe("boardRows: today's move", () => {
  it("keeps the published close and its move when the overlay is not live", () => {
    const [row] = boardRows([item("SUNTV")], { SUNTV: quote() }, false);
    expect(row?.source).toBe("close");
    expect(row?.price).toBe(100);
    expect(row?.dayFraction).toBeCloseTo(0.01);
  });

  it("uses the Kite quote while live, as a fraction", () => {
    const [row] = boardRows([item("SUNTV")], { SUNTV: quote() }, true);
    expect(row?.source).toBe("live");
    expect(row?.price).toBe(110);
    expect(row?.dayFraction).toBeCloseTo(0.1);
  });

  it("marks a stale quote stale, never live", () => {
    const [row] = boardRows([item("SUNTV")], { SUNTV: quote({ stale: true }) }, true);
    expect(row?.source).toBe("stale");
  });

  it("stays on the close for a name the live answer did not quote", () => {
    const [row] = boardRows([item("RATNAVEER")], { SUNTV: quote() }, true);
    expect(row?.source).toBe("close");
  });
});

describe("sortBoard", () => {
  const rows = boardRows(
    [
      item("B", { week_change_fraction: "0.05", day_volume: 10 }),
      item("A", { week_change_fraction: "-0.02", day_volume: null }),
      item("C", { week_change_fraction: null, day_volume: 30 }),
      item("D", { week_change_fraction: "0.05", day_volume: 20 }),
    ],
    {},
    false,
  );

  it("sorts numbers biggest first by default and sinks missing values", () => {
    expect(defaultAscending("week")).toBe(false);
    expect(sortBoard(rows, "week", false).map((r) => r.symbol)).toEqual(["B", "D", "A", "C"]);
  });

  it("keeps missing values at the bottom when ascending too", () => {
    expect(sortBoard(rows, "week", true).map((r) => r.symbol)).toEqual(["A", "B", "D", "C"]);
    expect(sortBoard(rows, "day_volume", true).map((r) => r.symbol)).toEqual(["B", "D", "C", "A"]);
  });

  it("sorts symbols A to Z by default", () => {
    expect(defaultAscending("symbol")).toBe(true);
    expect(sortBoard(rows, "symbol", true).map((r) => r.symbol)).toEqual(["A", "B", "C", "D"]);
  });

  it("does not mutate its input", () => {
    const before = rows.map((r) => r.symbol);
    sortBoard(rows, "symbol", false);
    expect(rows.map((r) => r.symbol)).toEqual(before);
  });
});

describe("formatting", () => {
  it("pairs the arrow with the sign", () => {
    expect(formatMove(0.0124)).toBe("▲ +1.24%");
    expect(formatMove(-0.008)).toBe("▼ −0.80%");
    expect(formatMove(0)).toBe("— 0.00%");
    expect(formatMove(null)).toBe("—");
  });

  it("colours up green, down red, flat and missing muted", () => {
    expect(moveTone(0.01)).toBe("text-positive");
    expect(moveTone(-0.01)).toBe("text-negative");
    expect(moveTone(0)).toBe("text-muted-foreground");
    expect(moveTone(null)).toBe("text-muted-foreground");
  });

  it("states the volume spike against the 20-day average", () => {
    expect(formatVolumeRatio("2.345")).toBe("×2.3 vs 20-day avg");
    expect(formatVolumeRatio(null)).toBe("");
  });
});

describe("the Add box", () => {
  it("upper-cases, trims and drops an NSE: prefix", () => {
    expect(normaliseSymbol("  suntv ")).toBe("SUNTV");
    expect(normaliseSymbol("nse:m&m")).toBe("M&M");
    expect(normaliseSymbol("BAJAJ-AUTO")).toBe("BAJAJ-AUTO");
  });

  it("refuses what cannot be a symbol", () => {
    expect(normaliseSymbol("")).toBeNull();
    expect(normaliseSymbol("SUN TV")).toBeNull();
    expect(normaliseSymbol("<script>")).toBeNull();
  });

  it("says plainly when the symbol is unknown", () => {
    expect(addErrorMessage(404, "XYZ")).toContain("No listed stock called XYZ");
  });
});
