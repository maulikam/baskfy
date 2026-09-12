import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PerformanceWorkspace } from "@/components/portfolio/command/performance-workspace";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { DrawdownPoint, NavPoint, NavRange, NavSeries, PortfolioRow } from "@/lib/portfolio/overview";

/**
 * The performance workspace as a person meets it.
 *
 * Written against the brief's own sentences rather than against the markup, because the markup is
 * the part that is allowed to change. The failures these exist to catch are each one a reader
 * could act on:
 *
 *   · a range pill that is dim and says nothing, so it gets clicked again tomorrow;
 *   · a read-out that shows a dash on a day the index did not print;
 *   · an attribution effect Baskfy cannot compute appearing with a number beside it;
 *   · a chart whose three lines are three colours and nothing else.
 */

/* Schema-exact fixtures, no `as` and no cast — `NavSeriesOut` and `PortfolioRowOut` are generated
   from the API's OpenAPI document, so `tsc` fails if a fixture describes a payload that does not
   exist. House rule 2, and PC1's third recorded defect. */

function mark(on: string, value: string, over: Partial<NavPoint> = {}): NavPoint {
  return { on, value, cash: "0", net_flow: "0", pending_reconciliation: false, ...over };
}

function fall(on: string, index: string, peak: string, drawdown = "0"): DrawdownPoint {
  return { on, index, peak, drawdown };
}

function chart(over: Partial<NavSeries> = {}): NavSeries {
  return {
    range: "1Y",
    pending_reconciliation: false,
    from_on: "2026-09-01",
    to_on: "2026-09-03",
    total_return: { label: "Total return", since: "2026-09-01", value: "0.100000" },
    max_drawdown: { drawdown: "-0.040000", peak_on: "2026-09-01", trough_on: "2026-09-02" },
    points: [
      mark("2026-09-01", "100000"),
      mark("2026-09-02", "104000"),
      mark("2026-09-03", "110000"),
    ],
    drawdown: [
      fall("2026-09-01", "100", "100000"),
      fall("2026-09-02", "104", "104000"),
      fall("2026-09-03", "110", "110000"),
    ],
    daily_pnl: [
      { on: "2026-09-02", amount: "4000", pct: "0.040000" },
      { on: "2026-09-03", amount: "6000", pct: "0.057692" },
    ],
    benchmark: {
      name: "NIFTY 500",
      portfolio: {
        kind: "TWR_SINCE_CREATED",
        label: "Your return",
        since: "2026-09-01",
        is_model: false,
        value: "0.100000",
      },
      benchmark: {
        kind: "TWR_SINCE_CREATED",
        label: "NIFTY 500",
        since: "2026-09-01",
        is_model: false,
        value: "0.040000",
      },
      difference: "0.060000",
      points: [
        mark("2026-09-01", "25000"),
        mark("2026-09-02", "25500"),
        mark("2026-09-03", "26000"),
      ],
    },
    ...over,
  };
}

/**
 * The same payload with no benchmark and no recorded worst fall.
 *
 * `exactOptionalPropertyTypes` is on, so `{ benchmark: undefined }` is NOT the same type as a
 * payload with no `benchmark` key — and a payload with no key is what the API actually sends for
 * a book with no index set. Deleting the key is the only way to write that fixture without a
 * cast, which is the point of schema-exact fixtures.
 */
function stripped(series: NavSeries): NavSeries {
  const { benchmark: _benchmark, max_drawdown: _maxDrawdown, ...rest } = series;
  return rest;
}

function row(id: number, name: string, value: string, over: Partial<PortfolioRow> = {}): PortfolioRow {
  return {
    portfolio_id: id,
    name,
    kind: "CAPITAL",
    source: "HOLDING_GROUP",
    source_badge: "Grouped",
    started_on: "2026-01-01",
    value,
    cash: "0",
    counts_toward_total: true,
    status: "Synced",
    holdings_count: 3,
    broker_count: 1,
    pending_reconciliation: false,
    todays_pnl: { amount: "10000", label: "Change since the previous close" },
    headline_return: {
      kind: "SINCE_GROUPED",
      label: "Since grouped",
      since: "2026-01-01",
      is_model: false,
    },
    ...over,
  };
}

