import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CARD_GAP_REM,
  CARD_HEIGHT_REM,
  CARD_OVERSCAN,
  ResultCards,
} from "@/components/screens/result-cards";
import type { ResultRow } from "@/components/screens/result-columns";

/**
 * `gates/leaf-7.4.1-mobile-perf.md` — the mobile card feed is not 271 nodes.
 *
 * The measured defect: at 390px the desktop grid is `display: none` and the card list rendered all
 * 271 rows into the DOM, while the grid it replaced held ~26 nodes for the same result. The one
 * surface a low-powered device actually renders was the only one paying full price for the row set.
 *
 * What this file asserts is the *spec*, not the implementation (house rule 2). It never reaches for
 * `@tanstack/react-virtual`, a window size or a particular index range — swapping the virtualiser
 * for another one, or retuning the overscan, must not turn any of these red. The four claims are:
 * the DOM stays bounded as the row set grows; the rows that are absent are still *announced* and
 * still *reachable* by scrolling; and a card that is on screen is a whole card.
 *
 * Two of those are the ones that make virtualisation dangerous rather than merely clever. A feed
 * that renders 13 of 271 rows and lets the reader scroll to none of the rest is not faster, it is
 * broken; and one that drops `aria-setsize` has told a screen-reader user the list is 13 items
 * long. Both are silent in a screenshot, which is why they are tests.
 */

/** The seeded universe's size — the number in the defect report, and the point of the exercise. */
const SEEDED_ROWS = 271;

/** jsdom's default viewport height. The virtualiser reads it to decide what is on screen. */
const VIEWPORT = 768;

/** What one card occupies, in px at the default root size: the box plus the gap beneath it. */
const STRIDE = (CARD_HEIGHT_REM + CARD_GAP_REM) * 16;

function rows(count: number): ResultRow[] {
  return Array.from({ length: count }, (_, index) => ({
    rank: index + 1,
    symbol: `SYM${String(index).padStart(3, "0")}`,
    name: `Holding Number ${index + 1} Limited`,
    // Descending, so the first card is the strongest — the same shape as a ranked screen.
    sorting_factor: Number((5.13 - index * 0.017).toFixed(4)),
    ret_12m: index % 3 === 0 ? -12.34 : 45.67,
    vol_12m: 0.179 + (index % 5) * 0.11,
    close_raw: 284.03 + index,
  }));
}

/**
 * jsdom lays nothing out: every `getBoundingClientRect` is zero and there is no scrolling. A
 * window virtualiser asked "what is on screen" under those conditions answers "nothing", and every
 * assertion below would pass against an empty list — the vacuous green this shim exists to prevent.
 *
 * So the two inputs it actually reads are supplied: the viewport height, and a document scroll
 * position that `scrollTo` moves for real. `stubScroll` returns the setter rather than exposing a
 * global, so a test that scrolls has to say so.
 */
function stubViewport() {
  Object.defineProperty(window, "innerHeight", {
    configurable: true,
    writable: true,
    value: VIEWPORT,
  });
  /* jsdom has no layout, so `scrollTo` is unimplemented and logs to stderr every time the
     virtualiser reaches for it. The position is driven directly by `scrollWindowTo` below, so the
     real method has nothing to do here; stubbing it keeps a green run quiet enough that a genuine
     warning in it would be noticed. */
  Object.defineProperty(window, "scrollTo", {
    configurable: true,
    writable: true,
    value: () => {},
  });
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
}

function scrollWindowTo(y: number) {
  Object.defineProperty(window, "scrollY", { configurable: true, writable: true, value: y });
  Object.defineProperty(window, "pageYOffset", { configurable: true, writable: true, value: y });
  act(() => {
    window.dispatchEvent(new Event("scroll"));
  });
}

/**
 * The scroll position is a property of the window, and the window outlives a test.
 *
 * Leaving it dirty is not a tidiness point: a test that scrolls to the same offset the previous
 * test left behind dispatches an event that changes nothing, the virtualiser correctly does
 * nothing, and the test reads the top of the list while believing it is 120 rows down. That was
 * observed here before this reset existed — one test passed and its neighbour failed purely on
 * the order they ran in.
 */
function resetScroll() {
  scrollWindowTo(0);
}

function renderedIndices(): number[] {
  return screen
    .getAllByTestId("result-card")
    .map((node) => Number(node.getAttribute("data-index")))
    .sort((a, b) => a - b);
}

beforeEach(() => {
  stubViewport();
  resetScroll();
});
afterEach(cleanup);

describe("G1: the feed is bounded", () => {
  it("renders far fewer nodes than rows for a 271-row result", () => {
    render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    const rendered = screen.getAllByTestId("result-card");

    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.length).toBeLessThan(SEEDED_ROWS);

    /* The real bound, stated as the spec rather than as today's number: what fits in the viewport,
       plus the overscan above and below, plus one partial card at each edge. A regression that
       un-virtualises the list fails here by two orders of magnitude. */
    const bound = Math.ceil(VIEWPORT / STRIDE) + CARD_OVERSCAN * 2 + 2;
    expect(rendered.length).toBeLessThanOrEqual(bound);
  });

  it("does not grow when the row set grows tenfold", () => {
    // The property that distinguishes a virtualised list from a short one: cost is independent of
    // the row count. A list that renders all rows passes the assertion above for a small result.
    const { unmount } = render(<ResultCards rows={rows(40)} onActivate={() => {}} />);
    const small = screen.getAllByTestId("result-card").length;
    unmount();

    render(<ResultCards rows={rows(400)} onActivate={() => {}} />);
    expect(screen.getAllByTestId("result-card").length).toBe(small);
  });

  it("is the list itself, so `ul > li` still holds", () => {
    // The usual virtualisation shape puts a spacer div between the ul and its li, which costs the
    // list its semantics and every `ul > li` selector — including the one the defect was measured
    // with.
    const { container } = render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    const list = screen.getByTestId("result-cards");
    expect(list.tagName).toBe("UL");
    expect(container.querySelectorAll("ul[aria-label] > li").length).toBe(
      screen.getAllByTestId("result-card").length,
    );
  });

  it("still announces the true total to assistive tech", () => {
    // Without this, a screen reader is told the list is thirteen items long.
    render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    for (const card of screen.getAllByTestId("result-card")) {
      expect(card.getAttribute("aria-setsize")).toBe(String(SEEDED_ROWS));
    }
    expect(screen.getAllByTestId("result-card")[0]?.getAttribute("aria-posinset")).toBe("1");
  });
});

