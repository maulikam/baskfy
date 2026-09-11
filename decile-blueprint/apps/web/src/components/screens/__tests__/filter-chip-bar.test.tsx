import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DEMO_FACTORS } from "@/app/(app)/kitchen-sink/fixtures";
import { FactorList } from "@/components/data/factor-combobox";
import { FilterChipBar } from "@/components/screens/filter-chip-bar";
import { defaultDefinition } from "@/lib/screens/defaults";

/**
 * The "Sorted by" chip, and the bug that made it unusable.
 *
 * Reported 27 Aug 2026: the factor dropdown could not be used from the chip bar. The cause was
 * structural rather than visual — `FilterChipBar` rendered `FactorCombobox`, which opens a Radix
 * popover **inside** the chip's own Radix popover. Radix portals popover content to
 * `document.body`, outside the DOM subtree of whatever opened it, so every click in the nested
 * list registered as an *outside* click on the chip: the chip dismissed itself, the list unmounted
 * with it, and the factors were gone before one could be chosen.
 *
 * It was the only nested usage in the app. `filter-sections.tsx` renders the same combobox in a
 * plain panel, which is why the identical control has always worked there — and why the bug read
 * as "the sort dropdown is broken" rather than "comboboxes are broken".
 */

/**
 * `cmdk` observes its list and scrolls the selected item into view; this jsdom has neither
 * `ResizeObserver` nor `Element.scrollIntoView`. The same stub `command-palette.test.tsx` and
 * `results-panel.test.tsx` install, for the same reason.
 */
function installCmdkDomStubs() {
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
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      writable: true,
      configurable: true,
      value: () => {},
    });
  }
}

beforeEach(installCmdkDomStubs);

const CHIP_BAR = readFileSync(
  resolve(process.cwd(), "src/components/screens/filter-chip-bar.tsx"),
  "utf8",
);

describe("the chip bar does not nest one popover inside another", () => {
  /**
   * Components that open a Radix popover of their own. Rendering one inside a `PopoverContent`
   * reintroduces the dismissal conflict above, in whichever chip does it.
   */
  const OPENS_ITS_OWN_POPOVER = ["FactorCombobox"];

  it.each(OPENS_ITS_OWN_POPOVER)("does not render %s inside a chip popover", (component) => {
    expect(
      CHIP_BAR.includes(`<${component}`),
      `${component} opens its own popover; render its inline form instead (see FactorList)`,
    ).toBe(false);
  });

  it("renders the factor list inline, which is the fix", () => {
    expect(CHIP_BAR).toContain("<FactorList");
  });
});

describe("the inline factor list is usable", () => {
  it("shows the factors, grouped by family", () => {
    render(<FactorList factors={DEMO_FACTORS} value={null} onChange={vi.fn()} />);
    // The headings docs/08 asks for, so the list is navigable rather than 64 flat rows.
    expect(screen.getByText("Absolute return")).toBeInTheDocument();
    expect(screen.getByText("Sharpe return")).toBeInTheDocument();
    // Every factor is offered — the list is not silently truncated, which was the reported feel.
    expect(screen.getAllByRole("option")).toHaveLength(DEMO_FACTORS.length);
  });

  it("filters as you type", async () => {
    const user = userEvent.setup();
    render(<FactorList factors={DEMO_FACTORS} value={null} onChange={vi.fn()} />);
    await user.type(screen.getByRole("combobox"), "sharpe");

    const options = screen.getAllByRole("option");
    expect(options.length).toBeGreaterThan(0);
    expect(options.length).toBeLessThan(DEMO_FACTORS.length);
  });

  it("says so when nothing matches, rather than showing an empty box", async () => {
    const user = userEvent.setup();
    render(<FactorList factors={DEMO_FACTORS} value={null} onChange={vi.fn()} />);
    await user.type(screen.getByRole("combobox"), "zzzznotafactor");
    expect(screen.getByText(/No factor matches/)).toBeInTheDocument();
  });

  it("reports the chosen factor — the click the nested version never received", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<FactorList factors={DEMO_FACTORS} value={null} onChange={onChange} />);

    await user.click(screen.getAllByRole("option")[0]!);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0]![0]).toBe(DEMO_FACTORS[0]!.key);
  });

  it("bounds its own height so a 64-item list cannot run off the screen", () => {
    /* The other half of the report — "so big it is not visible". An unbounded list of 64 factors
       is taller than most viewports, and a floating surface that overflows takes its last options
       with it. The cap lives on the list itself, so every surface that renders it inherits it. */
    const { container } = render(
      <FactorList factors={DEMO_FACTORS} value={null} onChange={vi.fn()} />,
    );
    const list = container.querySelector("[cmdk-list]");
    expect(list?.className).toMatch(/max-h-/);
    expect(list?.className).toMatch(/overflow-y-auto/);
  });
});