const ROWS: readonly PortfolioRow[] = [
  row(1, "Long term", "2000000"),
  row(2, "Swing Manual", "1500000", {
    todays_pnl: { amount: "-4000", label: "Change since the previous close" },
  }),
];

/** `app/providers.tsx` mounts `TooltipProvider` app-wide; a test renders a subtree, so it supplies
 *  the same context rather than the component carrying a provider it would never use in place. */
function renderWorkspace(
  over: Partial<React.ComponentProps<typeof PerformanceWorkspace>> = {},
) {
  const onRangeChange = vi.fn<(range: NavRange) => void>();
  const view = render(
    <TooltipProvider>
      <PerformanceWorkspace
        chart={chart()}
        rows={ROWS}
        todaysTotal="6000"
        unallocatedValue="0"
        onRangeChange={onRangeChange}
        {...over}
      />
    </TooltipProvider>,
  );
  return { ...view, onRangeChange };
}

afterEach(() => {
  vi.restoreAllMocks();
});

/* ------------------------------------------------------------------ the ranges */

describe("the period controls", () => {
  it("range: every window the API serves is offered as a pill", () => {
    renderWorkspace();

    for (const key of ["1M", "3M", "1Y", "3Y", "ALL"]) {
      expect(screen.getByTestId(`performance-range-${key}`)).toBeEnabled();
    }
    expect(screen.getByTestId("performance-range-1Y")).toHaveAttribute("aria-pressed", "true");
  });

  it("range: picking one asks the parent to fetch it", async () => {
    const user = userEvent.setup();
    const { onRangeChange } = renderWorkspace();

    await user.click(screen.getByTestId("performance-range-3M"));

    expect(onRangeChange).toHaveBeenCalledWith("3M");
  });

  it("range: 1D and 1W are declared with their reason, not drawn as pills that do nothing", () => {
    renderWorkspace();

    /* A dim pill is the defect: it teaches a reader to click it again tomorrow. There is no 1D
       control at all, and the reason it is absent is on the screen. */
    expect(screen.queryByTestId("performance-range-1D")).toBeNull();
    expect(screen.queryByTestId("performance-range-1W")).toBeNull();

    const note = screen.getByTestId("performance-range-unavailable");
    expect(note).toHaveTextContent("1D and 1W are not available");
    expect(note).toHaveTextContent("one mark per trading day");
    expect(note).toHaveTextContent("intraday");
  });

  it("range: the brush narrows the window and says which days are shown", () => {
    renderWorkspace();
    const brush = screen.getByTestId("performance-brush");

    expect(brush).toHaveTextContent("3 trading days");

    fireEvent.change(screen.getByTestId("performance-brush-start"), { target: { value: "1" } });

    expect(brush).toHaveTextContent("2 trading days");
    /* The abbreviation for September is "Sep" on some ICU builds and "Sept" on others, and which
       one this Node has is not what the brush is being tested for. */
    expect(brush.textContent ?? "").toMatch(/2 Sept? 2026/);
    /* And there is one way back to the whole series, offered only once it is needed. */
    expect(screen.getByTestId("performance-brush-reset")).toBeInTheDocument();
  });

  it("range: with no way to refetch, the pills say so rather than swallowing the click", () => {
    renderWorkspace({ onRangeChange: undefined });

    expect(screen.getByTestId("performance-range-3M")).toBeDisabled();
    expect(screen.getByTestId("performance-range-unavailable")).toHaveTextContent(
      "served a single window",
    );
  });
});

/* ------------------------------------------------------------------ the read-out */

