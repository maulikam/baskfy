import type { ScreenDefinition } from "@baskfy/api-client";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { describe, expect, it } from "vitest";

import { FilterChipBar } from "@/components/screens/filter-chip-bar";
import { columnDisplayLabel } from "@/lib/screens/column-display";
import { defaultDefinition } from "@/lib/screens/defaults";
import {
  UNIVERSE_ACRONYMS,
  UNIVERSE_LABEL_FALLBACK,
  universeChipLabel,
  universeLabelFromName,
  universeLabelFromSlug,
} from "@/lib/screens/universe-label";

/**
 * §1.1: the index chip reads `NIFTY Total Market`, at every moment of the page's life.
 *
 * The table below is not a sample. It is the whole of what
 * `GET /api/v1/meta/universes` returned on 23 Aug 2026, transcribed slug and name, and the
 * expectation beside each one is what §1.1's rule produces for it. A universe added to the seed
 * without a line here is a universe nobody has decided the label for.
 */
const PUBLISHED: readonly { slug: string; name: string; label: string }[] = [
  { slug: "nifty-50", name: "NIFTY 50", label: "NIFTY 50" },
  { slug: "nifty-next-50", name: "NIFTY NEXT 50", label: "NIFTY Next 50" },
  { slug: "nifty-100", name: "NIFTY 100", label: "NIFTY 100" },
  { slug: "nifty-200", name: "NIFTY 200", label: "NIFTY 200" },
  { slug: "nifty-500", name: "NIFTY 500", label: "NIFTY 500" },
  { slug: "nifty-total-market", name: "NIFTY TOTAL MARKET", label: "NIFTY Total Market" },
  { slug: "nifty-large-mid-250", name: "NIFTY LARGE MID 250", label: "NIFTY Large Mid 250" },
  { slug: "nifty-midcap-150", name: "NIFTY MIDCAP 150", label: "NIFTY Midcap 150" },
  { slug: "nifty-smallcap-250", name: "NIFTY SMALLCAP 250", label: "NIFTY Smallcap 250" },
  { slug: "nifty-microcap-250", name: "NIFTY MICROCAP 250", label: "NIFTY Microcap 250" },
  { slug: "nifty-mid-small-400", name: "NIFTY MID SMALL 400", label: "NIFTY Mid Small 400" },
  { slug: "nifty-fno", name: "NIFTY FNO", label: "NIFTY FNO" },
  { slug: "nifty-allcap", name: "All NSE Listed Stocks", label: "All NSE Listed Stocks" },
  { slug: "etf", name: "All NSE Listed ETFs", label: "All NSE Listed ETFs" },
];

/** Slug-shaped: lower-case words joined by a hyphen or an underscore. */
const SLUG_SHAPED = /^[a-z0-9]+([-_][a-z0-9]+)*$/;

describe("the published universe list", () => {
  it("is all fourteen, so nothing below passes by sampling", () => {
    expect(PUBLISHED).toHaveLength(14);
    expect(new Set(PUBLISHED.map((u) => u.slug)).size).toBe(14);
  });
});

describe("universeLabelFromName", () => {
  it.each(PUBLISHED)("turns $name into $label", ({ name, label }) => {
    expect(universeLabelFromName(name)).toBe(label);
  });

  it("keeps the brand and lower-cases only the descriptive words", () => {
    expect(universeLabelFromName("NIFTY TOTAL MARKET")).toBe("NIFTY Total Market");
  });

  it("leaves a number alone", () => {
    expect(universeLabelFromName("NIFTY 50")).toBe("NIFTY 50");
  });

  it("leaves a name already written for humans completely alone", () => {
    // `NSE` is an acronym inside an otherwise sentence-cased name; title-casing must not eat it.
    expect(universeLabelFromName("All NSE Listed Stocks")).toBe("All NSE Listed Stocks");
    expect(universeLabelFromName("All NSE Listed ETFs")).toBe("All NSE Listed ETFs");
  });

  it("is idempotent — relabelling a label changes nothing", () => {
    for (const { name, label } of PUBLISHED) {
      expect(universeLabelFromName(universeLabelFromName(name))).toBe(label);
    }
  });

  it("reads a name in any case the seed might carry", () => {
    expect(universeLabelFromName("nifty total market")).toBe("NIFTY Total Market");
    expect(universeLabelFromName("Nifty Total Market")).toBe("NIFTY Total Market");
    expect(universeLabelFromName("nIfTy ToTaL mArKeT")).toBe("NIFTY Total Market");
  });

  it("survives stray whitespace", () => {
    expect(universeLabelFromName("  NIFTY   TOTAL MARKET  ")).toBe("NIFTY Total Market");
    expect(universeLabelFromName("NIFTY 50\n")).toBe("NIFTY 50");
  });

  it("handles a single word", () => {
    expect(universeLabelFromName("ETF")).toBe("ETF");
    expect(universeLabelFromName("SMALLCAP")).toBe("Smallcap");
  });

  it("carries punctuation between words through untouched", () => {
    expect(universeLabelFromName("NIFTY LARGE-MID 250")).toBe("NIFTY Large-Mid 250");
  });

  it("never returns an empty label", () => {
    expect(universeLabelFromName("")).toBe(UNIVERSE_LABEL_FALLBACK);
    expect(universeLabelFromName("   ")).toBe(UNIVERSE_LABEL_FALLBACK);
  });
});

