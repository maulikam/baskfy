import { describe, expect, it } from "vitest";

import {
  COLUMN_DISPLAY,
  columnDisplayLabel,
  columnDisplayTooltip,
  suppressedResultColumns,
  visibleResultColumns,
} from "@/lib/screens/column-display";
import { countDefinitionChanges } from "@/lib/screens/change-count";
import { defaultDefinition } from "@/lib/screens/defaults";
import { definitionsEqual } from "@/lib/screens/url-state";

describe("countDefinitionChanges", () => {
  it("returns zero for identical definitions", () => {
    const base = defaultDefinition();
    expect(countDefinitionChanges(base, base)).toBe(0);
    expect(definitionsEqual(base, base)).toBe(true);
  });

  it("counts leaf field changes", () => {
    const base = defaultDefinition();
    const changed = { ...base, median_volume_1y: 10_000_000 };
    expect(countDefinitionChanges(base, changed)).toBe(1);
  });

  it("counts nested group changes", () => {
    const base = defaultDefinition();
    const changed = {
      ...base,
      moving_average: { ...base.moving_average, above_200: true },
    };
    expect(countDefinitionChanges(base, changed)).toBe(1);
  });
});

/** The column set the API serves for the example momentum screen. */
const SCREEN_COLUMNS: readonly string[] = Object.freeze([
  "symbol",
  "name",
  "sorting_factor",
  "close_raw",
  "series",
  "marketcap_cr",
  "ret_12m",
  "sharpe_12m",
  "vol_12m",
  "beta_12m",
  "ma_200",
]);

/** What the §2.2 diet leaves of it. */
const DIETED: readonly string[] = Object.freeze([
  "symbol",
  "sorting_factor",
  "close_raw",
  "ret_12m",
  "vol_12m",
]);

function populatedRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    rank: 1,
    symbol: "CUPID",
    name: "Cupid Ltd",
    sorting_factor: 1.42,
    close_raw: 118.4,
    series: "EQ",
    marketcap_cr: 892.1,
    ret_12m: 6.0837,
    sharpe_12m: 2.11,
    vol_12m: 0.4812,
    beta_12m: 0.88,
    ma_200: 96.2,
    ...overrides,
  };
}

/**
 * A row whose reads are observable, so the cost of the scan can be asserted rather than assumed.
 */
function countingRow(
  values: Record<string, unknown>,
  onRead: (key: string) => void,
): Record<string, unknown> {
  const row: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    Object.defineProperty(row, key, {
      enumerable: true,
      get: () => {
        onRead(key);
        return value;
      },
    });
  }
  return row;
}

describe("column diet", () => {
  it("hides legacy default columns from the table", () => {
    expect(visibleResultColumns(SCREEN_COLUMNS)).toEqual([...DIETED]);
  });

  it("keeps custom columns outside the legacy default set", () => {
    const columns = ["symbol", "name", "sorting_factor", "ret_12m", "vol_12m", "close_raw", "rsi_1y"];
    expect(visibleResultColumns(columns)).toContain("rsi_1y");
  });

  it("shows name only when there is no symbol column to fold it into", () => {
    expect(visibleResultColumns(["name", "sorting_factor"])).toEqual(["name", "sorting_factor"]);
    expect(visibleResultColumns(["symbol", "name"])).toEqual(["symbol"]);
  });

  it("is unchanged when rows are omitted or explicitly undefined", () => {
    expect(visibleResultColumns(SCREEN_COLUMNS, undefined)).toEqual(
      visibleResultColumns(SCREEN_COLUMNS),
    );
  });

  it("is unchanged by rows in which every column carries data", () => {
    const rows = [populatedRow(), populatedRow({ symbol: "AURIONPRO", rank: 2 })];
    expect(visibleResultColumns(SCREEN_COLUMNS, rows)).toEqual([...DIETED]);
    expect(suppressedResultColumns(SCREEN_COLUMNS, rows)).toEqual([]);
  });
});

