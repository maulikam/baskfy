import type { ColumnDef } from "@tanstack/react-table";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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

/**
 * G5 of `gates/leaf-7.2.2-tooltips.md`: "The §2.3 note about browser-side sorting not re-running
 * the screen is still reachable."
 *
 * This gate exists because of what the rest of the leaf did. The header's technical name moved off
 * a native `title=` and onto the repo's `Tooltip` primitive, and the sort note in this same file is
 * built the same way — one `ⓘ` that reveals a sentence. A change to how one of them is revealed is
 * a change to how both are, and the note is the one carrying a *correctness* warning rather than a
 * definition: the ranks a reader sees after clicking a column header are still the server's, and
 * the sort has not re-run the screen. Somebody who never learns that reads a re-ordered table as a
 * re-computed one.
 *
 * "Reachable" is therefore asserted as three separate properties, because the note can be lost in
 * three different ways and only the first is visible on screen:
 *
 * · it is *rendered* at all, whenever the table is given one;
 * · it is reachable without a pointer — a hover-only affordance is unreachable to a keyboard user
 *   and to every touch device, which is the exact defect this leaf was opened to fix in the header;
 * · reaching it does not *do something else*, in particular does not sort the column it sits in.
 *   The note is positioned inside the header row, so a click that bubbles would re-order the table
 *   as the price of reading a warning about re-ordering the table.
 */
describe("G5: the browser-side sorting note stays reachable", () => {
  const NOTE =
    "Sorting a column re-orders these rows in the browser. It does not re-run the screen, so the ranks stay as the server computed them.";

  function renderWithNote() {
    return render(
      <TooltipProvider>
        <DataTable
          data={ROWS}
          columns={COLUMNS}
          label="Screen results"
          repeatHeaderEvery={0}
          height={560}
          sortNote={NOTE}
        />
      </TooltipProvider>,
    );
  }

  it("is rendered, and carries the sentence as its accessible name", () => {
    renderWithNote();
    expect(screen.getByTestId("sort-note")).toHaveAttribute("aria-label", NOTE);
  });

  it("is reachable by keyboard, not only by hover", async () => {
    renderWithNote();
    const note = screen.getByTestId("sort-note");

    // A `div` with a hover handler would pass an aria-label assertion and fail this one.
    expect(note.tagName).toBe("BUTTON");
    // Focus opens the tooltip, which is a state update; React wants it inside act().
    act(() => {
      note.focus();
    });
    expect(note).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent(NOTE);
  });

  it("does not carry a native title, which is the affordance this leaf removed", () => {
    renderWithNote();
    expect(screen.getByTestId("sort-note")).not.toHaveAttribute("title");
    expect(document.querySelectorAll(`[title="${NOTE}"]`)).toHaveLength(0);
  });

  it("reading it does not sort the column it sits in", async () => {
    renderWithNote();
    const before = screen
      .getByRole("columnheader", { name: /stock/i })
      .getAttribute("aria-sort");

    await userEvent.click(screen.getByTestId("sort-note"));

    expect(
      screen.getByRole("columnheader", { name: /stock/i }).getAttribute("aria-sort"),
    ).toBe(before);
  });

  it("is absent when the table is given no note, rather than rendering an empty hint", () => {
    renderTable();
    expect(screen.queryByTestId("sort-note")).toBeNull();
  });
});
