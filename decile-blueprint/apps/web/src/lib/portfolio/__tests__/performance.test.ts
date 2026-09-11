import { describe, expect, it } from "vitest";

import {
  attribution,
  benchmarkExplanation,
  blockedRanges,
  clampSpan,
  eventMarkers,
  fullSpan,
  isNarrowed,
  NOT_DECOMPOSABLE,
  OFFERED_RANGES,
  PERIOD_UNAVAILABLE,
  performanceSeries,
  plotFor,
  ratioAsPercent,
  readoutAt,
  seriesExplanation,
  servableRanges,
  spanPnl,
  type PerformanceSeries,
} from "@/lib/portfolio/performance";
import type { DrawdownPoint, NavPoint, NavSeries, PortfolioRow } from "@/lib/portfolio/overview";

/**
 * The performance workspace's arithmetic, asserted against the spec rather than against the code.
 *
 * Four of these would each be a real defect in a product that places live orders:
 *
 *   · a deposit reading as a return, which is the single most common way a portfolio chart lies;
 *   · a range the brief names disappearing without saying why;
 *   · an attribution whose rows do not add up to the figure they claim to explain;
 *   · a figure appearing for an effect Baskfy cannot decompose.
 */

/* Fixtures carry NO `as` and no cast. `NavSeriesOut`, `NavPointOut` and `PortfolioRowOut` are
   generated from the API's own OpenAPI document, so a cast here would let a fixture describe a
   payload the server never sends and still go green. House rule 2. */

function mark(on: string, value: string, over: Partial<NavPoint> = {}): NavPoint {
  return { on, value, cash: "0", net_flow: "0", pending_reconciliation: false, ...over };
}

function fall(on: string, index: string, peak: string, drawdown = "0"): DrawdownPoint {
  return { on, index, peak, drawdown };
}

function series(over: Partial<NavSeries> = {}): NavSeries {
  return {
    range: "1Y",
    pending_reconciliation: false,
    from_on: "2026-09-01",
    to_on: "2026-09-03",
    total_return: { label: "Total return", since: "2026-09-01", value: "0.100000" },
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
    ...over,
  };
}

/**
 * The series at the heart of the deposit test.
 *
 * Day two takes a ₹50,000 assignment on a day the market did not move: the value line jumps by
 * half and the wealth index does not move at all. Day three is a genuine 10% gain.
 */
function withDeposit(): NavSeries {
  return series({
    total_return: { label: "Total return", since: "2026-09-01", value: "0.100000" },
    points: [
      mark("2026-09-01", "100000"),
      mark("2026-09-02", "150000", { net_flow: "50000" }),
      mark("2026-09-03", "165000"),
    ],
    drawdown: [
      fall("2026-09-01", "100", "100000"),
      fall("2026-09-02", "100", "150000"),
      fall("2026-09-03", "110", "165000"),
    ],
    daily_pnl: [
      { on: "2026-09-02", amount: "0", pct: "0.000000" },
      { on: "2026-09-03", amount: "15000", pct: "0.100000" },
    ],
  });
}

function withBenchmark(over: Partial<NavSeries> = {}): NavSeries {
  return series({
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
      points: [mark("2026-09-01", "25000"), mark("2026-09-02", "25500"), mark("2026-09-03", "26000")],
    },
    ...over,
  });
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
    todays_pnl: { amount: "0", label: "Change since the previous close" },
    headline_return: {
      kind: "SINCE_GROUPED",
      label: "Since grouped",
      since: "2026-01-01",
      is_model: false,
    },
    ...over,
  };
}

function portfolioSeries(points: NavPoint[]): PerformanceSeries {
  return performanceSeries({
    range: "1Y",
    pending_reconciliation: false,
    total_return: { label: "Total return", since: "2026-09-01" },
    points,
  });
}

/* ------------------------------------------------------------------ units */

