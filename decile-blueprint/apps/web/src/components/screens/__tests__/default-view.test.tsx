import type { ScreenRunResponse } from "@baskfy/api-client";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScreenBasketView } from "@/components/screens/screen-basket-view";
import { screenDefaultView } from "@/lib/screens/feature-flags";

/**
 * Tree 7 D8: `/build/[id]` lands on the ranked table, and the reversal is an env flip.
 *
 * What these assert is the brief's spec, not the code's shape: principle 1 ("the page must be
 * interesting before the user touches anything"), principle 2 ("the list is the hero") and the
 * click budget "see why the #1 stock is #1 — 0 clicks". `NEXT_PUBLIC_SCREEN_DEFAULT_VIEW=basket`
 * must restore Tree 6 §5's page *exactly*, which is why the basket case checks the sizing controls
 * and the save action rather than only the absence of the table.
 */

const FLAG = "NEXT_PUBLIC_SCREEN_DEFAULT_VIEW";

/** Stands in for the DataTable the editor passes down; identity is all these tests need. */
const TABLE = <div data-testid="ranked-table">271 ranked rows</div>;

function runResponse(rowCount: number): ScreenRunResponse {
  return {
    as_of: "2026-08-18",
    columns: ["symbol", "name", "sorting_factor", "close_raw"],
    data_version: 41,
    result_count: rowCount,
    sorting_factor: { key: "sharpe_12m", label: "Sharpe 12M" },
    rows: Array.from({ length: rowCount }, (_, i) => ({
      rank: i + 1,
      symbol: `SYM${i + 1}`,
      name: `Company ${i + 1}`,
      sorting_factor: 5.13 - i * 0.01,
      close_raw: 1_000 + i,
    })),
  };
}

function renderView(props: { topN?: number; rows?: number; table?: React.ReactNode } = {}) {
  return render(
    <ScreenBasketView
      result={runResponse(props.rows ?? 40)}
      screenName="Momentum — high Sharpe"
      screenPublicId="exmpl0000001"
      topN={props.topN}
      table={props.table ?? TABLE}
    />,
  );
}

/** The sizing form is the whole of what basket-first put in front of a first-time visitor. */
function sizingForm() {
  return screen.queryByText("Amount to invest");
}

function pressed(id: "basket" | "table"): string | null {
  return screen.getByTestId(`view-mode-${id}`).getAttribute("aria-pressed");
}

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

describe("screenDefaultView", () => {
  it("is table when nothing is set", () => {
    vi.stubEnv(FLAG, undefined);
    expect(screenDefaultView()).toBe("table");
  });

  it("is basket for the exact value `basket`", () => {
    vi.stubEnv(FLAG, "basket");
    expect(screenDefaultView()).toBe("basket");
  });

  it.each(["Basket", "BASKET", "baskets", " basket ", "table", "1", "0", "true", ""])(
    "reads %o as table, so a typo cannot decide what the page opens on",
    (value) => {
      vi.stubEnv(FLAG, value);
      expect(screenDefaultView()).toBe("table");
    },
  );
});

describe("the screen editor's first paint", () => {
  it("renders the ranked table, with no override set", () => {
    vi.stubEnv(FLAG, undefined);
    renderView();

    expect(screen.getByTestId("ranked-table")).toBeInTheDocument();
    expect(sizingForm()).not.toBeInTheDocument();
    expect(pressed("table")).toBe("true");
    expect(pressed("basket")).toBe("false");
  });

  it("renders the ranked table when the flag is garbage", () => {
    vi.stubEnv(FLAG, "Basket");
    renderView();

    expect(screen.getByTestId("ranked-table")).toBeInTheDocument();
    expect(sizingForm()).not.toBeInTheDocument();
    expect(pressed("table")).toBe("true");
  });

  it("reaches the top-ranked stock's own row in zero clicks", () => {
    vi.stubEnv(FLAG, undefined);
    renderView({ rows: 3, table: <div data-testid="ranked-table">SYM1 · 5.13</div> });

    expect(screen.getByTestId("ranked-table")).toHaveTextContent("SYM1 · 5.13");
  });

  it("renders the table on a zero-row result rather than an empty basket", () => {
    vi.stubEnv(FLAG, undefined);
    renderView({ rows: 0, table: <div data-testid="ranked-table">no rows</div> });

    expect(screen.getByTestId("ranked-table")).toBeInTheDocument();
    expect(sizingForm()).not.toBeInTheDocument();
  });

  it("is read-only-safe: the save action a read-only example cannot use is not what it lands on", () => {
    vi.stubEnv(FLAG, undefined);
    renderView();

    expect(screen.queryByTestId("save-basket")).not.toBeInTheDocument();
  });
});

