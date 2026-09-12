import type { ScreenRunResponse } from "@baskfy/api-client";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ColumnMeta } from "@/components/screens/result-columns";
import { ResultsPanel } from "@/components/screens/results-panel";
import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * Leaf 1.1.2: the results panel must actually run the dead-column policy (pass rows into
 * buildColumns), disclose what it hid, put the sort note on the table once, and offer undo
 * on an empty result.
 */

const SORT_NOTE =
  "Sorting a column re-orders these rows in the browser. It does not re-run the screen, so the ranks stay as the server computed them.";

const META = new Map<string, ColumnMeta>(
  (
    [
      ["sorting_factor", "Sharpe 12M", "ratio"],
      ["ret_12m", "1Y Return", "percent"],
      ["vol_12m", "Volatility 1Y", "fraction"],
      ["close_raw", "Close", "price"],
      ["marketcap_cr", "Market Cap", "crore"],
    ] as const
  ).map(([key, label, unit]) => [key, { key, label, unit }]),
);

function row(overrides: Record<string, unknown> = {}) {
  return {
    rank: 1,
    symbol: "CUPID",
    name: "Cupid Limited",
    sorting_factor: 5.13,
    ret_12m: 7.2663,
    vol_12m: 0.5793,
    close_raw: 284.03,
    marketcap_cr: 892.1,
    ...overrides,
  };
}

function run(overrides: Partial<ScreenRunResponse> = {}): ScreenRunResponse {
  const rows = overrides.rows ?? [row()];
  return {
    as_of: "2026-08-18",
    columns: [
      "symbol",
      "name",
      "sorting_factor",
      "close_raw",
      "ret_12m",
      "vol_12m",
      "marketcap_cr",
    ],
    data_version: 41,
    result_count: rows.length,
    sorting_factor: { key: "sharpe_12m", label: "Sharpe 12M" },
    rows,
    ...overrides,
  };
}

function mockScrollMetrics() {
  if (typeof window.ResizeObserver === "undefined") {
    Object.defineProperty(window, "ResizeObserver", {
      writable: true,
      configurable: true,
      value: class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    });
  }

  for (const prop of ["clientHeight", "offsetHeight"] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, {
      configurable: true,
      get() {
        return 560;
      },
    });
  }
  for (const prop of ["clientWidth", "offsetWidth"] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, {
      configurable: true,
      get() {
        return 800;
      },
    });
  }
}

function renderPanel(
  result: ScreenRunResponse | undefined,
  extra: Partial<{
    isPending: boolean;
    isFetching: boolean;
    onLoosenFilters: (() => void) | undefined;
    onUndoLastFilter: (() => void) | undefined;
  }> = {},
) {
  return render(
    <TooltipProvider>
      <ResultsPanel
        result={result}
        columnMeta={META}
        sortingFactorUnit="ratio"
        isPending={extra.isPending ?? false}
        isFetching={extra.isFetching ?? false}
        error={undefined}
        onRetry={vi.fn()}
        onLoosenFilters={extra.onLoosenFilters === undefined ? vi.fn() : extra.onLoosenFilters}
        onUndoLastFilter={extra.onUndoLastFilter}
      />
    </TooltipProvider>,
  );
}

function priceHeader() {
  return screen.queryByRole("columnheader", { name: /price/i });
}

/**
 * `DataTable` is loaded with `next/dynamic` so @tanstack/react-table stays out of the route's
 * first-load bundle (docs/11's "after code-splitting the table"). In jsdom that means the grid is
 * absent for a tick after render.
 *
 * **Every assertion about a header has to await this first, including the negative ones.** Without
 * it `expect(priceHeader()).not.toBeInTheDocument()` passes because the whole table is missing,
 * which is a test that can no longer fail — it would go green if the Price column were dropped, and
 * equally green if the table never rendered at all. `symbol` is the anchor because the column
 * policy never drops it (leaf-7.1.2 G3).
 */
/**
 * `DataTable` is loaded with `next/dynamic` so @tanstack/react-table stays out of the route's
 * first-load bundle — docs/11's "after code-splitting the table", which took `/build/[id]` from
 * 263.1 KB to 238.7 KB against a 250 KB budget. In jsdom the grid therefore arrives a tick after
 * render (`src/test/setup.ts` resolves the split module through `React.lazy`).
 *
 * **Every assertion about a header awaits this first, the negative ones especially.** Without it
 * `expect(priceHeader()).not.toBeInTheDocument()` is green when the Price column is correctly
 * dropped and equally green when no table rendered at all — a test that cannot fail. `findByRole`
 * throws when the grid never appears, which is what keeps those three assertions honest.
 */