describe("universeLabelFromSlug", () => {
  it.each(PUBLISHED)("reads $slug as $label before the fetch lands", ({ slug, label }) => {
    expect(universeLabelFromSlug(slug)).toBe(label);
  });

  it("agrees with the published name for every slug, so the chip never rewrites itself", () => {
    // The reason a slug-derived guess is defensible at all: for all fourteen it is the same
    // string the server is about to send. If that ever stops being true, prefer a skeleton.
    for (const { slug, name } of PUBLISHED) {
      expect(universeLabelFromSlug(slug)).toBe(universeLabelFromName(name));
    }
  });

  it("never returns anything slug-shaped", () => {
    for (const { slug } of PUBLISHED) {
      const label = universeLabelFromSlug(slug);
      expect(label).not.toBe(slug);
      expect(label).not.toMatch(SLUG_SHAPED);
      expect(label).not.toMatch(/[-_]/);
    }
  });

  it("reads a slug that is not in the published list", () => {
    expect(universeLabelFromSlug("nifty-bank")).toBe("NIFTY Bank");
    expect(universeLabelFromSlug("bse-sensex-30")).toBe("Bse Sensex 30");
  });

  it("accepts a slug in the wrong case or with stray whitespace", () => {
    expect(universeLabelFromSlug("  NIFTY-TOTAL-MARKET  ")).toBe("NIFTY Total Market");
    expect(universeLabelFromSlug("Nifty_Total_Market")).toBe("NIFTY Total Market");
  });

  it("shrugs off a malformed slug rather than emitting one", () => {
    expect(universeLabelFromSlug("nifty--total---market-")).toBe("NIFTY Total Market");
    expect(universeLabelFromSlug("-")).toBe(UNIVERSE_LABEL_FALLBACK);
    expect(universeLabelFromSlug("")).toBe(UNIVERSE_LABEL_FALLBACK);
    expect(universeLabelFromSlug("   ")).toBe(UNIVERSE_LABEL_FALLBACK);
  });
});

describe("universeChipLabel", () => {
  it("prefers the published name once the universe list has loaded", () => {
    expect(universeChipLabel("nifty-total-market", PUBLISHED)).toBe("NIFTY Total Market");
  });

  it("falls back to the slug while the universe list is still empty", () => {
    for (const { slug, label } of PUBLISHED) {
      expect(universeChipLabel(slug, [])).toBe(label);
    }
  });

  it("falls back to the slug for an index the list does not carry", () => {
    expect(universeChipLabel("nifty-bank", PUBLISHED)).toBe("NIFTY Bank");
  });

  it("falls back to the slug when the published name is blank", () => {
    const blank = [{ slug: "nifty-total-market", name: "   " }];
    expect(universeChipLabel("nifty-total-market", blank)).toBe("NIFTY Total Market");
  });

  it("never returns an empty string, whatever it is handed", () => {
    expect(universeChipLabel("", [])).toBe(UNIVERSE_LABEL_FALLBACK);
    expect(universeChipLabel("   ", PUBLISHED)).toBe(UNIVERSE_LABEL_FALLBACK);
  });
});

describe("the acronym set", () => {
  it("is explicit, and carries the four the universe list needs", () => {
    for (const acronym of ["NIFTY", "NSE", "ETF", "FNO"]) {
      expect(UNIVERSE_ACRONYMS.has(acronym)).toBe(true);
    }
  });

  it("is what decides the casing, so adding one changes the label", () => {
    // `PSU` is deliberately absent: an acronym nobody has added is an ordinary word.
    expect(UNIVERSE_ACRONYMS.has("PSU")).toBe(false);
    expect(universeLabelFromName("NIFTY PSU BANK")).toBe("NIFTY Psu Bank");
  });
});

// --- the chip itself -------------------------------------------------------

function asUniverses(rows: readonly { slug: string; name: string }[]) {
  return rows.map((universe, index) => ({
    slug: universe.slug,
    name: universe.name,
    index_id: index + 1,
    market_health: true,
    sort_order: index,
  }));
}

function renderChipBar(
  universes: readonly { slug: string; name: string }[] = PUBLISHED,
  definition: Partial<ScreenDefinition> = {},
) {
  return render(
    createElement(FilterChipBar, {
      definition: { ...defaultDefinition(), index: "nifty-total-market", ...definition },
      patch: () => undefined,
      factors: [],
      universes: asUniverses(universes),
      operands: [],
      tradingDays: [],
      dataStartDate: null,
      latestDate: null,
      onReset: () => undefined,
    }),
  );
}

describe("the index chip", () => {
  it("shows the human label, and its accessible name is that same string", () => {
    renderChipBar(PUBLISHED);
    const chip = screen.getByTestId("chip-index");

    expect(chip.textContent).toBe("NIFTY Total Market");
    expect(chip).toHaveAccessibleName("NIFTY Total Market");
  });

  it("shows no raw slug before the universe list resolves", () => {
    renderChipBar([]);
    const chip = screen.getByTestId("chip-index");

    expect(chip.textContent).toBe("NIFTY Total Market");
    expect(chip).toHaveAccessibleName("NIFTY Total Market");
    expect(chip.textContent).not.toContain("nifty-total-market");
  });
});