describe("dead-column policy", () => {
  it("drops a column whose value is empty in every row", () => {
    // The live fault: the seeded reference export carries `close` and never `close_raw`, so the
    // Price header sat above 271 em dashes.
    const rows = Array.from({ length: 271 }, (_unused, index) =>
      populatedRow({ rank: index + 1, close_raw: null }),
    );

    expect(visibleResultColumns(SCREEN_COLUMNS, rows)).toEqual([
      "symbol",
      "sorting_factor",
      "ret_12m",
      "vol_12m",
    ]);
  });

  it("counts null, undefined and the empty string as empty, and nothing else", () => {
    const columns = ["symbol", "a_null", "b_undefined", "c_blank", "d_space"];
    const rows = [
      { symbol: "CUPID", a_null: null, b_undefined: undefined, c_blank: "", d_space: " " },
      { symbol: "AURIONPRO", a_null: null, b_undefined: undefined, c_blank: "", d_space: " " },
    ];

    // A space renders as a space, not as an em dash, so the column is not dead.
    expect(visibleResultColumns(columns, rows)).toEqual(["symbol", "d_space"]);
  });

  it("treats a column missing from every row object as empty", () => {
    const columns = ["symbol", "sorting_factor", "rsi_1y"];
    const rows = [
      { symbol: "CUPID", sorting_factor: 1.42 },
      { symbol: "AURIONPRO", sorting_factor: 1.31 },
    ];

    expect(visibleResultColumns(columns, rows)).toEqual(["symbol", "sorting_factor"]);
    expect(suppressedResultColumns(columns, rows)).toEqual(["rsi_1y"]);
  });

  it("does not mistake an inherited property for data", () => {
    // `toString` resolves through Object.prototype on any JSON-parsed row; only own properties
    // are data.
    const columns = ["symbol", "toString"];
    const rows = [{ symbol: "CUPID" }, { symbol: "AURIONPRO" }];

    expect(visibleResultColumns(columns, rows)).toEqual(["symbol"]);
    expect(suppressedResultColumns(columns, rows)).toEqual(["toString"]);
  });

  it("keeps a column with even one non-empty value among many empty ones", () => {
    const rows = Array.from({ length: 500 }, (_unused, index) =>
      populatedRow({ rank: index + 1, close_raw: index === 499 ? 118.4 : null }),
    );

    expect(visibleResultColumns(SCREEN_COLUMNS, rows)).toEqual([...DIETED]);
    expect(suppressedResultColumns(SCREEN_COLUMNS, rows)).toEqual([]);
  });

  it("decides on a single row exactly as it would on many", () => {
    const one = [populatedRow({ close_raw: null })];
    expect(visibleResultColumns(SCREEN_COLUMNS, one)).toEqual([
      "symbol",
      "sorting_factor",
      "ret_12m",
      "vol_12m",
    ]);

    const oneLive = [populatedRow()];
    expect(visibleResultColumns(SCREEN_COLUMNS, oneLive)).toEqual([...DIETED]);
  });

  it("returns the whole diet for an empty row set — no rows is no evidence", () => {
    // A screen that matched nothing must render its configured headers over an empty state, not
    // collapse to its identity columns.
    expect(visibleResultColumns(SCREEN_COLUMNS, [])).toEqual([...DIETED]);
    expect(suppressedResultColumns(SCREEN_COLUMNS, [])).toEqual([]);
  });

  it("drops every occurrence of a duplicated dead column and keeps every live one", () => {
    const columns = ["symbol", "close_raw", "close_raw", "ret_12m", "ret_12m"];
    const rows = [populatedRow({ close_raw: null })];

    expect(visibleResultColumns(columns, rows)).toEqual(["symbol", "ret_12m", "ret_12m"]);
  });

  it("does not mutate the columns or the rows it is given", () => {
    const columns = Object.freeze(["symbol", "close_raw", "ret_12m"]);
    const row = Object.freeze(populatedRow({ close_raw: null }));
    const rows = Object.freeze([row]);

    expect(() => visibleResultColumns(columns, rows)).not.toThrow();
    expect(() => suppressedResultColumns(columns, rows)).not.toThrow();
    expect(columns).toEqual(["symbol", "close_raw", "ret_12m"]);
    expect(Object.keys(row)).toContain("close_raw");
  });
});