async function tableReady() {
  await screen.findByRole("grid");
}

beforeEach(() => {
  mockScrollMetrics();
});

afterEach(() => {
  cleanup();
});

describe("dead columns", () => {
  it("drops the Price header when every close_raw is null, and discloses it", async () => {
    renderPanel(
      run({
        result_count: 2,
        rows: [
          row({ close_raw: null }),
          row({ rank: 2, symbol: "INFY", name: "Infosys", close_raw: null }),
        ],
      }),
    );

    await tableReady();
    expect(priceHeader()).not.toBeInTheDocument();
    const note = screen.getByTestId("suppressed-columns");
    expect(note).toHaveTextContent("Price");
    expect(note).toHaveTextContent("hidden");
    expect(note).toHaveTextContent("every row came back empty");
  });

  it("keeps the Price header when close_raw is populated", async () => {
    renderPanel(run());
    await tableReady();

    expect(priceHeader()).toBeInTheDocument();
    expect(screen.queryByTestId("suppressed-columns")).not.toBeInTheDocument();
  });

  it("names every empty requested column, not the ones the diet already dropped", async () => {
    renderPanel(
      run({
        columns: [
          "symbol",
          "name",
          "sorting_factor",
          "close_raw",
          "ret_12m",
          "vol_12m",
          "marketcap_cr",
          "rsi_1y",
        ],
        rows: [row({ close_raw: null, rsi_1y: null })],
        result_count: 1,
      }),
    );

    const note = screen.getByTestId("suppressed-columns");
    expect(note).toHaveTextContent("Price");
    expect(note).toHaveTextContent("rsi_1y");
    expect(note).not.toHaveTextContent("Market cap");
    await tableReady();
    expect(priceHeader()).not.toBeInTheDocument();
  });
});

describe("empty state", () => {
  it("shows undo-last-filter when result_count is 0", () => {
    renderPanel(run({ rows: [], result_count: 0 }));

    expect(screen.getByText("Nothing survived your filters")).toBeInTheDocument();
    expect(screen.getByText(/Ruthless/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset filters to defaults" })).toBeInTheDocument();
    expect(screen.getByTestId("undo-last-filter")).toHaveTextContent("Undo last filter");
  });

  it("still renders undo-last-filter when onUndoLastFilter is omitted, disabled", () => {
    renderPanel(run({ rows: [], result_count: 0 }), {
      onUndoLastFilter: undefined,
      onLoosenFilters: vi.fn(),
    });

    expect(screen.getByTestId("undo-last-filter")).toBeDisabled();
  });

  it("calls onUndoLastFilter, not reset, when undo is clicked", async () => {
    const undo = vi.fn();
    const reset = vi.fn();
    const user = userEvent.setup();
    renderPanel(run({ rows: [], result_count: 0 }), {
      onUndoLastFilter: undo,
      onLoosenFilters: reset,
    });

    await user.click(screen.getByTestId("undo-last-filter"));
    expect(undo).toHaveBeenCalledOnce();
    expect(reset).not.toHaveBeenCalled();
  });
});

describe("sort note", () => {
  it("is present on the table and not duplicated as a paragraph", async () => {
    renderPanel(run());
    await tableReady();

    const note = screen.getByTestId("sort-note");
    expect(note).toHaveAttribute("aria-label", SORT_NOTE);
    expect(note).not.toHaveAttribute("title");
    // The sentence lives on the ⓘ, not as a second copy under the grid.
    expect(screen.queryByText(SORT_NOTE)).not.toBeInTheDocument();
  });
});

describe("kept chrome", () => {
  it("keeps result-count, as-of, and the sr-only sorting-factor hook", () => {
    renderPanel(run());

    expect(screen.getByTestId("result-count")).toHaveTextContent("1 matches");
    expect(screen.getByTestId("as-of")).toHaveTextContent("fresh as of");
    expect(screen.getByTestId("sorting-factor")).toHaveTextContent("Ranked by Sharpe 12M");
  });
});