describe("the hover read-out", () => {
  it("hover: gives the date, the value, the day's change, the cumulative return and the benchmark's", () => {
    renderWorkspace();
    const readout = screen.getByTestId("performance-readout");

    /* It opens on the last day of the window, which is the day a reader arrives asking about. */
    expect(readout.textContent ?? "").toMatch(/3 Sept? 2026/);
    expect(readout).toHaveTextContent("₹1,10,000");
    expect(readout).toHaveTextContent("That day");
    expect(readout).toHaveTextContent("₹6,000");
    expect(readout).toHaveTextContent("Since the left edge");
    expect(readout).toHaveTextContent("10.00%");
    expect(readout).toHaveTextContent("NIFTY 500");
    expect(readout).toHaveTextContent("4.00%");
    expect(readout).toHaveTextContent("Ahead or behind");
    expect(readout).toHaveTextContent("6.00%");
  });

  it("hover: moving the read-out to another day re-reads every figure", () => {
    renderWorkspace();

    fireEvent.change(screen.getByTestId("performance-cursor"), { target: { value: "1" } });

    const readout = screen.getByTestId("performance-readout");
    expect(readout.textContent ?? "").toMatch(/2 Sept? 2026/);
    expect(readout).toHaveTextContent("₹1,04,000");
    expect(readout).toHaveTextContent("₹4,000");
  });

  it("hover: pointing at the plot moves the read-out to the day under the pointer", () => {
    /* jsdom runs no layout, so the element has to be told how wide it is rendered — the pointer
       path itself is real, and it is the path a mouse actually takes. */
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 960,
      bottom: 300,
      width: 960,
      height: 300,
      toJSON: () => ({}),
    });
    renderWorkspace();

    /* The left-hand edge of the plot area is the first mark. */
    fireEvent.pointerMove(screen.getByTestId("performance-plot"), { clientX: 68 });

    expect(screen.getByTestId("performance-readout").textContent ?? "").toMatch(/1 Sept? 2026/);
  });

  it("hover: a day the benchmark did not print shows the reason, never a bare dash", () => {
    renderWorkspace({
      chart: chart({
        benchmark: {
          name: "NIFTY 500",
          portfolio: {
            kind: "TWR_SINCE_CREATED",
            label: "Your return",
            since: "2026-09-01",
            is_model: false,
            value: "0.100000",
          },
          benchmark: {
            kind: "TWR_SINCE_CREATED",
            label: "NIFTY 500",
            since: "2026-09-01",
            is_model: false,
            value: "0.020000",
          },
          points: [mark("2026-09-01", "25000"), mark("2026-09-02", "25500")],
        },
      }),
    });

    const readout = screen.getByTestId("performance-readout");
    expect(readout).toHaveTextContent("Needs more data");
    expect(readout.textContent ?? "").not.toContain("—");
    expect(readout.querySelector("[title*='NIFTY 500']")).not.toBeNull();
  });

  it("hover: the first day of the window explains its missing change rather than reading zero", () => {
    renderWorkspace();

    fireEvent.change(screen.getByTestId("performance-cursor"), { target: { value: "0" } });

    const readout = screen.getByTestId("performance-readout");
    expect(readout).toHaveTextContent("Needs more data");
    expect(readout.querySelector("[title*='first day']")).not.toBeNull();
  });
});

/* ------------------------------------------------------------------ the three views */

