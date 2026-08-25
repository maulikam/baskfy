import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PeekDrawer } from "@/components/screens/peek-drawer";
import type { ColumnMeta, ResultRow } from "@/components/screens/result-columns";
import { formatFraction } from "@/lib/format";

/**
 * Tree 1.3.2: the peek is a mini factsheet of the row already on screen — not a second
 * fetch of the instrument page, and not a 1-year chart (preview rows have no history series).
 */

afterEach(cleanup);

/** CUPID on the seeded 2026-08-18 screen: top rank, reference-export return and vol. */
const CUPID: ResultRow = {
  rank: 1,
  symbol: "CUPID",
  name: "Cupid Limited",
  sorting_factor: 5.13,
  ret_12m: 726.63,
  vol_12m: 0.5793,
  close_raw: 284.03,
};

const COLUMNS = ["symbol", "name", "sorting_factor", "ret_12m", "vol_12m", "close_raw"] as const;

const META = new Map<string, ColumnMeta>(
  (
    [
      ["sorting_factor", "ratio"],
      ["ret_12m", "percent"],
      ["vol_12m", "fraction"],
      ["close_raw", "price"],
    ] as const
  ).map(([key, unit]) => [key, { key, label: key, unit }]),
);

function draw(row: ResultRow | null, onClose = vi.fn()) {
  return {
    onClose,
    ...render(
      <PeekDrawer
        row={row}
        columns={COLUMNS}
        meta={META}
        sortingFactorLabel="Avg. Sharpe 12/6/3/1"
        onClose={onClose}
      />,
    ),
  };
}

function peek() {
  return screen.queryByTestId("peek-drawer");
}

function isShown(node: HTMLElement | null): boolean {
  return node !== null && node.getAttribute("data-state") !== "closed";
}

describe("PeekDrawer", () => {
  it("opens on a CUPID row as a mini factsheet: symbol, encodings, All numbers, factsheet link", () => {
    draw(CUPID);

    expect(screen.getByTestId("peek-drawer")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "CUPID" })).toBeInTheDocument();
    expect(screen.getByTestId("peek-all-numbers")).toHaveTextContent("All numbers");
    expect(screen.getByTestId("score-bar-fill")).toBeInTheDocument();

    const factsheet = screen.getByRole("link", { name: /Open the full factsheet/i });
    expect(factsheet).toHaveAttribute("href", "/instruments/CUPID");
  });

  it("is closed when there is no row", () => {
    draw(null);

    const drawer = peek();
    expect(isShown(drawer)).toBe(false);
  });

  it("slides in 200ms when motion is allowed", () => {
    draw(CUPID);

    expect(screen.getByTestId("peek-drawer").className).toMatch(/motion-safe:duration-200/);
  });

  it("puts the bumpiness percent on a native title, so the dots are not the only reading", () => {
    draw(CUPID);

    expect(screen.getAllByTitle(formatFraction(CUPID.vol_12m as number)).length).toBeGreaterThan(0);
  });

  it("does not render a 1-year chart — preview rows carry no history series", () => {
    draw(CUPID);

    expect(screen.queryByRole("img", { name: /chart|sparkline|1.?year/i })).not.toBeInTheDocument();
    expect(document.querySelector("canvas,svg[aria-label*='year' i]")).toBeNull();
  });

  it("closes through the existing Dialog: Escape calls onClose", async () => {
    const user = userEvent.setup();
    const { onClose } = draw(CUPID);

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
