import { readFileSync } from "node:fs";
import { resolve } from "node:path";

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

const TABLE_SOURCE = readFileSync(
  resolve(process.cwd(), "src/components/data/data-table.tsx"),
  "utf8",
);

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

    /*
     * Read the duration off a row *past* the stagger cap, and pin it to the exported constant.
     *
     * Row 0 is the wrong place to ask. It carries the stagger's `motion-safe:duration-200` as
     * well as the FLIP's, `cn()` is tailwind-merge, and tailwind-merge resolves two utilities
     * from the same group by keeping the last one — which is the stagger's. The two are both
     * 200ms today, so nothing is visibly wrong and nothing here failed; raising the FLIP alone to
     * 700ms left this assertion green while the first twelve rows silently kept 200. Rows past
     * `ROW_STAGGER_LIMIT` have no stagger class and so carry the FLIP's duration unmerged.
     *
     * Worth knowing rather than worth refactoring: the collision is invisible while the two
     * numbers agree, and `G4` below scans every row so it is caught wherever it surfaces.
     */
    const unstaggered = document.querySelector(`[data-row-index="${ROW_STAGGER_LIMIT + 2}"]`);
    expect(unstaggered, "no row past the stagger cap was rendered").not.toBeNull();
    expect(unstaggered?.className).not.toContain("animate-in");
    const durations = new Set(
      [...(unstaggered?.className ?? "").matchAll(/(?:^|[\s:])duration-(\d+)\b/g)].map((m) =>
        Number(m[1]),
      ),
    );
    expect([...durations]).toEqual([SORT_FLIP_MS]);

    await act(async () => {
      vi.advanceTimersByTime(SORT_FLIP_MS);
      await Promise.resolve();
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

/**
 * G4 of `gates/leaf-7.3.2-motion.md`: "No animation exceeds 300ms, and only transform/opacity are
 * animated (the repo's motion budget)."
 *
 * A constant being ≤ 300 is not the claim. `SORT_FLIP_MS` is already asserted against
 * `MAX_MOTION_MS` above, and that assertion would stay green while the rendered row carried
 * `duration-700`, because the class name and the timer are two separate facts that only a comment
 * currently keeps in step. So the durations here are read off the **rendered row**, which is the
 * thing a reader actually waits for.
 *
 * The second half is the one with teeth, and it has never been asserted anywhere.
 * `transform` and `opacity` are the two properties a browser can animate on the compositor without
 * touching layout or paint. Everything else — `height`, `width`, `top`, `margin`, and the
 * catch-all `transition-all` that silently includes them — forces layout on every frame of the
 * animation, for every row on screen. On a 271-row virtualised grid that is the difference between
 * a sort that glides and one that stutters, and it is invisible in every screenshot and every unit
 * test that only checks a class is present.
 *
 * `transition-all` is banned outright rather than measured. It is the single most likely way this
 * budget gets broken — it is shorter to type than the property list, it is what an editor
 * autocompletes, and it animates whatever happens to change.
 */
describe("G4: the row layer's motion budget", () => {
  /** Tailwind's `duration-N` on any variant: `motion-safe:duration-200`, `duration-150`. */
  const DURATIONS = /(?:^|[\s:])duration-(\d+)\b/g;

  /** Properties that cost only the compositor. Anything else animates layout or paint. */
  const COMPOSITED = new Set(["transform", "opacity"]);

  function rowClassNames(): string[] {
    return Array.from(document.querySelectorAll<HTMLElement>("[data-row-index]")).map(
      (node) => node.className,
    );
  }

  /** Every distinct `duration-N` currently on a row, as numbers. */
  function renderedDurations(): number[] {
    const found = new Set<number>();
    for (const className of rowClassNames()) {
      for (const match of className.matchAll(DURATIONS)) found.add(Number(match[1]));
    }
    return [...found].sort((a, b) => a - b);
  }

  it("renders no duration above the budget, including the one only a sort turns on", () => {
    /*
     * The sort has to happen first, and that is the whole point of this case.
     *
     * `transition-transform motion-safe:duration-200` is applied only while `sortAnimating` is
     * true, so a test that renders and looks sees the *stagger's* duration and nothing else. That
     * near-miss is not hypothetical: it was written that way first, and raising the FLIP to
     * `duration-700` left it green — the stagger's own `duration-200` satisfied a `toContain`
     * looking for exactly that string. Reading every duration on the row, while the FLIP class is
     * live, is what closes it.
     */
    vi.useFakeTimers();
    renderTable({ data: makeRows(30) });
    fireEvent.click(screen.getByRole("button", { name: /symbol/i }));

    const flipped = rowClassNames().find((c) => c.includes("transition-transform"));
    expect(flipped, "no row carried the FLIP transition after a sort").toBeDefined();

    const durations = renderedDurations();
    expect(durations.length).toBeGreaterThan(0);
    for (const ms of durations) {
      expect(ms, `a row animates for ${ms}ms, past the ${MAX_MOTION_MS}ms budget`).toBeLessThanOrEqual(
        MAX_MOTION_MS,
      );
    }
  });

  it("the rendered duration agrees with the timer that ends the FLIP", () => {
    // The comment on SORT_FLIP_MS asks these to stay in lockstep; this is that, asserted — and
    // asserted as a *set*, so the stagger's duration cannot stand in for the FLIP's.
    vi.useFakeTimers();
    renderTable({ data: makeRows(30) });
    fireEvent.click(screen.getByRole("button", { name: /symbol/i }));

    expect(renderedDurations()).toContain(SORT_FLIP_MS);
    expect(renderedDurations()).toEqual([SORT_FLIP_MS]);
  });

  it("transitions only composited properties on a row", () => {
    renderTable({ data: makeRows(30) });
    fireEvent.click(screen.getByRole("button", { name: /symbol/i }));

    for (const className of rowClassNames()) {
      for (const token of className.split(/\s+/)) {
        const utility = token.replace(/^(?:[a-z-]+:)*/, "");
        if (!utility.startsWith("transition")) continue;

        // `transition-transform` → "transform"; `transition-[transform,opacity]` → both.
        const properties =
          utility === "transition"
            ? ["transform"] // Tailwind's bare `transition` is colours+opacity+transform+filter
            : utility
                .slice("transition-".length)
                .replace(/^\[|\]$/g, "")
                .split(",")
                .map((p) => p.trim());

        for (const property of properties) {
          expect(
            COMPOSITED.has(property),
            `row animates \`${property}\` via \`${token}\`; only transform and opacity are composited`,
          ).toBe(true);
        }
      }
    }
  });

  it("never uses transition-all anywhere in the table", () => {
    // Source-level, because `transition-all` on a branch this render did not take is still a
    // frame-rate bug waiting for the branch that does.
    expect(TABLE_SOURCE).not.toMatch(/\btransition-all\b/);
  });

  it("enters rows with a keyframe animation that touches only opacity and transform", () => {
    renderTable({ data: makeRows(30) });
    const first = rowClassNames()[0] ?? "";

    // `animate-in fade-in` is tailwindcss-animate's opacity+transform entrance. `slide-in-*`
    // is a transform. Anything else here would be a layout animation on every row on load.
    expect(first).toContain("animate-in");
    expect(first).toContain("fade-in");
    expect(first).not.toMatch(/\banimate-(?:bounce|ping|pulse|spin)\b/);
  });

  it("adds no keyframes that animate a layout property", () => {
    /*
     * This leaf's motion is Tailwind utilities and `getBoundingClientRect`; it was allowed no new
     * dependency and it added no `@keyframes`. `globals.css` does hold two that animate `height`
     * (`accordion-down`/`-up`, Radix's own) and the marketing flow's `stroke-dashoffset` — all
     * pre-existing, none reachable from the row layer, and none this gate's to change. What is
     * asserted is the boundary: no keyframe the table names animates a layout property.
     */
    const named = [...TABLE_SOURCE.matchAll(/\banimate-\[([a-z-]+)/g)].map((m) => m[1]);
    expect(named).toEqual([]);
  });
});