describe("value, return and falls from the peak", () => {
  it("colour: every line is labelled on the chart and its stroke is named in words", () => {
    renderWorkspace();

    /* The brief asks for direct labels rather than a large legend, and never colour alone. Both
       are satisfied by the same thing: the line says what it is, where it ends. */
    expect(screen.getByTestId("performance-direct-label-portfolio")).toHaveTextContent("Value");
    expect(screen.getByTestId("performance-direct-label-capital")).toHaveTextContent(
      "Capital in play",
    );
    expect(screen.getByTestId("performance-direct-label-benchmark")).toHaveTextContent("NIFTY 500");

    expect(screen.getByTestId("performance-line-portfolio")).toHaveTextContent("solid");
    expect(screen.getByTestId("performance-line-capital")).toHaveTextContent("dotted");
    expect(screen.getByTestId("performance-line-benchmark")).toHaveTextContent("dashed");
  });

  it("colour: the headline figures carry a direction glyph as well as a hue", () => {
    renderWorkspace();

    /* ▲ for the gain, ▼ for the fall — the direction survives greyscale and colour blindness. */
    const headline = screen.getByTestId("performance-headline").textContent ?? "";
    expect(headline).toContain("▲");
    expect(headline).toContain("▼");
  });

  it("the return view replaces the value axis with the wealth index", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    await user.click(screen.getByTestId("performance-view-RETURN"));

    expect(screen.getByTestId("performance-view-RETURN")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("performance-direct-label-portfolio")).toHaveTextContent(
      "Your return",
    );
    /* The capital line has no meaning in return units — zero is where you started, and the axis
       rule says so instead of a second line that would always be flat. */
    expect(screen.queryByTestId("performance-direct-label-capital")).toBeNull();
  });

  it("the falls overlay hangs beneath the value chart when asked for", async () => {
    const user = userEvent.setup();
    renderWorkspace();

    expect(screen.queryByTestId("performance-overlay")).toBeNull();
    await user.click(screen.getByTestId("performance-falls-toggle"));

    expect(screen.getByTestId("performance-overlay")).toBeInTheDocument();
  });

  it("an event is marked with a glyph and explains itself in the read-out", () => {
    renderWorkspace({
      chart: chart({
        points: [
          mark("2026-09-01", "100000"),
          mark("2026-09-02", "154000", { net_flow: "50000" }),
          mark("2026-09-03", "160000"),
        ],
      }),
    });

    fireEvent.change(screen.getByTestId("performance-cursor"), { target: { value: "1" } });

    const events = screen.getByTestId("performance-readout-events");
    expect(events).toHaveTextContent("Money in");
    expect(events).toHaveTextContent("not a gain");
  });
});

/* ------------------------------------------------------------------ attribution */