describe("the units the API actually sends", () => {
  it("a stored ratio becomes a percentage exactly once", () => {
    /* `portfolio_nav._quantise_return` sends 1.99% as 0.019900. Rendering that with a "%" beside
       it would claim two hundredths of a percent, which is the wrong answer by a factor of 100. */
    expect(ratioAsPercent("0.019900")).toBe("1.99");
    expect(ratioAsPercent("-0.082000")).toBe("-8.20");
    expect(ratioAsPercent(null)).toBeNull();
    expect(ratioAsPercent("")).toBeNull();
  });

  it("the capital line is the opening value carried forward by the flows", () => {
    const built = performanceSeries(withDeposit());

    expect(built.points.map((point) => point.capital)).toEqual(["100000", "150000", "150000"]);
  });

  it("the four arrays are joined by date, not by position", () => {
    /* `daily_pnl` is deliberately one entry shorter than `points` — one per transition, because
       the first mark has no previous close. Aligning by index would put every day's move one row
       out and nothing on the screen would look wrong. */
    const built = performanceSeries(series());

    expect(built.points[0]?.dayPnl).toBeNull();
    expect(built.points[1]?.dayPnl).toBe("4000");
    expect(built.points[2]?.dayPnl).toBe("6000");
  });
});

/* ------------------------------------------------------------------ ranges */

describe("the ranges the brief names", () => {
  it("range: every one of 1D, 1W, 1M, 3M, 1Y, 3Y and All is accounted for", () => {
    expect(OFFERED_RANGES.map((option) => option.key)).toEqual([
      "1D",
      "1W",
      "1M",
      "3M",
      "1Y",
      "3Y",
      "ALL",
    ]);
  });

  it("range: 1D and 1W are unavailable WITH a reason, never silently dropped", () => {
    const blocked = blockedRanges();

    expect(blocked.map((option) => option.key)).toEqual(["1D", "1W"]);
    for (const option of blocked) {
      expect(option.available).toBe(false);
      expect(option.unavailable).not.toBeNull();
      expect(option.unavailable ?? "").toContain("intraday");
    }
  });

  it("range: every servable option is one the API will actually answer", () => {
    /* `NavRange` has no 1D member — the server's own docstring says a member that cannot honestly
       be served is worse than a missing one. This asserts the client never asks for one. */
    expect(servableRanges().map((option) => option.key)).toEqual(["1M", "3M", "1Y", "3Y", "ALL"]);
    for (const option of servableRanges()) {
      expect(option.unavailable).toBeNull();
    }
  });

  it("range: the brush never collapses the window to a single mark", () => {
    const built = performanceSeries(series());

    expect(clampSpan(built, { from: 2, to: 2 })).toEqual({ from: 1, to: 2 });
    expect(clampSpan(built, { from: 0, to: 0 })).toEqual({ from: 0, to: 1 });
    /* Reversed handles are a legal drag, not an error. */
    expect(clampSpan(built, { from: 2, to: 0 })).toEqual({ from: 0, to: 2 });
    /* And nothing escapes the array. */
    expect(clampSpan(built, { from: -5, to: 99 })).toEqual({ from: 0, to: 2 });
  });

  it("range: a brushed window is known to be narrower than the whole series", () => {
    const built = performanceSeries(series());

    expect(isNarrowed(built, fullSpan(built))).toBe(false);
    expect(isNarrowed(built, { from: 1, to: 2 })).toBe(true);
  });
});

/* ------------------------------------------------------------------ the three views */