describe("the sort-by chip", () => {
  /*
   * Guarded here rather than changed: the mapping lives in `column-display.ts`, which this leaf
   * does not touch. The chip renders `columnDisplayLabel(sort_by, factor.label ?? sort_by)`, so
   * the failure mode is the API's own label leaking through — `AVERAGE SHARPE RETURN 12 6 3 1
   * MONTHS` — if the key ever falls out of `COLUMN_DISPLAY`.
   */
  it("maps the default sort factor to its human name", () => {
    expect(defaultDefinition().sort_by).toBe("avg_sharpe_12_6_3_1");
    expect(
      columnDisplayLabel("avg_sharpe_12_6_3_1", "AVERAGE SHARPE RETURN 12 6 3 1 MONTHS"),
    ).toBe("Consistency score");
  });

  it("renders the human name even when the factor list carries the shouty one", () => {
    renderChipBar(PUBLISHED);
    const chip = screen.getByTestId("chip-sort");

    expect(chip.textContent).toContain("Consistency score");
    expect(chip.textContent).not.toContain("AVERAGE SHARPE");
  });
});

describe("the liquidity chip", () => {
  it("reads the open liquidity chip when median_volume_1y is null", () => {
    expect(defaultDefinition().median_volume_1y).toBeNull();
    renderChipBar(PUBLISHED);
    const chip = screen.getByTestId("chip-liquidity");

    expect(chip.textContent).toMatch(/Liquidity:\sany/);
  });

  it("does not read any when Investing 001's ₹1 crore floor is set", () => {
    renderChipBar(PUBLISHED, { median_volume_1y: 10_000_000 });
    const chip = screen.getByTestId("chip-liquidity");

    expect(chip.textContent).not.toMatch(/any/);
    expect(chip.textContent).toMatch(/₹1 crore|1 [Cc]r/);
  });

  it("does not also show the general filled chip when only volume is set", () => {
    renderChipBar(PUBLISHED, { median_volume_1y: 10_000_000 });
    expect(screen.queryByTestId("chip-filter-general")).not.toBeInTheDocument();
  });

  it("still shows the general filled chip when apply_filters_on is not all", () => {
    renderChipBar(PUBLISHED, {
      apply_filters_on: "decile_1",
      median_volume_1y: 10_000_000,
    });
    expect(screen.getByTestId("chip-filter-general")).toBeInTheDocument();
  });

  it("still shows the general filled chip when min_return_1y is set", () => {
    renderChipBar(PUBLISHED, { min_return_1y: "12" });
    expect(screen.getByTestId("chip-filter-general")).toBeInTheDocument();
  });

  it("labels a custom volume as custom or a short rupee figure", () => {
    renderChipBar(PUBLISHED, { median_volume_1y: 1_234_567 });
    const chip = screen.getByTestId("chip-liquidity");

    expect(chip.textContent).toMatch(/Liquidity: (custom|₹)/);
    expect(chip.textContent).not.toMatch(/any/);
  });

  it.each([
    [1_000_000, /₹10 lakh/],
    [2_000_000, /₹20 lakh/],
    [5_000_000, /₹50 lakh/],
    [10_000_000, /₹1 crore/],
    [20_000_000, /₹2 crore/],
    [50_000_000, /₹5 crore/],
    [100_000_000, /₹10 crore/],
  ] as const)("uses the GeneralFilters preset for %s", (volume, label) => {
    renderChipBar(PUBLISHED, { median_volume_1y: volume });
    const chip = screen.getByTestId("chip-liquidity");
    expect(chip.textContent).toMatch(label);
    expect(chip.textContent).not.toMatch(/any/);
  });
});

describe("the + Filter search", () => {
  it("still finds Circuit Filters by the word circuit", async () => {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    Object.defineProperty(globalThis, "ResizeObserver", {
      configurable: true,
      writable: true,
      value: ResizeObserverStub,
    });
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      writable: true,
      value: () => undefined,
    });

    const user = userEvent.setup();
    renderChipBar(PUBLISHED);
    await user.click(screen.getByTestId("chip-add-filter"));
    await user.type(screen.getByTestId("filter-search"), "circuit");

    expect(screen.getByText("Circuit Filters")).toBeInTheDocument();
  });

  it("keeps the standing chip testids on the bar", () => {
    renderChipBar(PUBLISHED);
    expect(screen.getByTestId("chip-index")).toBeInTheDocument();
    expect(screen.getByTestId("chip-sort")).toBeInTheDocument();
    expect(screen.getByTestId("chip-direction")).toBeInTheDocument();
    expect(screen.getByTestId("chip-liquidity")).toBeInTheDocument();
    expect(screen.getByTestId("chip-add-filter")).toBeInTheDocument();
    expect(screen.getByTestId("reset-filters")).toBeInTheDocument();
  });
});