describe("G2: scrolling reaches the last row, and a card still opens the drawer", () => {
  it("reserves the height of every row, not just the rendered ones", () => {
    render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    const total = Number.parseFloat(screen.getByTestId("result-cards").style.height);
    // A list that only reserves the rendered rows' height has no distance left to scroll, which is
    // how a virtualised feed silently loses everything past the first screen.
    expect(total).toBeGreaterThanOrEqual(SEEDED_ROWS * STRIDE * 0.99);
  });

  it("renders the rows around the scroll position, and the last row at the bottom", () => {
    render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    expect(renderedIndices()).toContain(0);

    scrollWindowTo(STRIDE * 120);
    const middle = renderedIndices();
    expect(middle).toContain(120);
    expect(middle).not.toContain(0);

    scrollWindowTo(SEEDED_ROWS * STRIDE - VIEWPORT);
    // The row the defect report cares about: the 271st, which an un-reachable feed never shows.
    expect(renderedIndices()).toContain(SEEDED_ROWS - 1);
  });

  it("hands the tapped row to onActivate", async () => {
    const onActivate = vi.fn();
    const data = rows(SEEDED_ROWS);
    render(<ResultCards rows={data} onActivate={onActivate} />);

    await userEvent.click(screen.getByRole("button", { name: /SYM000/ }));
    expect(onActivate).toHaveBeenCalledTimes(1);
    expect(onActivate.mock.calls[0]?.[0]).toBe(data[0]);
  });

  it("hands over a row from deep in the list, not just a remembered first one", async () => {
    const onActivate = vi.fn();
    const data = rows(SEEDED_ROWS);
    render(<ResultCards rows={data} onActivate={onActivate} />);

    scrollWindowTo(STRIDE * 120);
    await userEvent.click(screen.getByRole("button", { name: /SYM120/ }));
    expect(onActivate.mock.calls[0]?.[0]).toBe(data[120]);
  });
});

describe("G3: a card that is on screen is a whole card", () => {
  it("keeps its rank badge, symbol, name, score bar and return chip", () => {
    render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    const first = screen.getAllByTestId("result-card")[0]!;

    // §3.5's four encodings, each looked up inside the card rather than on the page, so a stray
    // match elsewhere cannot satisfy this.
    expect(first.querySelector("[data-testid='score-bar-fill']")).not.toBeNull();
    expect(first.textContent).toContain("SYM000");
    expect(first.textContent).toContain("Holding Number 1 Limited");
    expect(first.textContent).toContain("-12.34%");
    expect(first.querySelector(".sr-only")?.textContent).toMatch(/bumpiness \d of 5/);
  });

  it("keeps the badge shape that marks the top three", () => {
    render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    const cards = screen.getAllByTestId("result-card");
    // Rank 1 is a filled pill; rank 4 is a plain figure. Losing the distinction is exactly the
    // sort of thing that survives a screenshot.
    expect(cards[0]?.querySelector(".rounded-full.size-6, .size-6.rounded-full")).not.toBeNull();
    expect(cards[3]?.textContent).toContain("4");
  });

  it("scales every bar against the row set, so two strong scores differ", () => {
    /*
     * The defect this caught, 12 Sep 2026: the feed rendered `<ScoreBar value={score} />` with no
     * `scale`. Without one a score above the reference becomes its own ceiling, so the seeded
     * universe's top names — 5.13 down to about 3.0, the rows a reader is actually comparing —
     * every one of them painted a full track on a phone while the same rows encoded correctly in
     * the table. `leaf-7.1.1` G1 did not catch it because it passes a scale explicitly and so
     * never drove the path the card feed used.
     */
    render(<ResultCards rows={rows(SEEDED_ROWS)} onActivate={() => {}} />);
    const fills = screen
      .getAllByTestId("result-card")
      .map(
        (card) =>
          card.querySelector<HTMLElement>("[data-testid='score-bar-fill']")?.style.transform,
      );
    expect(fills[0]).toBe("scaleX(1)");
    expect(fills[1]).toBeDefined();
    expect(fills[1]).not.toBe(fills[0]);
    // Monotonic: a ranked feed's bars must shorten down the list, not merely differ.
    const widths = fills.map((t) => Number.parseFloat(String(t).replace(/[^0-9.]/g, "")));
    expect(widths.every((w, i) => i === 0 || w <= widths[i - 1]!)).toBe(true);
  });

  it("renders a card whose optional readings are missing without dropping the row", () => {
    // A screen can return rows with no price and no volatility; the card must still be a card.
    const sparse: ResultRow[] = [
      { rank: 1, symbol: "SPARSE", name: "Sparse Limited", sorting_factor: 1.2 },
    ];
    render(<ResultCards rows={sparse} onActivate={() => {}} />);
    const card = screen.getByTestId("result-card");
    expect(card.textContent).toContain("SPARSE");
    expect(card.querySelector("[data-testid='score-bar-fill']")).not.toBeNull();
  });
});
