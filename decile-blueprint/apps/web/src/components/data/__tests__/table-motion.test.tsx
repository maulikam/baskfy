import type { ColumnDef } from "@tanstack/react-table";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DataTable,
  MAX_MOTION_MS,
  ROW_STAGGER_LIMIT,
  ROW_STAGGER_MS,
  SORT_FLIP_MS,
  rowStaggerDelayMs,
} from "@/components/data/data-table";
import { TooltipProvider } from "@/components/ui/tooltip";

type Row = { symbol: string; name: string };

const COLUMNS: Array<ColumnDef<Row, unknown>> = [
  { accessorKey: "symbol", header: "Symbol" },
  { accessorKey: "name", header: "Name" },
];

function makeRows(count: number): Row[] {
  return Array.from({ length: count }, (_, index) => ({
    symbol: `SYM${String(index + 1).padStart(2, "0")}`,
    name: `Company ${index + 1}`,
  }));
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

function renderTable(
  props: Partial<{
    data: Row[];
    contentKey: string;
  }> = {},
) {
  const data = props.data ?? makeRows(20);
  return render(
    <TooltipProvider>
      <DataTable
        data={data}
        columns={COLUMNS}
        label="Screen results"
        repeatHeaderEvery={0}
        height={560}
        contentKey={props.contentKey}
      />
    </TooltipProvider>,
  );
}

beforeEach(() => {
  mockScrollMetrics();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("rowStaggerDelayMs", () => {
  it("stagger caps at ROW_STAGGER_LIMIT so row 271 does not wait seconds", () => {
    expect(rowStaggerDelayMs(0)).toBe(0);
    expect(rowStaggerDelayMs(ROW_STAGGER_LIMIT - 1)).toBe(
      (ROW_STAGGER_LIMIT - 1) * ROW_STAGGER_MS,
    );
    expect(rowStaggerDelayMs(ROW_STAGGER_LIMIT)).toBeUndefined();
    expect(rowStaggerDelayMs(270)).toBeUndefined();

    const lastDelay = (ROW_STAGGER_LIMIT - 1) * ROW_STAGGER_MS;
    expect(lastDelay + ROW_STAGGER_MS).toBeLessThanOrEqual(MAX_MOTION_MS);
  });
});

describe("table load stagger", () => {
  it("stagger assigns increasing animation delays on the first rows", () => {
    renderTable({ data: makeRows(30) });

    const row0 = document.querySelector<HTMLElement>('[data-row-index="0"]');
    const row11 = document.querySelector<HTMLElement>('[data-row-index="11"]');
    const row12 = document.querySelector<HTMLElement>('[data-row-index="12"]');

    expect(row0?.style.animationDelay).toBe("0ms");
    expect(row0?.className).toContain("animate-in");
    expect(row11?.style.animationDelay).toBe(`${11 * ROW_STAGGER_MS}ms`);
    expect(row12?.style.animationDelay).toBe("");
    expect(row12?.className).not.toContain("animate-in");
  });

  it("remounts rows when contentKey changes so stagger runs again", () => {
    const { rerender } = render(
      <TooltipProvider>
        <DataTable
          data={makeRows(4)}
          columns={COLUMNS}
          label="Screen results"
          repeatHeaderEvery={0}
          height={560}
          contentKey="a"
        />
      </TooltipProvider>,
    );

    const first = document.querySelector('[data-row-index="0"]');
    rerender(
      <TooltipProvider>
        <DataTable
          data={makeRows(4)}
          columns={COLUMNS}
          label="Screen results"
          repeatHeaderEvery={0}
          height={560}
          contentKey="b"
        />
      </TooltipProvider>,
    );
    const second = document.querySelector('[data-row-index="0"]');
    expect(first).not.toBe(second);
  });
});

describe("sort FLIP", () => {
  it("flip enables transition-transform only for the sort window", async () => {
    vi.useFakeTimers();
    renderTable();

    const before = document.querySelector("[data-row-index]");
    expect(before?.className).not.toContain("transition-transform");

    fireEvent.click(screen.getByRole("button", { name: /symbol/i }));

    const during = document.querySelector("[data-row-index]");
    expect(during?.className).toContain("motion-safe:transition-transform");
    expect(during?.className).toContain("motion-safe:duration-200");

    await act(async () => {
      vi.advanceTimersByTime(SORT_FLIP_MS);
    });

    const after = document.querySelector("[data-row-index]");
    expect(after?.className).not.toContain("transition-transform");
  });

  it("does not animate longer than MAX_MOTION_MS", () => {
    expect(SORT_FLIP_MS).toBeLessThanOrEqual(MAX_MOTION_MS);
    expect((ROW_STAGGER_LIMIT - 1) * ROW_STAGGER_MS).toBeLessThanOrEqual(MAX_MOTION_MS);
  });
});

describe("prefers-reduced-motion", () => {
  it("stagger rows carry motion-reduce:animate-none", () => {
    renderTable();
    const row = document.querySelector<HTMLElement>('[data-row-index="0"]');
    expect(row?.className).toContain("motion-reduce:animate-none");
  });
});
