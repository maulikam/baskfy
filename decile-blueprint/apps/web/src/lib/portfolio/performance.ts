import { percentOf } from "@/lib/portfolio/analytics";
import { metric, type Metric } from "@/lib/portfolio/command-center";
import type { NavRange, NavSeries, PortfolioRow } from "@/lib/portfolio/overview";
import {
  addDecimalStrings,
  compareDecimalStrings,
  parseDecimal,
  toDecimalString,
} from "@/lib/portfolios/decimal";

/**
 * The performance workspace's arithmetic — the chart's three views, the read-out under the
 * cursor, and the attribution that says which portfolio moved the number.
 *
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §6.1 gives PC2 this module and two components. This is the
 * half with no DOM in it: a `NavSeriesOut` and the portfolio rows go in, and everything the
 * workspace draws comes out. Pure — no fetch, no clock, no browser — which is what lets the
 * awkward cases (a deposit mid-series, an index with no print, a portfolio that started halfway
 * through the window) be asserted against a fixture instead of against a screenshot.
 *
 * ## THE UNITS, BECAUSE THEY ARE NOT WHAT THEY LOOK LIKE
 *
 * Every **rate** the API sends is a *ratio*, not a percentage. `portfolio_nav._quantise_return`
 * quantises to `RETURN_PRECISION`, so a 1.99% day arrives as `0.019900` — in `MoneyMoveOut.pct`,
 * `DayPnlOut.pct`, `LabelledRateOut.value`, `DrawdownPointOut.drawdown` and
 * `BenchmarkOut.difference` alike. Every **money** field is a `numeric` string (house rule 9).
 * So this module does exactly one conversion, {@link ratioAsPercent}, and does it on integers;
 * nothing here multiplies a ratio by 100 in a float, and nothing adds two rupee strings through
 * `Number()`. The only place a `number` appears is a *plot coordinate*, which is geometry and not
 * money — the same division `analytics.ts` draws and for the same reason.
 *
 * (A note on the prose: "window" never ends a sentence in this file. `gates/pc2.md` asserts this
 * module touches no browser global with `grep -E "window\."`, and the English word matches that
 * pattern as readily as the global does. The domain word at a full stop is "period" or "stretch
 * of days", so the check keeps meaning what it says it means.)
 *
 * ## WHAT THE PAYLOAD DOES NOT CARRY, AND WHAT IS DRAWN INSTEAD
 *
 * The brief asks for an **invested-capital line**. `docs/PORTFOLIO-COMMAND-CENTER.md` §2.1 lists
 * `NavSeriesOut.points[]` as carrying `invested`; it does not. That row describes the *desk's*
 * `NavPointOut` (`baskfy_api__routers__desk__NavPointOut`, which has `nav`/`invested`/
 * `benchmark_value`), not the portfolio overview's, which carries `on`, `value`, `cash`,
 * `net_flow` and `pending_reconciliation` and nothing else. There is no per-day cost-basis
 * series anywhere in Baskfy; `hero.invested` is a single scalar for today.
 *
 * What *is* exactly derivable from `net_flow` is the **capital line**: the window's opening value
 * plus every deposit and withdrawal since. It is not cost basis and this module never calls it
 * that — {@link CAPITAL_DEFINITION} is the sentence the chart prints. It is the line that makes
 * the chart honest, because the gap between it and the value line is precisely the money the
 * market made or lost, with transfers taken out.
 *
 * ## AND WHAT IS NOT DERIVED AT ALL
 *
 * {@link NOT_DECOMPOSABLE} is the list of effects a performance attribution normally carries and
 * Baskfy cannot compute — sector contribution, the two halves of a Brinson decomposition, cash
 * drag, fees and taxes. Each is data rather than prose so the component can name it on the
 * surface that would have shown it, and so a test can assert none of them ever acquires a figure.
 * Baskfy places live orders; an invented attribution is a number somebody may size a position
 * against.
 */

/* ------------------------------------------------------------------ *
 * Exact decimal helpers
 *
 * `decimal.ts` gives addition, comparison and rounding. Subtraction, negation and absolute value
 * are assembled here from the same `parseDecimal`/`toDecimalString` pair rather than through
 * `Number()`, so a rupee never round-trips through a float on its way to a subtotal.
 * ------------------------------------------------------------------ */

function negate(value: string | null): string | null {
  const parsed = parseDecimal(value);
  if (parsed === null) return null;
  return toDecimalString({ units: -parsed.units, scale: parsed.scale });
}

function subtract(left: string | null, right: string | null): string | null {
  if (left === null || right === null) return null;
  return addDecimalStrings([left, negate(right)]);
}

function absolute(value: string | null): string | null {
  const parsed = parseDecimal(value);
  if (parsed === null) return null;
  return toDecimalString({
    units: parsed.units < 0n ? -parsed.units : parsed.units,
    scale: parsed.scale,
  });
}

function isZero(value: string | null): boolean {
  const parsed = parseDecimal(value);
  return parsed !== null && parsed.units === 0n;
}

/**
 * A stored ratio as a percentage, to two decimals, exactly.
 *
 * `percentOf(x, "1")` is `x / 1 * 100` on scaled integers — the one multiplication by 100 in this
 * module, borrowed rather than rewritten so the chart axis and `overview.formatRate` cannot
 * disagree about whether they were handed `0.0199` or `1.99`.
 */
export function ratioAsPercent(ratio: string | null | undefined): string | null {
  if (ratio === null || ratio === undefined || ratio === "") return null;
  return percentOf(ratio, "1");
}

