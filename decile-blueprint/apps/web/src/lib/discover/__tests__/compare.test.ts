import { describe, expect, it } from "vitest";

import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";
import { EMPTY_CELL } from "@/lib/format";
import { UNCOMPUTED_METRIC_KEYS } from "@/lib/discover/metrics";
import {
  SECTION_ORDER,
  buildComparison,
  commonWindow,
  differences,
  rowsBySection,
  windowNote,
} from "@/lib/discover/compare";

function metrics(over: Partial<ExploreMetrics> = {}): ExploreMetrics {
  return {
    as_of_date: "2026-08-22",
    min_amount: "300000.00",
    volatility_bucket: "MEDIUM",
    volatility_value: "0.1820000000",
    ret_1m: "1.10",
    ret_6m: "9.40",
    ret_1y: "22.10",
    cagr_3y: null,
    cagr_5y: null,
    since_inception_pct: null,
    headline_label: "1Y returns",
    headline_pct: "22.10",
    return_convention: "PRICE_RETURN",
    dividends_included: false,
    return_convention_note: "Price return.",
    ...over,
  };
}

function card(slug: string, over: Partial<ExploreBasketCard> = {}): ExploreBasketCard {
  return {
    slug,
    name: slug.replace(/-/g, " "),
    access: "FREE",
    visibility: "PUBLISHED",
    type: "STOCK",
    categories: ["momentum"],
    rebalance_frequency: "QUARTERLY",
    source: "SCAN",
    description_md: null,
    launched_at: "2024-01-01",
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: metrics(),
    ...over,
  };
}

describe("the window is the same for every column, or the table lies", () => {
  it("uses the longest window every basket has", () => {
    const older = card("older", { metrics: metrics({ cagr_5y: "18.00", cagr_3y: "20.00" }) });
    const alsoOlder = card("also", { metrics: metrics({ cagr_5y: "14.00", cagr_3y: "16.00" }) });
    expect(commonWindow([older, alsoOlder]).label).toBe("5Y CAGR");
  });

  it("shortens to the window the youngest basket can support", () => {
    // The failure this prevents: a young basket's 1Y beside an old basket's 5Y CAGR, in a row
    // labelled "Return". One good year is not an annualised five-year figure.
    const old = card("old", { metrics: metrics({ cagr_5y: "18.00", cagr_3y: "20.00" }) });
    const young = card("young", { metrics: metrics() });
    const window = commonWindow([old, young]);
    expect(window.label).toBe("1Y return");
    expect(window.shortenedFrom).toBe("5Y CAGR");
  });

  it("says out loud that it shortened, and names who was too young", () => {
    const old = card("old", { metrics: metrics({ cagr_5y: "18.00" }) });
    const young = card("young", { metrics: metrics() });
    const note = windowNote(commonWindow([old, young]), [old, young]);
    expect(note).toMatch(/longest window all of these baskets share/);
    expect(note).toMatch(/would flatter it/);
    expect(note).toMatch(/young has less history/);
  });

  it("says so plainly when there is no shared window at all", () => {
    const bare = card("bare", {
      metrics: metrics({ ret_1m: null, ret_6m: null, ret_1y: null }),
    });
    const window = commonWindow([card("full"), bare]);
    expect(window.key).toBeNull();
    expect(windowNote(window, [card("full"), bare])).toMatch(/share no return window/);
  });

  it("labels the return row with the window it settled on", () => {
    const { rows } = buildComparison([card("a"), card("b")]);
    expect(rows.find((r) => r.key === "return")?.label).toBe("Return (1Y return)");
  });

  it("leaves the return row empty rather than mixing windows", () => {
    const bare = card("bare", { metrics: metrics({ ret_1m: null, ret_6m: null, ret_1y: null }) });
    const { rows } = buildComparison([card("full"), bare]);
    const returnRow = rows.find((r) => r.key === "return");
    expect(returnRow?.cells.every((c) => c.value === EMPTY_CELL)).toBe(true);
    expect(returnRow?.cells[0]?.absence?.kind).toBe("too-young");
  });
});