describe("identity columns", () => {
  it("never drops rank, symbol, name or sorting_factor for lack of data", () => {
    const columns = ["rank", "name", "sorting_factor", "ret_12m"];
    const rows = [
      { rank: null, name: "", sorting_factor: undefined, ret_12m: null },
      { rank: null, name: "", sorting_factor: undefined, ret_12m: null },
    ];

    expect(visibleResultColumns(columns, rows)).toEqual(["rank", "name", "sorting_factor"]);
    expect(suppressedResultColumns(columns, rows)).toEqual(["ret_12m"]);
  });

  it("never drops symbol for lack of data", () => {
    const rows = [{ symbol: null, sorting_factor: null }];
    expect(visibleResultColumns(["symbol", "sorting_factor"], rows)).toEqual([
      "symbol",
      "sorting_factor",
    ]);
  });

  it("keeps identity protection scoped to the data rule, so name still folds into symbol", () => {
    // `name` is exempt from the emptiness rule; it is not exempt from the diet, which renders it
    // inside the symbol cell whenever both are present.
    const rows = [populatedRow()];
    expect(visibleResultColumns(["symbol", "name", "sorting_factor"], rows)).toEqual([
      "symbol",
      "sorting_factor",
    ]);
    expect(suppressedResultColumns(["symbol", "name", "sorting_factor"], rows)).toEqual([]);
  });

  it("never discloses an identity column as suppressed", () => {
    const columns = ["rank", "name", "sorting_factor"];
    const rows = [{ rank: null, name: null, sorting_factor: null }];
    expect(suppressedResultColumns(columns, rows)).toEqual([]);
  });
});

describe("zero, false and NaN are data", () => {
  it("keeps the return column when every stock returned exactly zero", () => {
    const rows = Array.from({ length: 271 }, (_unused, index) =>
      populatedRow({ rank: index + 1, ret_12m: 0 }),
    );

    expect(visibleResultColumns(SCREEN_COLUMNS, rows)).toContain("ret_12m");
    expect(suppressedResultColumns(SCREEN_COLUMNS, rows)).toEqual([]);
  });

  it("keeps a column of zeros, of falses and of NaNs", () => {
    const columns = ["symbol", "zeroes", "falses", "nans", "negative_zeroes"];
    const rows = [
      { symbol: "CUPID", zeroes: 0, falses: false, nans: Number.NaN, negative_zeroes: -0 },
      { symbol: "AURIONPRO", zeroes: 0, falses: false, nans: Number.NaN, negative_zeroes: -0 },
    ];

    expect(visibleResultColumns(columns, rows)).toEqual([...columns]);
    expect(suppressedResultColumns(columns, rows)).toEqual([]);
  });

  it("keeps a column whose only non-empty value in 500 rows is a zero", () => {
    const rows = Array.from({ length: 500 }, (_unused, index) =>
      populatedRow({ rank: index + 1, vol_12m: index === 250 ? 0 : null }),
    );

    expect(visibleResultColumns(SCREEN_COLUMNS, rows)).toContain("vol_12m");
  });
});

