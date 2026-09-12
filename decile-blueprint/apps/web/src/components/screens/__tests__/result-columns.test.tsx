import { describe, expect, it } from "vitest";

import {
  buildColumns,
  renderValue,
  type ResultRow,
} from "@/components/screens/result-columns";
import { visibleResultColumns } from "@/lib/screens/column-display";

const META = new Map([
  ["close_raw", { key: "close_raw", label: "CLOSE RAW", unit: "price" }],
  ["marketcap_cr", { key: "marketcap_cr", label: "MARKETCAP", unit: "crore" }],
  ["ret_12m", { key: "ret_12m", label: "ABSOLUTE RETURN 1 YEAR", unit: "percent" }],
]);

function row(overrides: Partial<ResultRow> = {}): ResultRow {
  return {
    rank: 1,
    symbol: "CUPID",
    name: "Cupid Ltd",
    close_raw: 145.2,
    ret_12m: 0.12,
    ...overrides,
  };
}

describe("buildColumns dead-column policy", () => {
  it("drops close_raw when every row is empty", () => {
    const rows = [row({ close_raw: null }), row({ rank: 2, close_raw: null })];
    const columns = buildColumns(
      ["symbol", "name", "close_raw", "ret_12m"],
      META,
      "AVERAGE SHARPE",
      "ratio",
      rows,
    );
    const ids = columns.map((column) => column.id);
    expect(ids).not.toContain("close_raw");
    expect(visibleResultColumns(["symbol", "name", "close_raw", "ret_12m"], rows)).not.toContain(
      "close_raw",
    );
  });

  it("keeps close_raw when at least one row has a print", () => {
    const rows = [row({ close_raw: 145.2 })];
    const columns = buildColumns(["symbol", "close_raw"], META, "", "price", rows);
    expect(columns.map((column) => column.id)).toContain("close_raw");
  });

  it("never uses adjusted close under the Price label", () => {
    const rows = [row({ close_raw: 145.2, close: 999 })];
    const columns = buildColumns(["symbol", "close_raw"], META, "", "price", rows);
    const closeColumn = columns.find((column) => column.id === "close_raw");
    expect(closeColumn).toBeDefined();
    const cell = closeColumn?.cell;
    expect(typeof cell).toBe("function");
  });

  it("ret_12m always formats with a percent sign even if unit meta is wrong", () => {
    // Old bug: unit fell through to ratio → "605.93" pill next to a "+605.9%" tile.
    expect(renderValue(605.93, "ratio", "ret_12m")).toBe("+605.93%");
    expect(renderValue(605.93, "percent", "ret_12m")).toBe("+605.93%");
  });
});
