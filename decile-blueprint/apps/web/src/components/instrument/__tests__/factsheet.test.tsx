import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { Factsheet } from "@/components/instrument/factsheet";
import { CUPID_FACTSHEET, YOUNG_FACTSHEET } from "@/components/instrument/__tests__/fixtures";
import { EMPTY_CELL } from "@/lib/format";
import { instrumentJsonLd, instrumentMetadata, instrumentTitle } from "@/lib/instrument/seo";

/**
 * The rendered factsheet — Prompt 10 acceptance criteria 1 and 3.
 *
 * The numbers themselves are the API's, and `services/api/tests/test_api_instruments.py` asserts
 * it derives them correctly. What this suite asserts is that the page shows them: docs/01 §5's
 * eleven blocks in order, the reference product's six PROS lines word for word, and — for an
 * instrument with less than a year of history — an em dash where there is no value, never a zero.
 */

function draw(sheet = CUPID_FACTSHEET) {
  return render(
    <TooltipProvider>
      <Factsheet sheet={sheet} series={{}} />
    </TooltipProvider>,
  );
}

/**
 * docs/01 §5 block 3, verbatim: "'The close is above 200-day moving average.' (×4 for
 * 200/100/50/20), 'The close is within 25% of all time high.', 'The beta is less than 1.25'".
 *
 * Six lines, in the reference product's order. docs/05 §16 says "Ship **at least**" its eight
 * rules and all eight fire for this instrument, so the assertion is that these six lead the list
 * — see `docs/10a` §1 for why two more follow them.
 */
const REFERENCE_PROS = [
  "The close is above 200-day moving average.",
  "The close is above 100-day moving average.",
  "The close is above 50-day moving average.",
  "The close is above 20-day moving average.",
  "The close is within 25% of all time high.",
  "The beta is less than 1.25",
];

describe("PROS and CONS", () => {
  it("renders the reference product's six lines, in order", () => {
    draw();
    const pros = screen.getByRole("region", { name: "Pros" });
    const rendered = within(pros)
      .getAllByRole("listitem")
      .map((item) => item.textContent?.trim());
    expect(rendered.slice(0, REFERENCE_PROS.length)).toEqual(REFERENCE_PROS);
  });

  it("says nothing is violated rather than showing an empty list", () => {
    draw();
    const cons = screen.getByRole("region", { name: "Cons" });
    expect(within(cons).getByText(/no rule is violated/i)).toBeInTheDocument();
  });

  it("explains the rules it cannot decide instead of implying a con", () => {
    draw(YOUNG_FACTSHEET);
    expect(screen.getByText(/7 rules need data this instrument does not have/i)).toBeInTheDocument();
    expect(screen.queryByText(/the close is below 200-day moving average/i)).toBeNull();
  });
});

describe("docs/01 §5 block order", () => {
  it("renders the eleven blocks in the teardown's order", () => {
    draw();
    const headings = screen
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);
    expect(headings).toEqual([
      "Key stats",
      "Pros and cons",
      "Metrics",
      "Price & moving averages",
      "Returns, Sharpe, volatility and RSI",
      "Market quality",
      "Corporate actions",
    ]);
  });

  it("puts the header first, with the exchange print and the membership chips", () => {
    draw();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("CUPID");
    expect(screen.getByText("₹284.03")).toBeInTheDocument();
    expect(screen.getByText("NSE: CUPID")).toBeInTheDocument();
    const chips = screen.getByRole("list", { name: "Index membership" });
    expect(within(chips).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "NIFTY TOTAL MARKET",
      "NIFTY MICROCAP 250",
    ]);
  });

  it("leaves the disclaimer to the frame rather than repeating it", () => {
    // `AppShell` renders one on every page; two on one screen reads as a bug. See `factsheet.tsx`.
    draw();
    expect(screen.queryByRole("complementary", { name: /disclaimer/i })).toBeNull();
  });
});

describe("units", () => {
  it("shows volatility as a percentage of the stored fraction", () => {
    draw();
    // docs/13 §2 finding 4: stored 0.57931794, displayed 57.93%.
    expect(screen.getByText("57.93%")).toBeInTheDocument();
  });

  it("signs returns and leaves the share of positive days unsigned", () => {
    draw();
    // Twice: the metric card and the returns grid — the same number, formatted once.
    expect(screen.getAllByText("+726.63%")).toHaveLength(2);
    expect(screen.getByText("65.59%")).toBeInTheDocument();
  });

  it("shows marketcap as integer crore", () => {
    draw();
    expect(screen.getAllByText("38,192 cr").length).toBeGreaterThan(0);
  });
});

