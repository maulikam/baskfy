import type { ColumnDef } from "@tanstack/react-table";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { DataTable } from "@/components/data/data-table";
import { TooltipProvider } from "@/components/ui/tooltip";

type Row = { symbol: string; score: number };

const TECHNICAL = "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS";

const COLUMNS: Array<ColumnDef<Row, unknown>> = [
  {
    accessorKey: "symbol",
    header: "Stock",
    meta: { headerTooltip: TECHNICAL },
  },
  { accessorKey: "score", header: "Score" },
];

const ROWS: Row[] = [{ symbol: "CUPID", score: 5.13 }];

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

function renderTable() {
  return render(
    <TooltipProvider>
      <DataTable
        data={ROWS}
        columns={COLUMNS}
        label="Screen results"
        repeatHeaderEvery={0}
        height={560}
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

describe("column header tooltips", () => {
  it("does not put the technical name on a native title attribute", () => {
    renderTable();
    const headers = screen.getAllByRole("columnheader");
    for (const header of headers) {
      expect(header).not.toHaveAttribute("title", TECHNICAL);
    }
    expect(document.querySelectorAll('[title="AVERAGE SHARPE RETURN 12 6 3 1 MONTHS"]')).toHaveLength(
      0,
    );
  });

  it("exposes the technical name through a focusable trigger", async () => {
    renderTable();
    const trigger = screen.getByTestId("header-tooltip-trigger");
    expect(trigger).toHaveAttribute("aria-label", TECHNICAL);

    trigger.focus();
    expect(trigger).toHaveFocus();

    await userEvent.hover(trigger);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(TECHNICAL);
  });

  it("still sorts when the header label button is clicked", () => {
    renderTable();
    fireEvent.click(screen.getByRole("button", { name: /stock/i }));
    expect(screen.getByRole("columnheader", { name: /stock/i })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });
});