describe("what moved the number", () => {
  it("attribution: each portfolio's share of today, reconciling to the book's own figure", () => {
    renderWorkspace();
    const band = screen.getByTestId("attribution-today");

    expect(band).toHaveTextContent("Long term");
    expect(band).toHaveTextContent("₹10,000");
    expect(band).toHaveTextContent("Swing Manual");
    expect(band).toHaveTextContent("-₹4,000");
    expect(screen.getByTestId("attribution-today-reconciliation")).toHaveTextContent(
      "These add to ₹6,000",
    );
    expect(screen.getByTestId("attribution-today-reconciliation")).toHaveTextContent(
      "to the last paisa",
    );
  });

  it("attribution: a gap between the rows and the book's figure is named on the screen", () => {
    renderWorkspace({ todaysTotal: "6300", unallocatedValue: "180000" });

    const line = screen.getByTestId("attribution-today-reconciliation");
    expect(line).toHaveTextContent("The difference of ₹300");
    expect(line).toHaveTextContent("no portfolio");
  });

  it("attribution: mixed signs are stated rather than hidden behind a net figure", () => {
    renderWorkspace();

    expect(screen.getByTestId("attribution-today-offset")).toHaveTextContent(
      "the portfolios moved ₹14,000 between them",
    );
  });

  it("colour: a contribution carries its direction as a glyph and as words for a screen reader", () => {
    renderWorkspace();
    const band = screen.getByTestId("attribution-today");

    expect(band.textContent ?? "").toContain("▲");
    expect(band.textContent ?? "").toContain("▼");
    expect(within(band).getByTestId("attribution-row-1")).toHaveTextContent("gain of");
    expect(within(band).getByTestId("attribution-row-2")).toHaveTextContent("loss of");
  });

  it("unavailable: the period band shows no figure and says what is missing, in the reader's terms", () => {
    /* THIS TEST USED TO ASSERT THE OPPOSITE, and it was pinning a defect.
       It required the copy to contain `/nav` — "names the endpoint it needs" — so the screen told
       a retail investor which API route would serve the figure. A screenshot of the assembled page
       is what caught it: the same instinct had put `GET /api/v1/...`, `net_flow`, `packages/core`
       and a live `http://127.0.0.1:8100` in front of a reader. House rule 2 says a test asserts
       the SPEC, and the spec is the brief's copy style: concise, factual, calm. The engineering
       detail is not lost; it is in `PERIOD_UNAVAILABLE`'s doc comment, where the person who can
       act on it is reading. `no-internals.test.tsx` now holds the rule for the whole tree. */
    renderWorkspace();
    const band = screen.getByTestId("attribution-period");

    expect(band).toHaveTextContent("Needs more data");
    expect(band).toHaveTextContent("each portfolio's own value history");
    /* Apportioning the window's profit by size would assume every portfolio returned the same
       thing, which is the assumption this panel exists to test. */
    expect(band).toHaveTextContent("every portfolio returned the same thing");
    expect(band.textContent ?? "").not.toMatch(/\/nav|\/api\/v\d|GET |POST /);
  });

  it("unavailable: with each portfolio's own series the period band reconciles instead", () => {
    const byPortfolio = new Map<number, NavSeries>([
      [
        1,
        stripped(
          chart({
            points: [
              mark("2026-09-01", "60000"),
              mark("2026-09-02", "62000"),
              mark("2026-09-03", "66000"),
            ],
          }),
        ),
      ],
      [
        2,
        stripped(
          chart({
            points: [
              mark("2026-09-01", "40000"),
              mark("2026-09-02", "42000"),
              mark("2026-09-03", "44000"),
            ],
          }),
        ),
      ],
    ]);
    renderWorkspace({ seriesByPortfolio: byPortfolio });

    const band = screen.getByTestId("attribution-period");
    expect(band).toHaveTextContent("₹6,000");
    expect(band).toHaveTextContent("₹4,000");
    expect(screen.getByTestId("attribution-period-reconciliation")).toHaveTextContent(
      "These add to ₹10,000",
    );
  });
});