describe("an instrument with less than a year of history", () => {
  it("renders every unavailable window as an em dash, never as zero", () => {
    draw(YOUNG_FACTSHEET);
    const grid = screen.getByRole("table", {
      name: /returns, sharpe returns, volatility and rsi/i,
    });
    const sharpe = within(grid).getByRole("row", { name: /^Sharpe/ });
    const values = within(sharpe)
      .getAllByRole("cell")
      .map((cell) => cell.textContent?.trim());
    expect(values).toEqual([
      EMPTY_CELL,
      EMPTY_CELL,
      EMPTY_CELL,
      EMPTY_CELL,
      EMPTY_CELL,
    ]);
    expect(values).not.toContain("0.00");
  });

  it("shows an em dash for the missing metric-card values and medians", () => {
    draw(YOUNG_FACTSHEET);
    const cards = screen.getByRole("region", { name: "Metrics" });
    expect(within(cards).getAllByText(EMPTY_CELL).length).toBeGreaterThan(0);
    expect(within(cards).queryByText("Median: 0")).toBeNull();
  });

  it("shows no regime rather than guessing one", () => {
    draw(YOUNG_FACTSHEET);
    const quality = screen.getByRole("region", { name: "Market quality" });
    expect(within(quality).getAllByText(EMPTY_CELL).length).toBeGreaterThan(0);
    expect(within(quality).queryByText("BULL")).toBeNull();
  });

  it("says there are no corporate actions instead of rendering an empty table", () => {
    draw(YOUNG_FACTSHEET);
    expect(screen.getByText(/no corporate actions are on record/i)).toBeInTheDocument();
  });
});

describe("percentile bars", () => {
  it("names the comparison universe", () => {
    draw();
    expect(screen.getByText(/ranked against NIFTY MICROCAP 250/i)).toBeInTheDocument();
  });

  it("states each percentile in text, not only as a bar", () => {
    draw();
    const grid = screen.getByRole("table", {
      name: /returns, sharpe returns, volatility and rsi/i,
    });
    expect(within(grid).getAllByText(/99th percentile in this universe/).length).toBeGreaterThan(0);
  });
});

describe("corporate actions", () => {
  it("flags the actions that rebase adjusted history", () => {
    draw();
    expect(screen.getAllByText("Adjusts historical prices")).toHaveLength(2);
  });

  it("renders ratios as a:b and dividends as an amount", () => {
    draw();
    const table = screen.getByRole("table", { name: /corporate actions/i });
    expect(within(table).getByText("4:1")).toBeInTheDocument();
    expect(within(table).getByText("10:1")).toBeInTheDocument();
    expect(within(table).getByText("₹1.50")).toBeInTheDocument();
  });
});

describe("SEO", () => {
  it("uses docs/08's title, verbatim", () => {
    expect(instrumentTitle("CUPID")).toBe("CUPID share price, momentum & factor analysis");
    expect(instrumentMetadata(CUPID_FACTSHEET).title).toBe(instrumentTitle("CUPID"));
  });

  it("writes a description that states the date and the number", () => {
    const description = String(instrumentMetadata(CUPID_FACTSHEET).description);
    expect(description).toContain("18 Aug 2026");
    expect(description).toContain("726.63%");
    expect(description).toContain("Not investment advice.");
  });

  it("is canonical, and does not claim to be indexable from behind the gate", () => {
    /* This asserted `index: true` until AUDIT 4.8 (`d24126a`): an instrument page sits behind
       the session gate and redirects a crawler to `/login`, so inviting one in was a promise the
       route could not keep. The canonical stays — it is what makes the two casings of a symbol
       one page. `lib/instrument/__tests__/seo-robots.test.ts` is the decision's own pin. */
    const metadata = instrumentMetadata(CUPID_FACTSHEET);
    expect(metadata.robots).toEqual({ index: false, follow: false });
    expect(metadata.alternates?.canonical).toBe("/instruments/CUPID");
  });

  it("emits JSON-LD that describes a dataset, not a financial product", () => {
    const graph = instrumentJsonLd(CUPID_FACTSHEET)["@graph"];
    expect(Array.isArray(graph)).toBe(true);
    const types = (graph as { "@type": string }[]).map((node) => node["@type"]);
    expect(types).toEqual(["BreadcrumbList", "Dataset"]);
    expect(types).not.toContain("FinancialProduct");
  });

  it("puts the instrument's own numbers in the JSON-LD", () => {
    const graph = instrumentJsonLd(CUPID_FACTSHEET)["@graph"] as Record<string, unknown>[];
    const dataset = graph[1] as { temporalCoverage: string; variableMeasured: string[] };
    expect(dataset.temporalCoverage).toBe("2026-08-18");
    expect(dataset.variableMeasured).toContain("sharpe_12m");
  });
});