describe("NEXT_PUBLIC_SCREEN_DEFAULT_VIEW=basket restores Tree 6 §5", () => {
  it("lands on the basket, with its sizing controls and its save action", () => {
    vi.stubEnv(FLAG, "basket");
    renderView();

    expect(pressed("basket")).toBe("true");
    expect(pressed("table")).toBe("false");
    expect(screen.queryByTestId("ranked-table")).not.toBeInTheDocument();

    expect(sizingForm()).toBeInTheDocument();
    expect(screen.getByText("Number of stocks")).toBeInTheDocument();
    expect(screen.getByText("How spread out?")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Holding profile" })).toBeInTheDocument();
    expect(screen.getByTestId("save-basket")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Momentum — high Sharpe" })).toBeInTheDocument();
    expect(
      screen.getByText("Saving records the basket. Nothing here buys or sells anything."),
    ).toBeInTheDocument();
  });

  it("sizes the basket to the profile's suggestion, exactly as it did before", () => {
    vi.stubEnv(FLAG, "basket");
    renderView();

    /* BALANCED suggests 20 names; +1 for the header row. */
    expect(screen.getAllByRole("row")).toHaveLength(21);
  });

  it("changes nothing but which view is pressed", () => {
    vi.stubEnv(FLAG, "basket");
    renderView();
    const basketFirst = toggleShape();
    cleanup();

    vi.stubEnv(FLAG, "table");
    renderView();

    expect(toggleShape()).toEqual({ ...basketFirst, pressed: "table" });
    expect(basketFirst.pressed).toBe("basket");
  });
});

/** Everything about the toggle except which button is pressed must be flag-independent. */
function toggleShape(): { labels: string[]; testIds: string[]; pressed: string | undefined } {
  const buttons = screen.getAllByRole("button", { name: /^(Basket|Table)$/ });
  return {
    labels: buttons.map((b) => b.textContent ?? ""),
    testIds: buttons.map((b) => b.getAttribute("data-testid") ?? ""),
    pressed: buttons
      .find((b) => b.getAttribute("aria-pressed") === "true")
      ?.getAttribute("data-testid")
      ?.replace("view-mode-", ""),
  };
}

describe("the toggle", () => {
  it("keeps its group semantics and both Playwright hooks", () => {
    vi.stubEnv(FLAG, undefined);
    renderView();

    expect(screen.getByRole("group", { name: "Result view" })).toBeInTheDocument();
    expect(screen.getByTestId("view-mode-basket")).toHaveTextContent("Basket");
    expect(screen.getByTestId("view-mode-table")).toHaveTextContent("Table");
  });

  it("switches to the basket in one tap, and back again", async () => {
    vi.stubEnv(FLAG, undefined);
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByTestId("view-mode-basket"));
    expect(sizingForm()).toBeInTheDocument();
    expect(screen.queryByTestId("ranked-table")).not.toBeInTheDocument();
    expect(pressed("basket")).toBe("true");

    await user.click(screen.getByTestId("view-mode-table"));
    expect(screen.getByTestId("ranked-table")).toBeInTheDocument();
    expect(sizingForm()).not.toBeInTheDocument();
    expect(pressed("table")).toBe("true");
  });

  it("keeps a chosen view across a re-render: the flag is a default, not state", () => {
    vi.stubEnv(FLAG, undefined);
    const { rerender } = renderView();

    fireEvent.click(screen.getByTestId("view-mode-basket"));
    rerender(
      <ScreenBasketView
        result={runResponse(41)}
        screenName="Momentum — high Sharpe"
        screenPublicId="exmpl0000001"
        table={TABLE}
      />,
    );

    expect(pressed("basket")).toBe("true");
  });
});

describe("compact card previews", () => {
  it.each([undefined, "basket", "table", "nonsense"])(
    "show the same basket with no sizing controls, with the flag %o",
    (value) => {
      vi.stubEnv(FLAG, value);
      renderView({ topN: 5 });

      expect(pressed("basket")).toBe("true");
      expect(screen.queryByTestId("ranked-table")).not.toBeInTheDocument();
      expect(sizingForm()).not.toBeInTheDocument();
      expect(screen.queryByTestId("save-basket")).not.toBeInTheDocument();
      /* The pinned count, not the profile's suggestion: 5 names + the header row. */
      expect(screen.getAllByRole("row")).toHaveLength(6);
    },
  );
});

/**
 * G3 of `gates/leaf-7.3.1-default-view.md`: "Basket remains reachable in one tap, keeps its sizing
 * controls and its 'Save as basket' action, and the toggle keeps `aria-pressed`."
 *
 * The cases above each cover a piece of this, and the pieces are the problem. T7-D8 moved what the
 * page *opens* on; the risk it carries is that the view it moved away from quietly decays, because
 * nothing lands there any more and nothing fails when part of it stops arriving. A basket view that
 * still renders but has lost its save action is not a demotion, it is a removal, and it would not
 * show up in a test that only asks whether the sizing form appeared.
 *
 * So this asserts the whole claim as one journey, from the default the flag now produces: start on
 * the table, press Basket once, and find everything Tree 6 §5 put there. One tap means one — the
 * count is asserted rather than described, because "one tap away" degrades to two the moment
 * something is nested behind a disclosure, and a reader would still call that reachable.
 *
 * `aria-pressed` is in the same gate for a reason that is easy to lose: the toggle is two buttons,
 * not a tab list, so the pressed state is the *only* thing telling a screen-reader user which of
 * the two views they are looking at. Losing it leaves the page announcing two identical buttons.
 */
describe("G3: the basket stays one tap away, whole", () => {
  it("is one tap from the default view, with its sizing controls and its save action", async () => {
    vi.stubEnv(FLAG, undefined);
    const user = userEvent.setup();
    renderView();

    // The premise: we start where T7-D8 put us, not on the basket already.
    expect(screen.getByTestId("ranked-table")).toBeInTheDocument();
    expect(pressed("basket")).toBe("false");

    await user.click(screen.getByTestId("view-mode-basket"));

    // Everything Tree 6 §5 put on this view, in one place.
    expect(sizingForm()).toBeInTheDocument();
    expect(screen.getByText("Number of stocks")).toBeInTheDocument();
    expect(screen.getByText("How spread out?")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Holding profile" })).toBeInTheDocument();
    expect(screen.getByTestId("save-basket")).toHaveTextContent("Save as basket");
    expect(screen.getByRole("radiogroup", { name: "Cash allocation" })).toBeInTheDocument();
  });

  it("is exactly one tap, not one tap and a disclosure", async () => {
    vi.stubEnv(FLAG, undefined);
    const user = userEvent.setup();
    renderView();

    const basket = screen.getByTestId("view-mode-basket");
    expect(basket).toBeVisible();
    expect(basket).not.toBeDisabled();
    // Nothing stands between the reader and the toggle: it is not inside a closed disclosure.
    expect(basket.closest("[hidden]")).toBeNull();
    expect(basket.closest("details:not([open])")).toBeNull();

    await user.click(basket);
    expect(sizingForm()).toBeInTheDocument();
  });

  it("keeps aria-pressed on both buttons, in both directions", async () => {
    vi.stubEnv(FLAG, undefined);
    const user = userEvent.setup();
    renderView();

    // Both carry the attribute at all times — a button that drops it when unpressed announces
    // nothing about its state rather than announcing "not pressed".
    expect(pressed("table")).toBe("true");
    expect(pressed("basket")).toBe("false");

    await user.click(screen.getByTestId("view-mode-basket"));
    expect(pressed("basket")).toBe("true");
    expect(pressed("table")).toBe("false");

    await user.click(screen.getByTestId("view-mode-table"));
    expect(pressed("table")).toBe("true");
    expect(pressed("basket")).toBe("false");
  });

  it("survives the flag being set to basket: the same view, still whole", () => {
    // The reversal must not be a different basket view from the one a tap reaches.
    vi.stubEnv(FLAG, "basket");
    renderView();

    expect(pressed("basket")).toBe("true");
    expect(screen.getByTestId("save-basket")).toHaveTextContent("Save as basket");
    expect(screen.getByRole("radiogroup", { name: "Holding profile" })).toBeInTheDocument();
  });
});