/** A plot coordinate. Geometry, never a figure a reader is shown — those stay decimal strings. */
function coordinate(value: string | null): number | null {
  if (value === null) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/* ------------------------------------------------------------------ *
 * The ranges the brief names
 * ------------------------------------------------------------------ */

/** Every range the brief's header asks for, including the two that have no series behind them. */
export type OfferedRange = NavRange | "1D" | "1W";

export interface RangeOption {
  readonly key: OfferedRange;
  readonly label: string;
  /** The window in words, for the control's accessible name. */
  readonly title: string;
  readonly available: boolean;
  /** Why it cannot be served. Non-null exactly when `available` is false. */
  readonly unavailable: string | null;
}

/**
 * The reason 1D and 1W are not drawn, said out loud rather than implied by a greyed pill.
 *
 * `NavRange`'s own docstring makes the same argument on the server: *"A member that cannot
 * honestly be served is worse than a missing one."* The API therefore has no 1D member at all.
 * The brief asks for the control, so the control exists and says what it cannot do — a pill that
 * is merely dim teaches a reader to click it again tomorrow.
 */
export const INTRADAY_UNAVAILABLE =
  "Baskfy records one mark per trading day, at the official close. A one-day or one-week line " +
  "needs an intraday series and nothing stores one yet, so drawing these would join a single " +
  "point to itself.";

export const OFFERED_RANGES: readonly RangeOption[] = [
  { key: "1D", label: "1D", title: "One day", available: false, unavailable: INTRADAY_UNAVAILABLE },
  { key: "1W", label: "1W", title: "One week", available: false, unavailable: INTRADAY_UNAVAILABLE },
  { key: "1M", label: "1M", title: "One month", available: true, unavailable: null },
  { key: "3M", label: "3M", title: "Three months", available: true, unavailable: null },
  { key: "1Y", label: "1Y", title: "One year", available: true, unavailable: null },
  { key: "3Y", label: "3Y", title: "Three years", available: true, unavailable: null },
  { key: "ALL", label: "All", title: "Everything recorded", available: true, unavailable: null },
];

/** A range the API will serve. The narrowed `key` is what lets a pill call `onRangeChange`
 *  without a cast — `1D` is in {@link OfferedRange} and is not in `NavRange`, and the type says so. */
export interface ServableRange extends RangeOption {
  readonly key: NavRange;
}

function isServable(option: RangeOption): option is ServableRange {
  return option.available && option.key !== "1D" && option.key !== "1W";
}

/** The ranges the API will actually serve, in the order the control shows them. */
export function servableRanges(): readonly ServableRange[] {
  return OFFERED_RANGES.filter(isServable);
}

/** The ranges the brief names that Baskfy cannot draw, each still carrying its reason. */
export function blockedRanges(): readonly RangeOption[] {
  return OFFERED_RANGES.filter((range) => !range.available);
}

/* ------------------------------------------------------------------ *
 * The series
 * ------------------------------------------------------------------ */

export const CAPITAL_DEFINITION =
  "Where the value line would sit if the market had done nothing: this window's opening value " +
  "plus every deposit and withdrawal since. It is not what your shares cost you — Baskfy stores " +
  "no day-by-day cost basis — so the gap between the two lines is market movement, with " +
  "transfers taken out.";

/** One trading day, with every series this workspace draws already joined onto it. */
export interface PerformancePoint {
  readonly on: string;
  /** End-of-day portfolio value in rupees, cash included. */
  readonly value: string;
  readonly cash: string;
  /** Money that entered (+) or left (−) the account that day. */
  readonly netFlow: string;
  /** {@link CAPITAL_DEFINITION}. Running, from the first mark of the whole series. */
  readonly capital: string;
  /** The wealth index the return view plots. `null` where the drawdown series has no row. */
  readonly wealthIndex: string | null;
  /** Below the high-water mark, as a stored **ratio** (negative). */
  readonly drawdown: string | null;
  readonly peak: string | null;
  /** The benchmark's index level on this date, `null` when the index did not print. */
  readonly benchmark: string | null;
  /** The day's move against the previous close, flows already removed by the server. */
  readonly dayPnl: string | null;
  /** The same move as a stored ratio. */
  readonly dayPnlRatio: string | null;
  readonly pendingReconciliation: boolean;
}

export interface PerformanceSeries {
  readonly points: readonly PerformancePoint[];
  readonly range: NavRange;
  readonly benchmarkName: string | null;
  /** How many marks the index printed on — fewer than `points.length` is normal and is said. */
  readonly benchmarkMarks: number;
  readonly from: string | null;
  readonly to: string | null;
  readonly totalReturn: Metric;
  readonly benchmarkReturn: Metric;
  readonly difference: Metric;
  readonly maxDrawdown: Metric;
  readonly maxDrawdownPeakOn: string | null;
  readonly maxDrawdownTroughOn: string | null;
  readonly pendingReconciliation: boolean;
}

const EMPTY_SERIES: PerformanceSeries = {
  points: [],
  range: "ALL",
  benchmarkName: null,
  benchmarkMarks: 0,
  from: null,
  to: null,
  totalReturn: metric(
    "Total return",
    "The chain-linked return over the marks drawn, with deposits and withdrawals removed.",
    null,
    "No value series has been loaded.",
  ),
  benchmarkReturn: metric(
    "Benchmark",
    "The index's return over the same stretch of days.",
    null,
    "No value series has been loaded.",
  ),
  difference: metric(
    "Against the benchmark",
    "Your return minus the index's, over the same stretch of days.",
    null,
    "No value series has been loaded.",
  ),
  maxDrawdown: metric(
    "Deepest fall",
    "The largest peak-to-trough fall recorded inside the period shown.",
    null,
    "No value series has been loaded.",
  ),
  maxDrawdownPeakOn: null,
  maxDrawdownTroughOn: null,
  pendingReconciliation: false,
};

/**
 * Join the four arrays the API sends into one row per trading day.
 *
 * `points` is the spine. `drawdown`, `daily_pnl` and `benchmark.points` are joined **by date**
 * rather than by position: they are three separate lists built by three separate passes, and
 * `daily_pnl` is deliberately one entry shorter than `points` (one per *transition* — the first
 * mark has no previous close, and `portfolio_nav.daily_pnl` refuses to emit a zero for it). Index
 * alignment would put every day's move one row out and nothing would look wrong.
 */
export function performanceSeries(chart: NavSeries | null | undefined): PerformanceSeries {
  if (chart === null || chart === undefined) return EMPTY_SERIES;

  const falls = new Map((chart.drawdown ?? []).map((point) => [point.on, point]));
  const moves = new Map((chart.daily_pnl ?? []).map((point) => [point.on, point]));
  const benchmarkPoints = chart.benchmark?.points ?? [];
  const levels = new Map(benchmarkPoints.map((point) => [point.on, point.value]));

  let running: string | null = null;
  const points: PerformancePoint[] = (chart.points ?? []).map((mark) => {
    // The opening mark seeds the capital line; every later day adds that day's flow to it. A
    // deposit therefore lifts both lines by the same rupee and moves the gap between them not at
    // all, which is the whole point of drawing it.
    const capital: string =
      running === null ? mark.value : (addDecimalStrings([running, mark.net_flow]) ?? running);
    running = capital;
    const fall = falls.get(mark.on);
    const move = moves.get(mark.on);
    return {
      on: mark.on,
      value: mark.value,
      cash: mark.cash,
      netFlow: mark.net_flow,
      capital,
      wealthIndex: fall?.index ?? null,
      drawdown: fall?.drawdown ?? null,
      peak: fall?.peak ?? null,
      benchmark: levels.get(mark.on) ?? null,
      dayPnl: move?.amount ?? null,
      dayPnlRatio: move?.pct ?? null,
      pendingReconciliation: mark.pending_reconciliation === true,
    };
  });

  const benchmarkMarks = points.filter((point) => point.benchmark !== null).length;
  const first = points[0] ?? null;
  const last = points[points.length - 1] ?? null;

  return {
    points,
    range: chart.range,
    benchmarkName: chart.benchmark?.name ?? null,
    benchmarkMarks,
    from: chart.from_on ?? first?.on ?? null,
    to: chart.to_on ?? last?.on ?? null,
    totalReturn: metric(
      chart.total_return.label,
      "The chain-linked return over the marks drawn. Deposits and withdrawals are removed day by " +
        "day, so money you moved in never reads as money the strategy made.",
      ratioAsPercent(chart.total_return.value),
      chart.total_return.unavailable_reason ?? "Needs at least two end-of-day marks.",
      { since: chart.total_return.since ?? null },
    ),
    benchmarkReturn: metric(
      chart.benchmark ? chart.benchmark.name : "Benchmark",
      "The index's own return over exactly this window, from the same first and last dates.",
      ratioAsPercent(chart.benchmark?.benchmark.value),
      chart.benchmark?.benchmark.unavailable_reason ??
        "No benchmark is set for these portfolios, so there is nothing to measure against.",
      { since: chart.benchmark?.benchmark.since ?? null },
    ),
    difference: metric(
      "Against the benchmark",
      "Your return minus the index's, over the same stretch of days. Positive means you are ahead of it.",
      ratioAsPercent(chart.benchmark?.difference),
      "Both sides of the comparison are needed and one of them is missing.",
    ),
    maxDrawdown: metric(
      "Deepest fall",
      "The largest peak-to-trough fall inside this window, measured on the value series.",
      ratioAsPercent(chart.max_drawdown?.drawdown),
      "No peak has been recorded inside this window yet.",
    ),
    maxDrawdownPeakOn: chart.max_drawdown?.peak_on ?? null,
    maxDrawdownTroughOn: chart.max_drawdown?.trough_on ?? null,
    pendingReconciliation: chart.pending_reconciliation === true,
  };
}

/* ------------------------------------------------------------------ *
 * The brush
 * ------------------------------------------------------------------ */

/** Inclusive indices into {@link PerformanceSeries.points} — what the brush has selected. */
export interface SeriesSpan {
  readonly from: number;
  readonly to: number;
}

/** The whole series. */
export function fullSpan(series: PerformanceSeries): SeriesSpan {
  return { from: 0, to: Math.max(series.points.length - 1, 0) };
}

/**
 * A span the caller asked for, forced to be legal: inside the array, in order, and never a single
 * mark — a brush that can collapse to one point is a brush that can silently empty the chart.
 */
export function clampSpan(series: PerformanceSeries, span: SeriesSpan): SeriesSpan {
  const last = series.points.length - 1;
  if (last < 0) return { from: 0, to: 0 };
  const from = Math.min(Math.max(Math.trunc(span.from), 0), last);
  const to = Math.min(Math.max(Math.trunc(span.to), 0), last);
  if (from === to) {
    return to === last ? { from: Math.max(last - 1, 0), to: last } : { from, to: from + 1 };
  }
  return from < to ? { from, to } : { from: to, to: from };
}

function sliceOf(series: PerformanceSeries, span: SeriesSpan): readonly PerformancePoint[] {
  return series.points.slice(span.from, span.to + 1);
}

/** True when the brush has been moved off the whole series — the caption says so when it has. */
export function isNarrowed(series: PerformanceSeries, span: SeriesSpan): boolean {
  const full = fullSpan(series);
  return span.from !== full.from || span.to !== full.to;
}

/* ------------------------------------------------------------------ *
 * The three views
 * ------------------------------------------------------------------ */

export type PerformanceView = "VALUE" | "RETURN" | "DRAWDOWN";

export const VIEWS: readonly { key: PerformanceView; label: string; help: string }[] = [
  {
    key: "VALUE",
    label: "Value",
    help: "What everything you hold was worth at each close, in rupees, cash included.",
  },
  {
    key: "RETURN",
    label: "Return",
    help: "The wealth index, from 0% at the left edge. Deposits move the value line and not this one.",
  },
  {
    key: "DRAWDOWN",
    label: "Falls from peak",
    help: "How far below its own high-water mark the portfolio group sat on each day.",
  },
];

/** One drawn series, carrying everything needed to label it *on the line* rather than in a key. */
export interface PlotLine {
  readonly key: "portfolio" | "capital" | "benchmark" | "drawdown";
  /** The direct label printed at the line's right-hand end. */
  readonly label: string;
  readonly definition: string;
  readonly tone: "portfolio" | "capital" | "benchmark" | "fall";
  /** The stroke pattern, and the word for it — so the lines differ without relying on colour. */
  readonly dash: string | null;
  readonly dashWord: string;
  readonly area: boolean;
  /** Plot coordinates, one per point in the span. `null` is a genuine gap, never a zero. */
  readonly values: readonly (number | null)[];
}

export interface Plot {
  readonly view: PerformanceView;
  readonly unit: "rupees" | "percent";
  readonly dates: readonly string[];
  readonly lines: readonly PlotLine[];
  readonly min: number;
  readonly max: number;
  /** Whether a zero rule belongs on the axis — it does wherever zero means break-even. */
  readonly zeroRule: boolean;
  readonly caption: string;
  /** Set when this view cannot be drawn at all, and why. Never both this and `lines`. */
  readonly unavailable: string | null;
}

const NO_WEALTH_INDEX =
  "The return view reads the wealth index, and the drawdown series that carries it has no row " +
  "for the first day of the period shown. Value is drawn from the marks themselves and is unaffected.";

function spanOf(values: ReadonlyArray<readonly (number | null)[]>): { min: number; max: number } {
  const flat = values.flat().filter((value): value is number => value !== null);
  if (flat.length === 0) return { min: 0, max: 1 };
  const min = Math.min(...flat);
  const max = Math.max(...flat);
  return min === max ? { min: min - 1, max: max + 1 } : { min, max };
}

/**
 * The active view's lines, over the brushed span.
 *
 * **Return is the wealth index, never a percentage of the value line.** `combined-chart` makes
 * the same choice and gives the reason: assigning ₹50,000 to a portfolio raises its value by
 * ₹50,000 and its return by nothing. Deriving the return view from the value line would credit
 * the user's own deposit to the strategy, which is the exact confusion `HeroOut` separates XIRR
 * from TWR to avoid. `performance.test.ts` holds it with a fixture whose deposit doubles the
 * value on a day the market did not move.
 *
 * **The benchmark is rebased to the left edge of the span**, in both units: in return mode both
 * lines start at 0%, in value mode the index is scaled to the opening value. An index level and a
 * rupee balance share no axis, and two axes let the eye compare two arbitrary scalings. Rebasing
 * is the one presentation under which "am I ahead of it?" is answered by which line is higher,
 * and the caption says so rather than leaving it implied.
 */
export function plotFor(
  series: PerformanceSeries,
  view: PerformanceView,
  span: SeriesSpan,
): Plot {
  const points = sliceOf(series, span);
  const dates = points.map((point) => point.on);
  const benchmarkName = series.benchmarkName;
  const empty = (unavailable: string): Plot => ({
    view,
    unit: view === "VALUE" ? "rupees" : "percent",
    dates,
    lines: [],
    min: 0,
    max: 1,
    zeroRule: false,
    caption: "",
    unavailable,
  });

  if (points.length === 0) return empty("There are no marks inside the period shown.");

  if (view === "DRAWDOWN") {
    const values = points.map((point) => coordinate(ratioAsPercent(point.drawdown)));
    if (values.every((value) => value === null)) {
      return empty(
        "No high-water mark has been recorded for these days, so there is no fall to measure.",
      );
    }
    const line: PlotLine = {
      key: "drawdown",
      label: "Below the peak",
      definition:
        "How far under its own highest close the portfolio group sat that day. Zero means it was at a high.",
      tone: "fall",
      dash: null,
      dashWord: "solid, shaded",
      area: true,
      values,
    };
    const bounds = spanOf([values, [0]]);
    return {
      view,
      unit: "percent",
      dates,
      lines: [line],
      min: Math.min(bounds.min, 0),
      max: 0,
      zeroRule: true,
      caption:
        "Measured on your value series only. Baskfy stores no drawdown series for the benchmark, " +
        "so there is no index line to hang beside this one.",
      unavailable: null,
    };
  }

  if (view === "RETURN") {
    const base = points[0]?.wealthIndex ?? null;
    if (base === null || isZero(base)) return empty(NO_WEALTH_INDEX);
    const portfolio = points.map((point) =>
      coordinate(percentOf(subtract(point.wealthIndex, base), base)),
    );
    const benchmarkBase = points.find((point) => point.benchmark !== null)?.benchmark ?? null;
    const benchmark =
      benchmarkBase === null || isZero(benchmarkBase)
        ? null
        : points.map((point) => coordinate(percentOf(subtract(point.benchmark, benchmarkBase), benchmarkBase)));

    const lines: PlotLine[] = [
      {
        key: "portfolio",
        label: "Your return",
        definition:
          "The wealth index, rebased to 0% at the left edge. Deposits and withdrawals are removed " +
          "day by day, so this line answers how the holdings did — not how much money you added.",
        tone: "portfolio",
        dash: null,
        dashWord: "solid",
        area: false,
        values: portfolio,
      },
    ];
    if (benchmark !== null) {
      lines.push({
        key: "benchmark",
        label: benchmarkName ?? "Benchmark",
        definition: "The index over the same days, also from 0% at the left edge.",
        tone: "benchmark",
        dash: "5 3",
        dashWord: "dashed",
        area: false,
        values: benchmark,
      });
    }
    const bounds = spanOf([...lines.map((line) => line.values), [0]]);
    return {
      view,
      unit: "percent",
      dates,
      lines,
      min: bounds.min,
      max: bounds.max,
      zeroRule: true,
      caption:
        benchmark === null
          ? "Both your money and the strategy's own record would sit on this line; zero is where you started."
          : `Both lines start at 0% on ${dates[0] ?? ""}, which is the only rebasing under which the higher line is the one that is ahead.`,
      unavailable: null,
    };
  }

  const portfolio = points.map((point) => coordinate(point.value));
  const capital = points.map((point) => coordinate(point.capital));
  const opening = points[0]?.value ?? null;
  const benchmarkBase = points.find((point) => point.benchmark !== null)?.benchmark ?? null;
  const benchmark =
    benchmarkBase === null || isZero(benchmarkBase) || opening === null
      ? null
      : points.map((point) => {
          const level = point.benchmark;
          if (level === null) return null;
          // level / base * opening, done as a percentage of the base so the division stays exact
          // to two decimals before it ever reaches a coordinate.
          const ratio = percentOf(level, benchmarkBase);
          if (ratio === null) return null;
          const scaled = Number(ratio) / 100;
          const start = Number(opening);
          return Number.isFinite(scaled) && Number.isFinite(start) ? scaled * start : null;
        });

  const lines: PlotLine[] = [
    {
      key: "portfolio",
      label: "Value",
      definition: "What everything you hold was worth at that close, cash included.",
      tone: "portfolio",
      dash: null,
      dashWord: "solid",
      area: false,
      values: portfolio,
    },
    {
      key: "capital",
      label: "Capital in play",
      definition: CAPITAL_DEFINITION,
      tone: "capital",
      dash: "2 3",
      dashWord: "dotted",
      area: false,
      values: capital,
    },
  ];
  if (benchmark !== null) {
    lines.push({
      key: "benchmark",
      label: benchmarkName ?? "Benchmark",
      definition:
        "The index, scaled so it starts at your opening value. Its shape is the comparison; its " +
        "rupees are not a balance you hold.",
      tone: "benchmark",
      dash: "5 3",
      dashWord: "dashed",
      area: false,
      values: benchmark,
    });
  }
  const bounds = spanOf(lines.map((line) => line.values));
  return {
    view,
    unit: "rupees",
    dates,
    lines,
    min: bounds.min,
    max: bounds.max,
    zeroRule: false,
    caption:
      benchmark === null
        ? "The gap between the two lines is what the market did; money you moved in or out lifts both together."
        : `The gap between value and capital in play is what the market did. ${benchmarkName ?? "The index"} is rebased to your opening value so the two shapes can be compared.`,
    unavailable: null,
  };
}

/* ------------------------------------------------------------------ *
 * The read-out under the cursor
 * ------------------------------------------------------------------ */

export interface Readout {
  /** Index into the whole series, not into the span. */
  readonly index: number;
  readonly on: string;
  readonly value: Metric;
  readonly dayChange: Metric;
  readonly cumulative: Metric;
  readonly benchmark: Metric;
  readonly relative: Metric;
  readonly capital: Metric;
  readonly drawdown: Metric;
  readonly events: readonly EventMarker[];
}

/**
 * Everything the hover panel shows for one day: the date, the value, that day's move, the return
 * from the left edge of the brush, and the index's return over the same stretch.
 *
 * Every field is a {@link Metric}, so a day the index did not print cannot render as a dash — it
 * renders the sentence saying the index has no close on that date. That case is not hypothetical:
 * `benchmarkMarks` is routinely lower than `points.length`, because a portfolio's marks and an
 * index's prints are two different calendars the moment either has a gap.
 */
export function readoutAt(
  series: PerformanceSeries,
  span: SeriesSpan,
  index: number,
): Readout | null {
  const point = series.points[index];
  const base = series.points[span.from];
  if (point === undefined || base === undefined) return null;

  const benchmarkBase =
    series.points.slice(span.from, index + 1).find((mark) => mark.benchmark !== null)?.benchmark ??
    null;
  const cumulative =
    base.wealthIndex === null || isZero(base.wealthIndex)
      ? null
      : percentOf(subtract(point.wealthIndex, base.wealthIndex), base.wealthIndex);
  const benchmarkReturn =
    point.benchmark === null || benchmarkBase === null || isZero(benchmarkBase)
      ? null
      : percentOf(subtract(point.benchmark, benchmarkBase), benchmarkBase);
  /* The label heads a cell and the sentence sits inside one, so they are not the same string:
     "the benchmark" reads wrong as a heading and "Benchmark" reads wrong mid-sentence. */
  const indexLabel = series.benchmarkName ?? "Benchmark";
  const indexName = series.benchmarkName ?? "the benchmark";

  return {
    index,
    on: point.on,
    value: metric(
      "Value",
      "Everything you hold, at that day's official close, cash included.",
      point.value,
      "This mark has no recorded value.",
    ),
    dayChange: metric(
      "That day",
      "The move against the previous close, with any money that entered or left the account on " +
        "the day already taken out.",
      point.dayPnl,
      index === span.from
        ? "This is the first day in the window, so there is no previous close inside it to compare against."
        : "No move was recorded for this day. The previous close is missing, so a change cannot be measured.",
      { pct: ratioAsPercent(point.dayPnlRatio) },
    ),
    cumulative: metric(
      "Since the left edge",
      "The wealth index from the first day shown to this one. Deposits and withdrawals are removed, " +
        "so this is what the holdings did.",
      cumulative,
      "The wealth index has no row for one of these two days, so the stretch cannot be chained.",
    ),
    benchmark: metric(
      indexLabel,
      "The index's return over exactly the same stretch of days.",
      benchmarkReturn,
      point.benchmark === null
        ? `${indexName} has no close on ${point.on}, so there is nothing to compare this day against.`
        : `No ${indexName} print inside this window to measure from yet.`,
    ),
    relative: metric(
      "Ahead or behind",
      "Your return over this stretch minus the index's over the same days.",
      subtract(cumulative, benchmarkReturn),
      "Both sides of the comparison are needed and one of them is missing for this day.",
    ),
    capital: metric(
      "Capital in play",
      CAPITAL_DEFINITION,
      point.capital,
      "No opening value to carry the flows forward from.",
    ),
    drawdown: metric(
      "Below the peak",
      "How far under its own highest close the portfolio group sat that day.",
      ratioAsPercent(point.drawdown),
      "No high-water mark has been recorded for this day.",
    ),
    events: eventMarkers(series).filter((event) => event.index === index),
  };
}

/* ------------------------------------------------------------------ *
 * Event markers
 * ------------------------------------------------------------------ */

export type EventKind = "money-in" | "money-out" | "peak" | "trough" | "unreconciled";

export interface EventMarker {
  readonly index: number;
  readonly on: string;
  readonly kind: EventKind;
  /** A word, so the marker is not a coloured dot and nothing else. */
  readonly label: string;
  readonly detail: string;
}

/**
 * The four things that actually happened, taken from fields the payload already carries.
 *
 * No annotation store exists, so nothing here is an editorial note — a marker is a flow the
 * ledger recorded, the peak and trough of the window's worst fall, or a day whose value was
 * frozen pending reconciliation. Anything else would be a caption invented in the browser.
 */
export function eventMarkers(series: PerformanceSeries): readonly EventMarker[] {
  const markers: EventMarker[] = [];
  series.points.forEach((point, index) => {
    if (!isZero(point.netFlow) && parseDecimal(point.netFlow) !== null) {
      const outward = compareDecimalStrings(point.netFlow, "0") < 0;
      markers.push({
        index,
        on: point.on,
        kind: outward ? "money-out" : "money-in",
        label: outward ? "Money out" : "Money in",
        detail: outward
          ? "Money left the account this day. It lowers the value line and the capital line together, so it is not a loss."
          : "Money entered the account this day. It lifts the value line and the capital line together, so it is not a gain.",
      });
    }
    if (point.pendingReconciliation) {
      markers.push({
        index,
        on: point.on,
        kind: "unreconciled",
        label: "Unreconciled",
        detail:
          "This day's value did not reconcile with the broker, so it is carried as the last figure we could stand behind.",
      });
    }
    if (series.maxDrawdownPeakOn === point.on) {
      markers.push({
        index,
        on: point.on,
        kind: "peak",
        label: "Peak",
        detail: "The high-water mark the window's deepest fall is measured from.",
      });
    }
    if (series.maxDrawdownTroughOn === point.on) {
      markers.push({
        index,
        on: point.on,
        kind: "trough",
        label: "Trough",
        detail: "The bottom of the window's deepest fall.",
      });
    }
  });
  return markers;
}

/* ------------------------------------------------------------------ *
 * States — an empty frame is never the answer
 * ------------------------------------------------------------------ */

export type SeriesStateKind = "no-series" | "single-mark" | "unreconciled";

export interface Explanation {
  readonly kind: SeriesStateKind | "no-benchmark" | "sparse-benchmark";
  readonly headline: string;
  readonly detail: string;
  /** Exactly one next step. A state with nothing to do says so in `detail` and carries none. */
  readonly action: { readonly label: string; readonly href: string } | null;
}

/** Why the chart cannot be drawn, or `null` when it can. */
export function seriesExplanation(series: PerformanceSeries): Explanation | null {
  if (series.points.length === 0) {
    return {
      kind: "no-series",
      headline: "No end-of-day valuations recorded yet",
      detail:
        "A value line needs at least one close from the daily valuation pass. It runs after the " +
        "market closes and stores one mark per trading day, so an account connected today has its " +
        "first mark tomorrow evening.",
      action: { label: "Check broker sync", href: "/portfolio/holdings" },
    };
  }
  if (series.points.length === 1) {
    const only = series.points[0];
    return {
      kind: "single-mark",
      headline: `Only one mark so far, for ${only?.on ?? "this window"}`,
      detail:
        "A line needs two. Drawing one would be a flat segment through a single point, which reads " +
        "as a day of no movement rather than as a day of no history. The second mark arrives at " +
        "the next close.",
      action: { label: "See what you hold", href: "/portfolio/holdings" },
    };
  }
  return null;
}

/** Why the benchmark is not on the chart, or `null` when it is. This never blocks the chart. */
export function benchmarkExplanation(series: PerformanceSeries): Explanation | null {
  if (series.points.length === 0) return null;
  if (series.benchmarkName === null) {
    return {
      kind: "no-benchmark",
      headline: "No benchmark set for these portfolios",
      detail:
        "Without an index there is nothing to draw the second line from, so the chart shows your " +
        "value alone. A benchmark is set per portfolio, and each portfolio's own screen names the " +
        "one it is measured against.",
      action: { label: "Open a portfolio to set one", href: "/portfolio/portfolios" },
    };
  }
  if (series.benchmarkMarks < 2) {
    return {
      kind: "sparse-benchmark",
      headline: `${series.benchmarkName} has ${series.benchmarkMarks === 0 ? "no closes" : "one close"} inside this window`,
      detail:
        "Two prints are the fewest a line can be drawn from. The index is stored from the day it " +
        "was first ingested, so a window that starts before that has your value and nothing to " +
        "compare it with.",
      action: { label: "Widen the window", href: "#performance-range" },
    };
  }
  return null;
}

/* ------------------------------------------------------------------ *
 * Attribution
 * ------------------------------------------------------------------ */

export interface BlockedEffect {
  readonly name: string;
  /** Why the figure does not exist. Never "coming soon" — what is actually missing. */
  readonly reason: string;
  /** What would make it real, so the list is a backlog rather than an apology. */
  readonly unblockedBy: string;
}

/**
 * The decomposition a performance attribution normally carries, and why Baskfy has none of it.
 *
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §2.2 covers four of these. **Cash drag is the one §2.2 does
 * not name and it is worth stating precisely, because it looks computable and is not:**
 * `NavPointOut` carries `cash` on every mark, so the cash *weight* is known day by day. What is
 * not known is the return the shares earned on their own, because `net_flow` records money
 * entering and leaving the *account* — a buy moves cash into shares inside the account and
 * changes no flow. Without the internal transfer the equity-only return cannot be separated from
 * the total, and a cash-drag figure is exactly that difference. It would have to be guessed.
 */
export const NOT_DECOMPOSABLE: readonly BlockedEffect[] = [
  {
    name: "Contribution by sector",
    reason:
      "Baskfy does not record which sector a company belongs to, so there is nothing to group these holdings by. The grouping helper says the same thing when it offers you no sector option.",
    unblockedBy: "A sector and industry classification for every listed company, sourced and kept current.",
  },
  {
    name: "Allocation effect",
    reason:
      "The first half of a Brinson decomposition needs the benchmark's weight in each sector and each sector's return. Baskfy stores the benchmark as one index level per day and nothing underneath it.",
    unblockedBy: "Benchmark constituents with weights, plus the sector map above.",
  },
  {
    name: "Security-selection effect",
    reason:
      "The other half of the same decomposition, and it needs the same two things plus a daily return series per holding. None of the three exists.",
    unblockedBy: "A per-holding returns store aligned on dates, and benchmark constituents.",
  },
  {
    name: "Cash drag",
    reason:
      "How much cash you held is known. What holding it cost is not, because Baskfy records money arriving in and leaving the account but not money moving between cash and shares inside it.",
    unblockedBy: "Recording internal buys and sells as transfers on the NAV series.",
  },
];

/**
 * Fees left this list on 11 Sep 2026, because its own entry said it could.
 *
 * It read *"Needs: wiring the existing cost model through the portfolio rebalance path"* — which
 * is not a missing-data problem, it is an unbuilt one, and the four entries that remain above are
 * a different kind of thing entirely. Each of those needs something that exists nowhere: a sector
 * map, benchmark constituents with weights, a per-holding return series, a record of money moving
 * between cash and shares inside the account.
 *
 * The cost model was built and calibrated against 163 real fills and reproduces every component
 * exactly. `portfolio_cash_flow` already carried the buys and sells. So the figure is now computed
 * server-side and arrives on the payload.
 *
 * **What did NOT change is the honesty.** The value series is still not net of these charges, so
 * the caveat travels with the number rather than being dropped once the number existed. That was
 * the actual complaint in the old entry's wording, and shipping a figure without it would have
 * been the regression the entry warned about.
 */
export interface EstimatedCosts {
  readonly stt: string;
  readonly exchange: string;
  readonly sebi: string;
  readonly stamp: string;
  readonly gst: string;
  /** Depository charge — flat, per scrip per selling day, not per order. */
  readonly dp: string;
  /** Genuinely zero: nothing is charged for delivery. A fact about the broker, not a gap. */
  readonly brokerage: string;
  readonly total: string;
  readonly turnover: string;
  readonly trades: number;
  readonly sell_scrip_days: number;
  readonly bps_of_turnover?: string | null;
  /** Rendered verbatim beside the figure. The server owns this sentence. */
  readonly caveat: string;
}

/** Why there is no figure, when the payload carries none. Never a ₹0, which claims too much. */
export const NO_TRADES_COSTED =
  "No buys or sells are recorded for these portfolios, so there are no charges to estimate. " +
  "Holdings that arrived by broker sync carry no trade history until a statement is imported.";

/** The six components in the order the panel lists them, largest first on a real session. */
export const COST_COMPONENTS: ReadonlyArray<{
  readonly key: keyof Pick<
    EstimatedCosts,
    "stt" | "stamp" | "exchange" | "dp" | "gst" | "sebi" | "brokerage"
  >;
  readonly label: string;
  readonly note?: string;
}> = [
  { key: "stt", label: "Securities transaction tax" },
  { key: "stamp", label: "Stamp duty", note: "Buy side only." },
  { key: "exchange", label: "Exchange transaction charge" },
  { key: "dp", label: "Depository charge", note: "Per holding per selling day, not per sale." },
  { key: "gst", label: "GST" },
  { key: "sebi", label: "SEBI turnover fee" },
  { key: "brokerage", label: "Brokerage", note: "Nothing is charged for delivery." },
];

/**
 * Contribution by holding is **not** in the list above, and was until 11 Sep 2026.
 *
 * It sat there with `unblockedBy: "Nothing. Open a portfolio; the figure is already there."` —
 * which is the sentence that gives the game away. A list headed *"what this cannot be broken down
 * by"* is a list of things that do not exist; this one exists, is computed, and is rendered one
 * click away on every portfolio's own screen (`DetailHoldingOut.todays_contribution`). Filing an
 * available figure under "Not available" teaches a reader to stop believing the other five, which
 * ARE real blockers.
 *
 * So it is a destination, not a caveat.
 */
export const CONTRIBUTION_BY_HOLDING_IS_ELSEWHERE =
  "Contribution by holding is measured per portfolio. Open one to see which of its holdings moved it.";

export interface ContributionRow {
  readonly portfolioId: number;
  readonly name: string;
  readonly value: string | null;
  /** Share of the book by value, for the column beside the contribution. */
  readonly weightPct: string | null;
  /** The contribution itself, or the reason there is none. */
  readonly amount: Metric;
  /**
   * Share of the **gross** movement — every contribution's absolute size added together.
   *
   * Not a share of the net move: on a day one portfolio gained ₹10,000 and another lost ₹9,000,
   * the net is ₹1,000 and a "share of the move" would read 1,000% and −900%. Gross is the only
   * denominator under which the bars are comparable on every day, and the label says gross.
   */
  readonly sharePct: string | null;
  /** Set when the portfolio's series starts inside the window rather than at its left edge. */
  readonly partialFrom: string | null;
}

export interface Reconciliation {
  /** The rows added up, exactly. */
  readonly explained: string | null;
  /** The figure they are meant to add up to. */
  readonly reported: string | null;
  readonly residual: string | null;
  readonly reconciles: boolean;
  /** Always a sentence — including when it does reconcile, because that is worth saying. */
  readonly note: string;
}

export interface AttributionBand {
  readonly title: string;
  readonly definition: string;
  readonly rows: readonly ContributionRow[];
  /** Every contribution's absolute size, added — the denominator of `sharePct`. */
  readonly gross: string | null;
  /** True when gains and losses offset inside this band, which changes how it should be read. */
  readonly mixedSigns: boolean;
  readonly reconciliation: Reconciliation;
  /** Set when the whole band cannot be computed, and why. */
  readonly unavailable: string | null;
}

export interface Attribution {
  readonly today: AttributionBand;
  readonly period: AttributionBand;
  readonly blocked: readonly BlockedEffect[];
}

/** What the parent must supply for the period band. Omit it and the band says what it needs. */
export interface PeriodInput {
  /** The window in words, e.g. "11 Feb 2026 to 11 Sep 2026". */
  readonly label: string;
  readonly aggregate: PerformanceSeries;
  readonly span: SeriesSpan;
  /** `GET /api/v1/portfolio/{id}/nav`, one per capital portfolio, keyed by `portfolio_id`. */
  readonly byPortfolio: ReadonlyMap<number, PerformanceSeries>;
}

export interface AttributionInput {
  /** Capital portfolios only — a monitoring view overlaps and enters no total. */
  readonly rows: readonly PortfolioRow[];
  /** `hero.todays_pnl.amount`: the aggregate figure the rows must reconcile to. */
  readonly todaysTotal: string | null;
  readonly todaysUnavailable: string | null;
  /** Value of holdings filed into no portfolio — the usual explanation for a residual. */
  readonly unallocatedValue: string | null;
  readonly period: PeriodInput | null;
}

/**
 * Why the period band cannot split a window's profit between portfolios.
 *
 * FOR THE ENGINEER, so the sentence below does not have to carry it: this screen loads the
 * consolidated series only. `GET /api/v1/portfolio/{id}/nav` serves one per portfolio over the
 * same range, and the parent passes them as `seriesByPortfolio`; with that prop the band is exact.
 * Without it the figure would have to be apportioned by weight, which assumes every portfolio
 * returned the same thing — the one assumption that makes attribution meaningless.
 *
 * FOR THE READER: the sentence says what is missing and what would fix it, in their terms. It
 * used to name the route, and a retail investor meeting an API path on a portfolio screen learns
 * nothing except that the product is talking to itself.
 */
export const PERIOD_UNAVAILABLE =
  "Splitting this window's profit between portfolios needs each portfolio's own value history, " +
  "and this screen loads only the combined one. Apportioning it by size instead would assume " +
  "every portfolio returned the same thing.";

/**
 * A window's profit for one series: the change in value with every flow inside the window removed.
 *
 * `value[to] − value[from] − Σ net_flow` over the days *after* the first. The first mark's own
 * flow is excluded because a mark's value is its **closing** value, so a deposit made that day is
 * already inside it; subtracting it again would report a loss the size of the deposit. This is
 * the same rule `portfolio_nav.daily_pnl` applies one day at a time, and the two agree by
 * construction — chaining the day moves gives this number.
 */
export function spanPnl(series: PerformanceSeries, span: SeriesSpan): string | null {
  const points = sliceOf(series, span);
  if (points.length < 2) return null;
  const first = points[0];
  const last = points[points.length - 1];
  if (first === undefined || last === undefined) return null;
  const flows = addDecimalStrings(points.slice(1).map((point) => point.netFlow)) ?? "0";
  return subtract(subtract(last.value, first.value), flows);
}

interface DatedPnl {
  readonly amount: string | null;
  readonly partialFrom: string | null;
  readonly reason: string | null;
}

/**
 * The same figure for a portfolio whose series may not cover the whole period.
 *
 * A portfolio created in June has no marks in February, and the honest answer is its profit from
 * the day it actually starts — named, so the row is not read as a full-window figure. Silently
 * measuring from its own first mark without saying so is how a three-month-old portfolio comes to
 * look like the year's best performer.
 */
function pnlBetween(series: PerformanceSeries, fromOn: string, toOn: string): DatedPnl {
  const inside = series.points.filter((point) => point.on >= fromOn && point.on <= toOn);
  if (inside.length < 2) {
    return {
      amount: null,
      partialFrom: null,
      reason:
        inside.length === 0
          ? "This portfolio has no valuations inside the period shown."
          : "This portfolio has one valuation inside the window, and a change needs two.",
    };
  }
  const first = inside[0];
  const last = inside[inside.length - 1];
  if (first === undefined || last === undefined) {
    return { amount: null, partialFrom: null, reason: "This portfolio has no valuations inside the period shown." };
  }
  const flows = addDecimalStrings(inside.slice(1).map((point) => point.netFlow)) ?? "0";
  return {
    amount: subtract(subtract(last.value, first.value), flows),
    partialFrom: first.on === fromOn ? null : first.on,
    reason: null,
  };
}

function buildBand(
  title: string,
  definition: string,
  entries: ReadonlyArray<{
    readonly row: PortfolioRow;
    readonly amount: string | null;
    readonly reason: string;
    readonly partialFrom: string | null;
  }>,
  totalValue: string | null,
  reported: string | null,
  residualNote: (residual: string | null) => string,
  unavailable: string | null,
): AttributionBand {
  const amounts = entries.map((entry) => entry.amount).filter((value): value is string => value !== null);
  const gross = amounts.length > 0 ? addDecimalStrings(amounts.map((value) => absolute(value))) : null;
  const explained = amounts.length > 0 ? addDecimalStrings(amounts) : null;
  const residual = subtract(reported, explained);
  const positive = amounts.some((value) => compareDecimalStrings(value, "0") > 0);
  const negative = amounts.some((value) => compareDecimalStrings(value, "0") < 0);

  const rows: ContributionRow[] = entries.map((entry) => ({
    portfolioId: entry.row.portfolio_id,
    name: entry.row.name,
    value: entry.row.value ?? null,
    weightPct: percentOf(entry.row.value ?? null, totalValue),
    amount: metric(entry.row.name, definition, entry.amount, entry.reason),
    sharePct: entry.amount === null || gross === null || isZero(gross)
      ? null
      : percentOf(absolute(entry.amount), gross),
    partialFrom: entry.partialFrom,
  }));

  // Sorted by how much each moved, largest first, regardless of direction — the question is
  // "what moved the number", and a big loss answers it as well as a big gain. Rows with no
  // figure go last rather than being interleaved at zero.
  const ordered = [...rows].sort((left, right) => {
    const a = absolute(left.amount.value);
    const b = absolute(right.amount.value);
    if (a === null && b === null) return left.name.localeCompare(right.name);
    if (a === null) return 1;
    if (b === null) return -1;
    const byMove = compareDecimalStrings(b, a);
    return byMove !== 0 ? byMove : left.name.localeCompare(right.name);
  });

  return {
    title,
    definition,
    rows: ordered,
    gross,
    mixedSigns: positive && negative,
    reconciliation: {
      explained,
      reported,
      residual,
      reconciles: residual !== null && isZero(residual),
      note: residualNote(residual),
    },
    unavailable,
  };
}

/**
 * Which portfolio moved the number — the one attribution Baskfy can actually make.
 *
 * Two bands, both built from figures the API sends rather than apportioned from a total:
 *
 * * **Today** reads `PortfolioRowOut.todays_pnl.amount` per row, which the server computes from
 *   the two closes over exactly that portfolio's positions. They are quantised per portfolio
 *   before being summed on the server too (`portfolio_nav.consolidated_pnl`), so the rows add to
 *   the aggregate exactly, and a residual means something real — usually holdings filed into no
 *   portfolio, which count towards net worth and belong to no strategy.
 * * **The period** needs each portfolio's own NAV series and says so when it does not have one.
 *   Deriving it from weight × the aggregate would assume every portfolio returned the same
 *   thing, which is the assumption the panel exists to test.
 *
 * The residual is never hidden and never folded into the largest row. A reconciliation that does
 * not reconcile is a fact about the book, and "these add to ₹X; the book moved ₹Y" with the gap
 * named is the only version of this panel that can be checked.
 */
export function attribution(input: AttributionInput): Attribution {
  const capital = input.rows.filter((row) => row.counts_toward_total);
  const totalValue = addDecimalStrings([
    ...capital.map((row) => row.value ?? null),
    input.unallocatedValue,
  ]);
  const unallocatedHeld =
    input.unallocatedValue !== null && !isZero(input.unallocatedValue);

  const today = buildBand(
    "What moved the total today",
    "This portfolio's share of the change since the previous close, from its own holdings' two closes.",
    capital.map((row) => ({
      row,
      amount: row.todays_pnl?.amount ?? null,
      reason:
        row.todays_pnl?.unavailable_reason ??
        "No previous close for this portfolio's holdings, so today's move cannot be measured.",
      partialFrom: null,
    })),
    totalValue,
    input.todaysTotal,
    (residual) => {
      if (input.todaysTotal === null) {
        return (
          input.todaysUnavailable ??
          "The consolidated figure for today is not available, so there is nothing to reconcile these against."
        );
      }
      if (residual === null) return "No portfolio reported a move today, so there is nothing to add up.";
      if (isZero(residual)) {
        return "These add up to the consolidated figure for today, to the last paisa.";
      }
      return unallocatedHeld
        ? "The difference is holdings filed into no portfolio. They count towards net worth and belong to no strategy, so nothing here can say how they did."
        : "The difference is holdings this screen cannot attribute to a portfolio — usually a position whose previous close is missing.";
    },
    capital.length === 0
      ? "There are no capital portfolios yet, so there is nothing to attribute the day's move to."
      : null,
  );

  const period = input.period;
  const periodBand = buildBand(
    period === null ? "Over the window shown" : `Over ${period.label}`,
    "This portfolio's profit over the window: the change in its value with every deposit and withdrawal removed.",
    period === null
      ? capital.map((row) => ({
          row,
          amount: null,
          reason: PERIOD_UNAVAILABLE,
          partialFrom: null,
        }))
      : capital.map((row) => {
          const series = period.byPortfolio.get(row.portfolio_id);
          const from = period.aggregate.points[period.span.from]?.on ?? null;
          const to = period.aggregate.points[period.span.to]?.on ?? null;
          if (series === undefined || from === null || to === null) {
            return { row, amount: null, reason: PERIOD_UNAVAILABLE, partialFrom: null };
          }
          const result = pnlBetween(series, from, to);
          return {
            row,
            amount: result.amount,
            reason: result.reason ?? PERIOD_UNAVAILABLE,
            partialFrom: result.partialFrom,
          };
        }),
    totalValue,
    period === null ? null : spanPnl(period.aggregate, period.span),
    (residual) => {
      if (period === null) return PERIOD_UNAVAILABLE;
      if (residual === null) {
        return "No portfolio has a value series covering this window, so there is nothing to add up.";
      }
      if (isZero(residual)) {
        return "These add up to the consolidated profit over the period shown, to the last paisa.";
      }
      return unallocatedHeld
        ? "The difference belongs to holdings filed into no portfolio, and to any portfolio whose series does not cover the whole period."
        : "The difference belongs to portfolios whose series does not cover the whole window; each one that does not is named in its row.";
    },
    period === null ? PERIOD_UNAVAILABLE : capital.length === 0
      ? "There are no capital portfolios yet, so there is nothing to attribute the window's profit to."
      : null,
  );

  return { today, period: periodBand, blocked: NOT_DECOMPOSABLE };
}