describe("value, return and drawdown are three views of one series", () => {
  it("deposit: the return view is the wealth index, so money paid in earns nothing", () => {
    const built = performanceSeries(withDeposit());
    const span = fullSpan(built);

    const value = plotFor(built, "VALUE", span);
    const returns = plotFor(built, "RETURN", span);

    /* The value line does jump by half on the day of the assignment … */
    expect(value.lines.find((line) => line.key === "portfolio")?.values).toEqual([
      100000, 150000, 165000,
    ]);
    /* … and a percentage derived from THAT line would read +50% for day two. The return view
       reads the wealth index instead, which did not move: the strategy is not credited with the
       user's own deposit. This is the confusion `HeroOut` separates XIRR from TWR to avoid. */
    expect(returns.lines.find((line) => line.key === "portfolio")?.values).toEqual([0, 0, 10]);
  });

  it("deposit: the capital line rises with the deposit so the gap stays honest", () => {
    const built = performanceSeries(withDeposit());
    const value = plotFor(built, "VALUE", fullSpan(built));

    /* Both lines move by the same ₹50,000 on day two, so the gap between them — which is what a
       reader takes as profit — is unchanged by a transfer. */
    expect(value.lines.find((line) => line.key === "capital")?.values).toEqual([
      100000, 150000, 150000,
    ]);
  });

  it("deposit: a window's profit removes every flow inside it", () => {
    const built = performanceSeries(withDeposit());

    /* 165,000 − 100,000 − 50,000 paid in = 15,000, which is exactly the two daily moves chained. */
    expect(spanPnl(built, fullSpan(built))).toBe("15000");
  });

  it("the benchmark is rebased to the left edge in both units", () => {
    const built = performanceSeries(withBenchmark());
    const returns = plotFor(built, "RETURN", fullSpan(built));
    const value = plotFor(built, "VALUE", fullSpan(built));

    /* In return mode both lines start at 0% — the only rebasing under which "am I ahead of it?"
       is answered by which line is higher. */
    expect(returns.lines.find((line) => line.key === "benchmark")?.values?.[0]).toBe(0);
    /* In value mode the index is scaled to the opening value, not left as an index level. */
    expect(value.lines.find((line) => line.key === "benchmark")?.values?.[0]).toBe(100000);
  });

  it("the drawdown view draws the portfolio alone and says why", () => {
    const built = performanceSeries(
      series({
        drawdown: [
          fall("2026-09-01", "100", "100000", "0"),
          fall("2026-09-02", "96", "100000", "-0.040000"),
          fall("2026-09-03", "110", "110000", "0"),
        ],
      }),
    );
    const plot = plotFor(built, "DRAWDOWN", fullSpan(built));

    expect(plot.lines.map((line) => line.key)).toEqual(["drawdown"]);
    expect(plot.lines[0]?.values).toEqual([0, -4, 0]);
    expect(plot.caption).toContain("no drawdown series for the benchmark");
  });

  it("unavailable: the return view says what it is missing rather than drawing a flat line", () => {
    const built = performanceSeries(series({ drawdown: [] }));
    const plot = plotFor(built, "RETURN", fullSpan(built));

    expect(plot.lines).toHaveLength(0);
    expect(plot.unavailable).toContain("wealth index");
  });

  it("colour: every line carries a direct label and a stroke pattern named in words", () => {
    const built = performanceSeries(withBenchmark());
    const plot = plotFor(built, "VALUE", fullSpan(built));

    /* Three lines in one chart must remain three lines in greyscale, so each carries its own
       word for how it is drawn as well as its own tone. */
    expect(plot.lines.map((line) => line.dashWord).sort()).toEqual(["dashed", "dotted", "solid"]);
    for (const line of plot.lines) {
      expect(line.label.length).toBeGreaterThan(0);
      expect(line.definition.length).toBeGreaterThan(0);
    }
  });
});

/* ------------------------------------------------------------------ the read-out */

describe("the read-out for one day", () => {
  it("readout: gives the date, the value, the day's move and the cumulative return", () => {
    const built = performanceSeries(withBenchmark());
    const readout = readoutAt(built, fullSpan(built), 2);

    expect(readout?.on).toBe("2026-09-03");
    expect(readout?.value.value).toBe("110000");
    expect(readout?.dayChange.value).toBe("6000");
    expect(readout?.cumulative.value).toBe("10.00");
    expect(readout?.benchmark.value).toBe("4.00");
    expect(readout?.relative.value).toBe("6.00");
  });

  it("readout: the first day of the window says why it has no change, rather than reading zero", () => {
    const built = performanceSeries(series());
    const readout = readoutAt(built, fullSpan(built), 0);

    expect(readout?.dayChange.value).toBeNull();
    expect(readout?.dayChange.unavailable).toContain("first day in the window");
  });

  it("unavailable: a day the index did not print names the index and the date", () => {
    const built = performanceSeries(
      withBenchmark({
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
          /* The index printed on the first two days and not on the third — two calendars, which
             is normal the moment either side has a gap. */
          points: [mark("2026-09-01", "25000"), mark("2026-09-02", "25500")],
        },
      }),
    );
    const readout = readoutAt(built, fullSpan(built), 2);

    expect(readout?.benchmark.value).toBeNull();
    expect(readout?.benchmark.unavailable).toContain("NIFTY 500");
    expect(readout?.benchmark.unavailable).toContain("2026-09-03");
    /* And the comparison that depends on it does not quietly become the portfolio's own number. */
    expect(readout?.relative.value).toBeNull();
  });

  it("readout: the cumulative return is measured from the brush's left edge, not the series start", () => {
    const built = performanceSeries(series());
    const readout = readoutAt(built, { from: 1, to: 2 }, 2);

    /* 110 / 104 − 1 = 5.77%, not the 10% measured from day one. A brush that did not re-base
       would report the whole series' return under a window that excludes half of it. */
    expect(readout?.cumulative.value).toBe("5.77");
  });
});