describe("suppressed columns", () => {
  it("names exactly what was dropped for lack of data", () => {
    const columns = ["symbol", "sorting_factor", "close_raw", "ret_12m", "rsi_1y"];
    const rows = [
      populatedRow({ close_raw: null, rsi_1y: "" }),
      populatedRow({ close_raw: null, rsi_1y: "" }),
    ];

    expect(suppressedResultColumns(columns, rows)).toEqual(["close_raw", "rsi_1y"]);
  });

  it("excludes the columns the diet drops — the user did not ask for those", () => {
    // marketcap_cr, series, sharpe_12m, beta_12m and ma_200 are all absent from the rows, and all
    // five are diet casualties. Only close_raw is a column the user asked for and we cannot fill.
    const rows = [
      { rank: 1, symbol: "CUPID", name: "Cupid Ltd", sorting_factor: 1.42, ret_12m: 6.08, vol_12m: 0.48 },
    ];

    expect(suppressedResultColumns(SCREEN_COLUMNS, rows)).toEqual(["close_raw"]);
  });

  it("discloses a duplicated dead column once", () => {
    const columns = ["symbol", "close_raw", "close_raw"];
    const rows = [populatedRow({ close_raw: null })];
    expect(suppressedResultColumns(columns, rows)).toEqual(["close_raw"]);
  });

  it("partitions the dieted column set together with the visible columns", () => {
    const columns = [...SCREEN_COLUMNS, "rsi_1y"];
    const rows = [populatedRow({ close_raw: null, rsi_1y: null })];

    const visible = visibleResultColumns(columns, rows);
    const suppressed = suppressedResultColumns(columns, rows);

    expect(new Set([...visible, ...suppressed])).toEqual(new Set(visibleResultColumns(columns)));
    expect(visible.filter((key) => suppressed.includes(key))).toEqual([]);
  });

  it("is empty when no rows are supplied to judge by", () => {
    expect(suppressedResultColumns(SCREEN_COLUMNS, [])).toEqual([]);
  });
});

describe("scan cost", () => {
  it("stops reading rows once every column is proven populated", () => {
    let reads = 0;
    const rows = Array.from({ length: 20_000 }, () =>
      countingRow(populatedRow(), () => {
        reads += 1;
      }),
    );

    expect(visibleResultColumns(SCREEN_COLUMNS, rows)).toEqual([...DIETED]);
    // One row was enough; anything near 20_000 would mean the scan is cols x rows.
    expect(reads).toBeLessThanOrEqual(DIETED.length);
  });

  it("reads a proven column only once even when another column is dead throughout", () => {
    let reads = 0;
    const rows = Array.from({ length: 20_000 }, (_unused, index) =>
      countingRow(populatedRow({ rank: index + 1, close_raw: null }), () => {
        reads += 1;
      }),
    );

    expect(visibleResultColumns(SCREEN_COLUMNS, rows)).not.toContain("close_raw");
    // Row 0 proves the other columns; every later row reads only the one open question.
    expect(reads).toBeLessThanOrEqual(DIETED.length + rows.length);
  });
});

describe("human label map (§1.3)", () => {
  const EXPECTED: Array<[string, string]> = [
    ["avg_sharpe_12_6_3_1", "Consistency score"],
    ["ret_12m", "1-yr return"],
    ["sharpe_12m", "Return vs risk (1 yr)"],
    ["vol_12m", "Bumpiness"],
    ["close_raw", "Price"],
    ["ma_200", "200-day average"],
    ["beta_12m", "Moves with market"],
  ];

  it.each(EXPECTED)("labels %s as its plain-English name", (key, label) => {
    expect(columnDisplayLabel(key, "TECHNICAL")).toBe(label);
  });

  it.each(EXPECTED)("gives %s a technical tooltip", (key) => {
    const tooltip = columnDisplayTooltip(key);
    expect(tooltip).toBeTruthy();
    expect(tooltip).not.toBe(columnDisplayLabel(key, "TECHNICAL"));
  });

  it("falls back to the technical label for an unmapped key", () => {
    expect(columnDisplayLabel("rsi_1y", "RSI (1y)")).toBe("RSI (1y)");
    expect(columnDisplayTooltip("rsi_1y")).toBeUndefined();
  });

  it("labels the sorting factor column and the marketcap and series columns", () => {
    expect(columnDisplayLabel("sorting_factor", "Score")).toBe("Consistency score");
    expect(columnDisplayLabel("marketcap_cr", "Market Cap")).toBe("Market cap");
    expect(columnDisplayLabel("series", "Series")).toBe("Series");
  });

  it("gives every mapped column both a label and a tooltip", () => {
    for (const [key, display] of Object.entries(COLUMN_DISPLAY)) {
      expect(display.label, key).toBeTruthy();
      expect(display.tooltip, key).toBeTruthy();
    }
  });
});