describe("absent is visible, not omitted", () => {
  it("keeps a row for every metric nobody computes", () => {
    // Dropping the drawdown row lets a reader conclude the baskets are alike on drawdown.
    const { rows } = buildComparison([card("a"), card("b")]);
    for (const key of UNCOMPUTED_METRIC_KEYS) {
      const row = rows.find((r) => r.key === key);
      expect(row, `${key} has a row`).toBeDefined();
      expect(row!.anyAvailable).toBe(false);
      expect(row!.cells.every((c) => c.value === EMPTY_CELL)).toBe(true);
      expect(row!.cells[0]?.absence?.note).toMatch(/Not computed yet/);
    }
  });

  it("files the uncomputed rows under the section a reader would look in", () => {
    const { rows } = buildComparison([card("a"), card("b")]);
    expect(rows.find((r) => r.key === "max_drawdown")?.section).toBe("Downside");
    expect(rows.find((r) => r.key === "turnover_pct")?.section).toBe("Operations");
    expect(rows.find((r) => r.key === "rolling_1y_positive_pct")?.section).toBe("Consistency");
    expect(rows.find((r) => r.key === "top10_concentration_pct")?.section).toBe("Portfolio");
  });

  it("states the return convention rather than letting a reader assume it", () => {
    const { rows } = buildComparison([card("a")]);
    expect(rows.find((r) => r.key === "return_convention")?.cells[0]?.value).toMatch(
      /dividends not included/,
    );
  });

  it("carries the blended-volatility caveat beside the volatility row", () => {
    const { rows } = buildComparison([card("a")]);
    expect(rows.find((r) => r.key === "volatility")?.explain).toMatch(/reads high/);
  });
});

describe("sections", () => {
  it("orders sections with performance first and downside immediately after", () => {
    expect(SECTION_ORDER[0]).toBe("Performance");
    expect(SECTION_ORDER[1]).toBe("Downside");
  });

  it("groups rows and drops sections with nothing in them", () => {
    const { rows } = buildComparison([card("a"), card("b")]);
    const grouped = rowsBySection(rows);
    expect(grouped.map(([section]) => section)[0]).toBe("Performance");
    for (const [, sectionRows] of grouped) expect(sectionRows.length).toBeGreaterThan(0);
  });
});

describe("what is different", () => {
  it("reports only rows where the columns actually disagree", () => {
    const a = card("a", { rebalance_frequency: "MONTHLY" });
    const b = card("b", { rebalance_frequency: "QUARTERLY" });
    const { rows } = buildComparison([a, b]);
    const keys = differences(rows).map((r) => r.key);
    expect(keys).toContain("rebalance");
    expect(keys).not.toContain("manager");
  });

  it("never calls two blanks a difference", () => {
    // Otherwise the metrics gap would generate false signal on every comparison.
    const { rows } = buildComparison([card("a"), card("b")]);
    for (const key of UNCOMPUTED_METRIC_KEYS) {
      expect(differences(rows).map((r) => r.key)).not.toContain(key);
    }
  });

  it("finds nothing to report between two identical baskets", () => {
    const { rows } = buildComparison([card("a"), card("a")]);
    expect(differences(rows)).toHaveLength(0);
  });
});

describe("every row explains its own measure", () => {
  it("gives a reader something to read when they ask what a number means", () => {
    const { rows } = buildComparison([card("a"), card("b")]);
    for (const row of rows) {
      expect(row.explain.length, `${row.key} explains itself`).toBeGreaterThan(20);
    }
  });

  it("produces one cell per basket on every row", () => {
    const baskets = [card("a"), card("b"), card("c")];
    const { rows } = buildComparison(baskets);
    for (const row of rows) {
      expect(row.cells.map((c) => c.slug)).toEqual(["a", "b", "c"]);
    }
  });
});