/* ------------------------------------------------------------------ events */

describe("event markers", () => {
  it("a flow, a freeze, the peak and the trough are the only things marked", () => {
    const built = performanceSeries(
      series({
        points: [
          mark("2026-09-01", "100000"),
          mark("2026-09-02", "150000", { net_flow: "50000" }),
          mark("2026-09-03", "140000", { net_flow: "-5000", pending_reconciliation: true }),
        ],
        max_drawdown: { drawdown: "-0.040000", peak_on: "2026-09-02", trough_on: "2026-09-03" },
      }),
    );

    expect(eventMarkers(built).map((event) => event.kind)).toEqual([
      "money-in",
      "peak",
      "money-out",
      "unreconciled",
      "trough",
    ]);
  });

  it("colour: each marker carries a word and a sentence, not only a tick", () => {
    const built = performanceSeries(
      series({
        points: [
          mark("2026-09-01", "100000"),
          mark("2026-09-02", "150000", { net_flow: "50000" }),
          mark("2026-09-03", "155000"),
        ],
      }),
    );

    const marker = eventMarkers(built)[0];
    expect(marker?.label).toBe("Money in");
    expect(marker?.detail).toContain("not a gain");
  });
});

/* ------------------------------------------------------------------ states */

describe("every state explains itself", () => {
  it("state: an empty series says what would produce the first mark", () => {
    const explanation = seriesExplanation(performanceSeries(series({ points: [] })));

    expect(explanation?.kind).toBe("no-series");
    expect(explanation?.detail).toContain("daily valuation pass");
    expect(explanation?.action).not.toBeNull();
  });

  it("state: a single mark says a line needs two", () => {
    const explanation = seriesExplanation(
      performanceSeries(series({ points: [mark("2026-09-03", "110000")] })),
    );

    expect(explanation?.kind).toBe("single-mark");
    expect(explanation?.headline).toContain("2026-09-03");
  });

  it("state: a drawable series blocks nothing", () => {
    expect(seriesExplanation(performanceSeries(series()))).toBeNull();
  });

  it("state: a missing benchmark explains itself without blocking the chart", () => {
    const built = performanceSeries(series());

    expect(seriesExplanation(built)).toBeNull();
    expect(benchmarkExplanation(built)?.kind).toBe("no-benchmark");
  });

  it("state: an index with one print inside the window says so", () => {
    const built = performanceSeries(
      withBenchmark({
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
    );

    expect(benchmarkExplanation(built)?.kind).toBe("sparse-benchmark");
    expect(benchmarkExplanation(built)?.headline).toContain("one close");
  });
});

/* ------------------------------------------------------------------ attribution */

describe("attribution by portfolio", () => {
  const rows = [
    row(1, "Long term", "2000000", {
      todays_pnl: { amount: "10000", label: "Change since the previous close" },
    }),
    row(2, "Swing Manual", "1500000", {
      todays_pnl: { amount: "-4000", label: "Change since the previous close" },
    }),
  ];

  it("attribution: the rows add up to the book's own figure for today", () => {
    const built = attribution({
      rows,
      todaysTotal: "6000",
      todaysUnavailable: null,
      unallocatedValue: "0",
      period: null,
    });

    expect(built.today.reconciliation.explained).toBe("6000");
    expect(built.today.reconciliation.reported).toBe("6000");
    expect(built.today.reconciliation.reconciles).toBe(true);
    expect(built.today.reconciliation.note).toContain("to the last paisa");
  });

  it("attribution: a residual is named, never folded into the largest row", () => {
    const built = attribution({
      rows,
      todaysTotal: "6300",
      todaysUnavailable: null,
      unallocatedValue: "180000",
      period: null,
    });

    expect(built.today.reconciliation.residual).toBe("300");
    expect(built.today.reconciliation.reconciles).toBe(false);
    expect(built.today.reconciliation.note).toContain("no portfolio");
    /* The rows themselves are untouched — the gap is stated, not absorbed. */
    expect(built.today.rows.map((entry) => entry.amount.value)).toEqual(["10000", "-4000"]);
  });

  it("attribution: the share bar is a share of the GROSS move, so mixed signs stay readable", () => {
    const built = attribution({
      rows,
      todaysTotal: "6000",
      todaysUnavailable: null,
      unallocatedValue: "0",
      period: null,
    });

    /* Against the ₹6,000 net, "share of the move" would read 166.67% and −66.67%. Against the
       ₹14,000 the two portfolios actually moved between them, the bars are comparable. */
    expect(built.today.gross).toBe("14000");
    expect(built.today.mixedSigns).toBe(true);
    expect(built.today.rows.map((entry) => entry.sharePct)).toEqual(["71.43", "28.57"]);
  });

  it("attribution: rows are ordered by how much they moved, in either direction", () => {
    const built = attribution({
      rows: [
        row(1, "Small mover", "2000000", {
          todays_pnl: { amount: "500", label: "Change since the previous close" },
        }),
        row(2, "Big faller", "100000", {
          todays_pnl: { amount: "-90000", label: "Change since the previous close" },
        }),
        row(3, "Unpriced", "300000", {
          todays_pnl: {
            label: "Change since the previous close",
            unavailable_reason: "No previous close for these holdings.",
          },
        }),
      ],
      todaysTotal: "-89500",
      todaysUnavailable: null,
      unallocatedValue: "0",
      period: null,
    });

    /* A big loss answers "what moved the number" as well as a big gain does, and a row with no
       figure has no place in an ordering by figure, so it goes last rather than reading as flat. */
    expect(built.today.rows.map((entry) => entry.name)).toEqual([
      "Big faller",
      "Small mover",
      "Unpriced",
    ]);
    expect(built.today.rows[2]?.amount.value).toBeNull();
    expect(built.today.rows[2]?.amount.unavailable).toContain("No previous close");
  });

  it("attribution: with no per-portfolio series the period band shows no figure and says what it needs", () => {
    const built = attribution({
      rows,
      todaysTotal: "6000",
      todaysUnavailable: null,
      unallocatedValue: "0",
      period: null,
    });

    expect(built.period.unavailable).toBe(PERIOD_UNAVAILABLE);
    expect(built.period.unavailable).toContain("/nav");
    for (const entry of built.period.rows) {
      expect(entry.amount.value).toBeNull();
    }
  });

  it("attribution: with each portfolio's own series the period band reconciles to the aggregate", () => {
    const aggregate = performanceSeries(
      series({
        points: [
          mark("2026-09-01", "100000"),
          mark("2026-09-02", "104000"),
          mark("2026-09-03", "110000"),
        ],
      }),
    );
    const built = attribution({
      rows,
      todaysTotal: "6000",
      todaysUnavailable: null,
      unallocatedValue: "0",
      period: {
        label: "1 Sep 2026 to 3 Sep 2026",
        aggregate,
        span: fullSpan(aggregate),
        byPortfolio: new Map([
          [
            1,
            portfolioSeries([
              mark("2026-09-01", "60000"),
              mark("2026-09-02", "62000"),
              mark("2026-09-03", "66000"),
            ]),
          ],
          [
            2,
            portfolioSeries([
              mark("2026-09-01", "40000"),
              mark("2026-09-02", "42000"),
              mark("2026-09-03", "44000"),
            ]),
          ],
        ]),
      },
    });

    expect(built.period.rows.map((entry) => entry.amount.value)).toEqual(["6000", "4000"]);
    expect(built.period.reconciliation.explained).toBe("10000");
    expect(built.period.reconciliation.reported).toBe("10000");
    expect(built.period.reconciliation.reconciles).toBe(true);
  });

  it("attribution: a portfolio that starts inside the window is measured from its own first mark and says so", () => {
    const aggregate = performanceSeries(series());
    const built = attribution({
      rows,
      todaysTotal: "6000",
      todaysUnavailable: null,
      unallocatedValue: "0",
      period: {
        label: "1 Sep 2026 to 3 Sep 2026",
        aggregate,
        span: fullSpan(aggregate),
        byPortfolio: new Map([
          [
            1,
            portfolioSeries([
              mark("2026-09-01", "60000"),
              mark("2026-09-02", "62000"),
              mark("2026-09-03", "66000"),
            ]),
          ],
          /* Created on the 2nd: it has no February, and silently measuring from its own first
             mark without saying so is how a two-day-old portfolio becomes the year's best. */
          [2, portfolioSeries([mark("2026-09-02", "42000"), mark("2026-09-03", "44000")])],
        ]),
      },
    });

    const late = built.period.rows.find((entry) => entry.portfolioId === 2);
    expect(late?.partialFrom).toBe("2026-09-02");
    expect(built.period.reconciliation.reconciles).toBe(false);
  });

  it("attribution: with no capital portfolios the band says so instead of rendering an empty list", () => {
    const built = attribution({
      rows: [],
      todaysTotal: null,
      todaysUnavailable: "No previous close to compare against yet.",
      unallocatedValue: null,
      period: null,
    });

    expect(built.today.rows).toHaveLength(0);
    expect(built.today.unavailable).toContain("no capital portfolios");
  });

  it("attribution: a monitoring view never enters the arithmetic", () => {
    /* A lens overlaps the portfolios it lenses, so adding its move would count the same shares
       twice — the same rule §4.1 states for every total on this screen. */
    const built = attribution({
      rows: [
        ...rows,
        row(9, "High momentum", "900000", {
          kind: "MONITORING",
          counts_toward_total: false,
          todays_pnl: { amount: "7000", label: "Change since the previous close" },
        }),
      ],
      todaysTotal: "6000",
      todaysUnavailable: null,
      unallocatedValue: "0",
      period: null,
    });

    expect(built.today.rows.map((entry) => entry.name)).not.toContain("High momentum");
    expect(built.today.reconciliation.explained).toBe("6000");
  });
});

describe("what cannot be decomposed", () => {
  it("unavailable: every effect the brief names carries a reason and what would unblock it", () => {
    const names = NOT_DECOMPOSABLE.map((effect) => effect.name).join(" | ");

    for (const expected of [
      "sector",
      "Allocation effect",
      "Security-selection effect",
      "Cash drag",
      "Fees, brokerage and taxes",
      "holding",
    ]) {
      expect(names).toContain(expected);
    }
    for (const effect of NOT_DECOMPOSABLE) {
      expect(effect.reason.length).toBeGreaterThan(40);
      expect(effect.unblockedBy.length).toBeGreaterThan(10);
    }
  });

  it("unavailable: none of them carries a figure", () => {
    /* A number beside one of these names would be invented, and this product places live orders. */
    for (const effect of NOT_DECOMPOSABLE) {
      expect(/^[^:]{0,40}[-+]?\d/.test(effect.name)).toBe(false);
    }
  });

  it("attribution: cash drag is blocked for the reason it is actually blocked", () => {
    /* It looks computable — every mark carries a cash balance — and it is not: `net_flow` records
       money entering and leaving the ACCOUNT, never money moving between cash and shares inside
       it, so the equity-only return cannot be separated from the total. */
    const drag = NOT_DECOMPOSABLE.find((effect) => effect.name === "Cash drag");

    expect(drag?.reason).toContain("net_flow");
    expect(drag?.reason).toContain("inside it");
  });
});