/**
 * G3 of `gates/leaf-7.2.1-chip-labels.md`: "The chip's accessible name equals its visible label."
 *
 * §1.1 fixed what the universe chip *shows*. This is the other half, and it is the half that goes
 * wrong silently: a chip can read `NIFTY Total Market` on screen while announcing something else
 * entirely, and nothing about the page looks broken. The three ways that happens are all live
 * possibilities in this file — an `aria-label` added "for clarity" that then drifts from the text
 * beside it; a decorative chevron that loses its `aria-hidden` and appends its own name; and the
 * loading state, where the label is derived from the slug and a mismatch would mean a screen
 * reader hearing `nifty-total-market` while the screen says `NIFTY Total Market`.
 *
 * So each case below asserts the same identity twice over, from both directions: the button's
 * `textContent` is the expected label, and a role query *for that exact name* finds the same node.
 * `getByRole(..., { name })` runs the real accessible-name computation, so an `aria-label` would
 * make the second assertion fail while the first still passed, and an un-hidden chevron would
 * break the first. Asserting only one of them is how this gate would pass while being wrong.
 *
 * Both moments of the page's life are covered because they are produced by different code paths:
 * `universeChipLabel` reads the slug before `/meta/universes` answers and the published name
 * afterwards, and the whole point of §1.1 is that those two paths agree.
 */
describe("G3: the chip announces exactly what it shows", () => {
  const UNIVERSES = [
    { slug: "nifty-total-market", name: "NIFTY TOTAL MARKET" },
    { slug: "nifty-500", name: "NIFTY 500" },
  ];

  function renderBar(overrides: {
    // The definition's own index type, not `string`: `definition.index` is a slug union, and a
    // widened `string` is the TS2322 that had `make lint` red across the whole repo.
    index?: ReturnType<typeof defaultDefinition>["index"];
    universes?: readonly { slug: string; name: string }[];
  }) {
    return render(
      <FilterChipBar
        definition={{ ...defaultDefinition(), index: overrides.index ?? "nifty-total-market" }}
        patch={() => {}}
        factors={DEMO_FACTORS}
        universes={(overrides.universes ?? []) as never}
        operands={[]}
        tradingDays={[]}
        dataStartDate={null}
        latestDate={null}
        onReset={() => {}}
      />,
    );
  }

  function assertNameMatchesLabel(expected: string) {
    const chip = screen.getByTestId("chip-index");
    // Visible: the chevron contributes nothing because it is aria-hidden and empty.
    expect(chip.textContent?.trim()).toBe(expected);
    // Accessible: the real name computation, not a reading of the same DOM property.
    expect(screen.getByRole("button", { name: expected })).toBe(chip);
  }

  it("before /meta/universes answers, which is every first paint", () => {
    renderBar({ universes: [] });
    assertNameMatchesLabel("NIFTY Total Market");
  });

  it("after the published name lands", () => {
    renderBar({ universes: UNIVERSES });
    assertNameMatchesLabel("NIFTY Total Market");
  });

  it("for a name whose digits must not be re-cased", () => {
    renderBar({ index: "nifty-500", universes: UNIVERSES });
    assertNameMatchesLabel("NIFTY 500");
  });

  it("never announces the raw slug in either state", () => {
    const { unmount } = renderBar({ universes: [] });
    expect(screen.queryByRole("button", { name: /nifty-total-market/i })).toBeNull();
    unmount();
    renderBar({ universes: UNIVERSES });
    expect(screen.queryByRole("button", { name: /nifty-total-market/i })).toBeNull();
  });

  it("adds no aria-label that could drift from the text", () => {
    // The failure this catches is a later edit, not today's markup: an aria-label added to the
    // chip would keep the page looking right and make the two names diverge on the next relabel.
    renderBar({ universes: UNIVERSES });
    expect(screen.getByTestId("chip-index").hasAttribute("aria-label")).toBe(false);
  });
});
