import type { ColumnDef } from "@tanstack/react-table";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DataTable, ROW_HEIGHT } from "@/components/data/data-table";
import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * Screen-page contract for DataTable (PLAN.md leaf 1.1.1):
 *
 * - `repeatHeaderEvery={0}` is a single sticky header — no copy of the header every 16 rows.
 * - `sortNote` is one ⓘ on that header, not a second header row and not a paragraph.
 * - `rowHeight` overrides density; comfortable stays 34 when the prop is omitted.
 */

type Row = { symbol: string; name: string };

const COLUMNS: Array<ColumnDef<Row, unknown>> = [
  { accessorKey: "symbol", header: "Symbol" },
  { accessorKey: "name", header: "Name" },
];

const NOTE = "Sorting re-orders these rows in the browser. It does not re-run the screen.";

const ROWS: Row[] = Array.from({ length: 20 }, (_, index) => ({
  symbol: `SYM${String(index + 1).padStart(2, "0")}`,
  name: `Company ${index + 1}`,
}));

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

function renderTable(
  props: Partial<{
    rowHeight: number;
    sortNote: string;
    repeatHeaderEvery: number;
    density: "comfortable" | "compact";
  }> = {},
) {
  return render(
    <TooltipProvider>
      <DataTable
        data={ROWS}
        columns={COLUMNS}
        label="Screen results"
        repeatHeaderEvery={0}
        height={560}
        {...props}
      />
    </TooltipProvider>,
  );
}

beforeEach(() => {
  mockScrollMetrics();
});

afterEach(() => {
  cleanup();
});

describe("DataTable screen contract", () => {
  it("repeatHeaderEvery={0} keeps one columnheader set and zero repeat-header rows", () => {
    renderTable({ repeatHeaderEvery: 0 });

    const headers = screen.getAllByRole("columnheader");
    expect(headers).toHaveLength(2);
    expect(screen.getAllByText("Symbol")).toHaveLength(1);
    expect(screen.getAllByText("Name")).toHaveLength(1);
    expect(document.querySelectorAll("[data-repeat-header]")).toHaveLength(0);

    const dataRows = document.querySelectorAll("[data-row-index]");
    expect(dataRows.length).toBeGreaterThan(16);
  });

  it("repeat every 16 still paints an aria-hidden header copy, so 0 is a real disable", () => {
    renderTable({ repeatHeaderEvery: 16 });
    expect(document.querySelectorAll("[data-repeat-header]").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("columnheader")).toHaveLength(2);
  });

  it("renders sort-note as a focusable ⓘ on the sticky header when sortNote is passed", () => {
    renderTable({ sortNote: NOTE });

    const note = screen.getByTestId("sort-note");
    expect(note).toHaveAttribute("aria-label", NOTE);
    expect(note).not.toHaveAttribute("title");
    expect(note).toHaveTextContent("ⓘ");
    expect(note.closest('[role="row"]')).toBe(screen.getAllByRole("row")[0]);

    expect(screen.getAllByRole("columnheader")).toHaveLength(2);
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it("does not render sort-note when the prop is omitted", () => {
    renderTable();
    expect(screen.queryByTestId("sort-note")).not.toBeInTheDocument();
  });

  it("rowHeight={52} sets every data row's style height to 52", () => {
    renderTable({ rowHeight: 52 });

    const dataRows = document.querySelectorAll<HTMLElement>("[data-row-index]");
    expect(dataRows.length).toBeGreaterThan(0);
    for (const row of dataRows) {
      expect(row.style.height).toBe("52px");
    }
  });

  it("omitting rowHeight keeps the comfortable default of 34", () => {
    expect(ROW_HEIGHT.comfortable).toBe(34);
    renderTable({ density: "comfortable" });

    const dataRows = document.querySelectorAll<HTMLElement>("[data-row-index]");
    expect(dataRows.length).toBeGreaterThan(0);
    for (const row of dataRows) {
      expect(row.style.height).toBe("34px");
    }
  });

  it("FLIP-animates data rows only for the sort window, never as a standing scroll class", async () => {
    vi.useFakeTimers();
    renderTable();

    const before = document.querySelector("[data-row-index]");
    expect(before?.className).not.toContain("transition-transform");

    fireEvent.click(screen.getByRole("button", { name: /symbol/i }));

    const during = document.querySelector("[data-row-index]");
    expect(during?.className).toContain("motion-safe:transition-transform");
    expect(during?.className).toContain("motion-safe:duration-200");
    expect(during?.className).not.toMatch(/duration-(300|500|700|1000)/);

    await act(async () => {
      vi.advanceTimersByTime(200);
      await Promise.resolve();
    });

    const after = document.querySelector("[data-row-index]");
    expect(after?.className).not.toContain("transition-transform");
    vi.useRealTimers();
  });
});