describe("what the panel refuses to invent", () => {
  it("unavailable: every effect the brief names that Baskfy cannot compute is listed with its reason", () => {
    renderWorkspace();
    const blocked = screen.getByTestId("attribution-blocked");

    /* Four, not five. Fees left this list on 11 Sep 2026 — its own entry said the cost model
       existed and only the wiring was missing, which is an unbuilt thing sitting in a list of
       impossible ones. It is wired; see `estimated-costs` below. */
    for (const name of [
      "Contribution by sector",
      "Allocation effect",
      "Security-selection effect",
      "Cash drag",
    ]) {
      expect(blocked).toHaveTextContent(name);
    }
    expect(blocked.textContent ?? "").not.toContain("Fees, brokerage and taxes");
    /* The reason now says what a READER needs, not which table lacks a column. `instrument` is a
       schema name and it reached the screen because the no-internals scan looks for snake_case,
       paths and routes — a bare table name that is also an English word walked straight through
       it. Found by Maulik reading the rendered panel. */
    expect(blocked).toHaveTextContent("does not record which sector a company belongs to");
    expect(blocked.textContent ?? "").not.toContain("`instrument`");
    expect(blocked).toHaveTextContent("Needs:");

    /* And contribution by holding is a DESTINATION here, not a caveat: it exists, and saying
       otherwise devalues the five above it. */
    expect(blocked).toHaveTextContent("Open one to see which of its holdings moved it");
  });

  it("unavailable: none of the blocked effects is ever shown with a number", () => {
    renderWorkspace();
    const text = screen.getByTestId("attribution-blocked").textContent ?? "";

    /* A figure beside one of these names would be invented, and this product places live orders
       with real money. They may appear as prose — that is the point — never as "Cash drag −0.4%". */
    for (const name of ["Cash drag", "Allocation effect", "Security-selection effect"]) {
      const withFigure = new RegExp(`${name}[^a-zA-Z]{0,3}[-+−]?\\d`);
      expect(withFigure.test(text), `"${name}" is shown with a figure`).toBe(false);
    }
  });

  it("unavailable: nothing on the workspace is phrased as investment advice", () => {
    renderWorkspace();

    /* Baskfy is not a registered adviser (D3), so this surface may describe the data and never
       the trade. */
    const text = screen.getByTestId("performance-workspace").textContent ?? "";
    expect(text).not.toMatch(/\b(you should buy|you should sell|we recommend|hot stock|guaranteed)\b/i);
  });

  it("unavailable: no bare em dash reaches the screen, on the payload most likely to produce one", () => {
    renderWorkspace({
      chart: stripped(
        chart({ daily_pnl: [], total_return: { label: "Total return", since: "2026-09-01" } }),
      ),
      todaysTotal: null,
      todaysUnavailable: "No previous close to compare against yet.",
      rows: [
        row(1, "Long term", "2000000", {
          todays_pnl: {
            label: "Change since the previous close",
            unavailable_reason: "No previous close for these holdings.",
          },
        }),
      ],
    });

    /* The rule is that no figure is ever a bare dash — not that the character is banned from
       prose, where it is punctuation. So this scans the regions where a NUMBER is rendered:
       `formatRupees` returns "—" for anything it cannot parse, and every one of those paths runs
       on this payload. */
    for (const region of ["performance-headline", "performance-readout", "attribution-today"]) {
      const element = screen.getByTestId(region);
      expect(element.textContent ?? "", `${region} rendered a bare dash`).not.toContain("—");
      expect(element).toHaveTextContent("Needs more data");
    }
    /* Metric tiles keep the reason on title; the attribution band still names it in the section. */
    expect(
      screen
        .getByTestId("performance-headline")
        .querySelector('[title*="Needs at least two end-of-day marks"]'),
    ).not.toBeNull();
    expect(screen.getByTestId("attribution-today")).toHaveTextContent("No previous close");
  });
});

/* ------------------------------------------------------------------ states */

describe("every state explains itself", () => {
  it("state: an empty series explains what would produce the first mark, with one next action", () => {
    renderWorkspace({ chart: chart({ points: [], drawdown: [], daily_pnl: [] }) });

    const explain = screen.getByTestId("performance-empty");
    expect(explain).toHaveTextContent("No end-of-day valuations recorded yet");
    expect(explain).toHaveTextContent("daily valuation pass");
    expect(within(explain).getByRole("link")).toHaveTextContent("Check broker sync");
    /* And no empty frame is drawn behind it. */
    expect(screen.queryByTestId("performance-plot")).toBeNull();
  });

  it("state: a single mark says a line needs two and names the day it has", () => {
    renderWorkspace({
      chart: chart({
        points: [mark("2026-09-03", "110000")],
        drawdown: [fall("2026-09-03", "110", "110000")],
        daily_pnl: [],
      }),
    });

    const explain = screen.getByTestId("performance-empty");
    expect(explain).toHaveTextContent("Only one mark so far");
    expect(explain).toHaveTextContent("2026-09-03");
  });

  it("state: a book with no benchmark explains the missing line without blocking the chart", () => {
    renderWorkspace({ chart: stripped(chart()) });

    expect(screen.getByTestId("performance-plot")).toBeInTheDocument();
    const explain = screen.getByTestId("performance-benchmark-state");
    expect(explain).toHaveTextContent("No benchmark set for these portfolios");
    expect(within(explain).getByRole("link")).toBeInTheDocument();
  });

  it("state: an index with too few prints inside the window says so", () => {
    renderWorkspace({
      chart: chart({
        benchmark: {
          name: "NIFTY 500",
          portfolio: {
            kind: "TWR_SINCE_CREATED",
            label: "Your return",
            since: "2026-09-01",
            is_model: false,
          },
          benchmark: {
            kind: "TWR_SINCE_CREATED",
            label: "NIFTY 500",
            since: "2026-09-01",
            is_model: false,
          },
          points: [mark("2026-09-03", "26000")],
        },
      }),
    });

    expect(screen.getByTestId("performance-benchmark-state")).toHaveTextContent(
      "NIFTY 500 has one close inside this window",
    );
  });

  it("state: a view that cannot be drawn says which one and why", async () => {
    const user = userEvent.setup();
    renderWorkspace({ chart: chart({ drawdown: [] }) });

    await user.click(screen.getByTestId("performance-view-RETURN"));

    expect(screen.getByTestId("performance-view-unavailable")).toHaveTextContent("wealth index");
    expect(screen.queryByTestId("performance-plot")).toBeNull();
  });

  it("state: the workspace offers no way to place an order", () => {
    renderWorkspace();

    const text = screen.getByTestId("performance-workspace").textContent ?? "";
    expect(text).not.toMatch(/place (an )?order|buy now|execute/i);
    expect(screen.queryByRole("button", { name: /order|execute/i })).toBeNull();
  });
});

/* ------------------------------------------------------------------ fees, once it was wired
 *
 * This was the sixth entry in "what this cannot be broken down by", and its own text gave the
 * game away: *"Needs: wiring the existing cost model through the portfolio rebalance path."* Not
 * missing data — unbuilt. The cost model was measured against 163 real fills and reproduces every
 * component exactly, and `portfolio_cash_flow` already held the buys and sells.
 *
 * The figure is now computed server-side. **What these tests hold is the honesty**, because the
 * danger the old entry named is precisely the one shipping a number creates: the value series is
 * still not net of these charges, and a reader who reads this as a net-of-fees return is wrong. */

const COSTS = {
  stt: "7349.55",
  exchange: "218.28",
  sebi: "12.30",
  stamp: "840.73",
  gst: "47.95",
  dp: "127.44",
  brokerage: "0.00",
  total: "8596.25",
  turnover: "7349552.00",
  trades: 163,
  sell_scrip_days: 8,
  bps_of_turnover: "11.70",
  caveat:
    "An estimate of what these trades cost at current statutory rates, not a billed amount. " +
    "Returns shown elsewhere are before these charges, not after them.",
};

describe("fees, brokerage and taxes", () => {
  it("fees: the figure is shown with its components rather than one opaque total", () => {
    renderWorkspace({ estimatedCosts: COSTS });
    const panel = screen.getByTestId("estimated-costs");

    expect(panel).toHaveTextContent("Securities transaction tax");
    expect(panel).toHaveTextContent("Stamp duty");
    expect(panel).toHaveTextContent("Depository charge");
    /* Brokerage is genuinely nothing for delivery, and saying so is worth a line — omitting it
       would leave a reader assuming it was folded into the total. */
    expect(panel).toHaveTextContent("Brokerage");
  });

  it("fees: the caveat says the returns shown elsewhere are NOT net of these", () => {
    /* The gate the whole feature turns on. Without this sentence the panel implies a net-of-fees
       return that no series behind it actually is. */
    renderWorkspace({ estimatedCosts: COSTS });
    const panel = screen.getByTestId("estimated-costs");

    expect(panel).toHaveTextContent("not a billed amount");
    expect(panel).toHaveTextContent("before these charges, not after them");
  });

  it("fees: a book with no recorded trades says why, and never shows zero", () => {
    renderWorkspace({ estimatedCosts: null });
    const absent = screen.getByTestId("estimated-costs-absent");

    expect(absent).toHaveTextContent("No buys or sells are recorded");
    expect(absent).toHaveTextContent("broker sync");
    expect(absent.textContent ?? "").not.toMatch(/₹\s*0(\.00)?\b/);
  });

  it("fees: it is no longer listed as something that cannot be broken down", () => {
    renderWorkspace({ estimatedCosts: COSTS });
    const blocked = screen.getByTestId("attribution-blocked");
    expect(blocked.textContent ?? "").not.toContain("Fees, brokerage and taxes");
  });
});
