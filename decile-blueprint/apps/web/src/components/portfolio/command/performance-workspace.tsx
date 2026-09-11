"use client";

import { curveMonotoneX } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { AreaClosed, LinePath } from "@visx/shape";
import { CircleAlert, Info, MoveHorizontal } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { Attribution } from "@/components/portfolio/command/attribution";
import { MetricCell } from "@/components/portfolio/command/metric-band";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatNumber, formatTradeDate } from "@/lib/format";
import type { Metric } from "@/lib/portfolio/command-center";
import type { NavRange, NavSeries, PortfolioRow } from "@/lib/portfolio/overview";
import {
  attribution,
  benchmarkExplanation,
  blockedRanges,
  clampSpan,
  eventMarkers,
  fullSpan,
  isNarrowed,
  performanceSeries,
  plotFor,
  readoutAt,
  seriesExplanation,
  servableRanges,
  VIEWS,
  type Explanation,
  type PerformanceSeries,
  type Plot as PlotModel,
  type PerformanceView,
  type PlotLine,
  type SeriesSpan,
} from "@/lib/portfolio/performance";
import { cn } from "@/lib/utils";

/**
 * PC2 — the performance workspace: the chart, what it was worth on any day, and what moved it.
 *
 * Brief: *"an interactive portfolio value chart ... value over time, invested capital, benchmark,
 * drawdown overlay, event markers, a value/return/drawdown toggle, a hover read-out, brush to
 * change the period, and direct labels rather than a large legend"*, with performance attribution
 * beneath it. `docs/PORTFOLIO-COMMAND-CENTER.md` §6.1 gives this leaf this file, `attribution.tsx`
 * and `lib/portfolio/performance.ts`; the parent mounts it.
 *
 * ## WHY THIS IS NOT `CombinedChart`
 *
 * `components/portfolio/combined-chart.tsx` already draws a value/return toggle, a rebased
 * benchmark and a drawdown panel over the same `NavSeriesOut`, and the reasoning in its docstring
 * is right and is inherited here rather than re-argued — particularly that the return view is the
 * **wealth index** and never a percentage of the value line. What it has no notion of is a cursor,
 * a brush, a capital line, an event marker or a direct label, and four of the brief's seven
 * requirements are exactly those. It is also the chart on the *existing* portfolio screen, which
 * is still mounted; changing it would change that screen too. So the primitives are shared — the
 * same locked visx stack, `@visx/curve`, `@visx/scale`, `@visx/shape`, nothing new installed —
 * and the component is not. `docs/pc-findings/pc2.md` lists what `CombinedChart` would need to
 * become the single shared chart, for whoever consolidates them.
 *
 * ## THE READ-OUT IS A CONTROL, NOT A HOVER
 *
 * A panel that only appears under a mouse pointer does not exist on a phone and does not exist
 * for a keyboard. The cursor here is state, and three things move it: the pointer over the plot,
 * the arrow keys on a real range input, and the brush when it moves the window. The hover is one
 * of the three inputs rather than the mechanism, which is also what makes it testable off a
 * browser.
 *
 * ## NEVER A BARE DASH, NEVER COLOUR ALONE
 *
 * Every figure in the read-out goes through `Metric` and renders through PC1's `MetricCell`, so a
 * day the index did not print shows the sentence saying so and not a dash. Every line carries a
 * direct label at its right-hand end **and** a named stroke pattern — solid, dotted, dashed — so
 * three lines remain three lines in greyscale.
 */

/* Geometry. The right margin is wide because the labels live on the lines, not in a legend. */
const WIDTH = 960;
const HEIGHT = 300;
const MARGIN = { top: 14, right: 104, bottom: 30, left: 68 };
const OVERLAY_HEIGHT = 116;
const MIN_MARKS = 2;

const STROKE: Record<PlotLine["tone"], string> = {
  portfolio: "stroke-accent",
  capital: "stroke-muted-foreground",
  benchmark: "stroke-info",
  fall: "stroke-negative",
};

const SWATCH: Record<PlotLine["tone"], string> = {
  portfolio: "bg-accent",
  capital: "bg-muted-foreground",
  benchmark: "bg-info",
  fall: "bg-negative",
};

const LABEL_FILL: Record<PlotLine["tone"], string> = {
  portfolio: "fill-accent",
  capital: "fill-muted-foreground",
  benchmark: "fill-info",
  fall: "fill-negative",
};

/** Event markers get a glyph as well as a tone — a coloured tick alone says nothing. */
const EVENT_GLYPH = {
  "money-in": "↓",
  "money-out": "↑",
  peak: "▲",
  trough: "▼",
  unreconciled: "!",
} as const;

export interface PerformanceWorkspaceProps {
  /** `OverviewOut.chart` — the consolidated NAV series. `null` when the overview did not load. */
  chart: NavSeries | null;
  /** `OverviewOut.portfolios` — capital portfolios only. Monitoring views enter no total. */
  rows: readonly PortfolioRow[];
  /** `hero.todays_pnl.amount`: the figure the attribution rows must reconcile to. */
  todaysTotal?: string | null | undefined;
  /** `hero.todays_pnl.unavailable_reason`, so a missing total explains itself. */
  todaysUnavailable?: string | null | undefined;
  /** `unallocated.holdings_value` — the usual explanation for a residual. */
  unallocatedValue?: string | null | undefined;
  /**
   * `GET /api/v1/portfolio/{id}/nav` per capital portfolio, over the same range, keyed by
   * `portfolio_id`. Optional: without it the period band says exactly what it needs and shows
   * no figure, rather than apportioning the window's profit by weight.
   */
  seriesByPortfolio?: ReadonlyMap<number, NavSeries> | undefined;
  /** Ask the parent for a different range. Omitted → the pills say the page serves one window. */
  onRangeChange?: ((range: NavRange) => void) | undefined;
  /** True while a new range is in flight; the drawn series is still the previous one. */
  loading?: boolean | undefined;
}

export function PerformanceWorkspace({
  chart,
  rows,
  todaysTotal = null,
  todaysUnavailable = null,
  unallocatedValue = null,
  seriesByPortfolio,
  onRangeChange,
  loading = false,
}: PerformanceWorkspaceProps) {
  const series = useMemo(() => performanceSeries(chart), [chart]);
  const [view, setView] = useState<PerformanceView>("VALUE");
  const [showFalls, setShowFalls] = useState(false);
  /* `null` means "the brush has not been touched", so a refetch that replaces the series does not
     leave the window pinned to indices from a range that no longer exists. */
  const [brush, setBrush] = useState<SeriesSpan | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);

  const span = useMemo(
    () => (brush === null ? fullSpan(series) : clampSpan(series, brush)),
    [brush, series],
  );
  const plot = useMemo(() => plotFor(series, view, span), [series, view, span]);
  const falls = useMemo(() => plotFor(series, "DRAWDOWN", span), [series, span]);
  const blocked = seriesExplanation(series);
  const benchmarkNote = benchmarkExplanation(series);

  const cursorIndex = Math.min(Math.max(cursor ?? span.to, span.from), span.to);
  const readout = useMemo(
    () => (blocked === null ? readoutAt(series, span, cursorIndex) : null),
    [blocked, series, span, cursorIndex],
  );

  const periodSeries = useMemo(() => {
    const built = new Map<number, PerformanceSeries>();
    if (seriesByPortfolio === undefined) return built;
    for (const [id, value] of seriesByPortfolio) built.set(id, performanceSeries(value));
    return built;
  }, [seriesByPortfolio]);

  const from = series.points[span.from]?.on ?? null;
  const to = series.points[span.to]?.on ?? null;

  const bands = useMemo(
    () =>
      attribution({
        rows,
        todaysTotal,
        todaysUnavailable,
        unallocatedValue,
        period:
          seriesByPortfolio === undefined || series.points.length < MIN_MARKS
            ? null
            : {
                label: `${formatTradeDate(from)} to ${formatTradeDate(to)}`,
                aggregate: series,
                span,
                byPortfolio: periodSeries,
              },
      }),
    [
      rows,
      todaysTotal,
      todaysUnavailable,
      unallocatedValue,
      seriesByPortfolio,
      series,
      span,
      periodSeries,
      from,
      to,
    ],
  );

  return (
    <div className="space-y-4" data-testid="performance-workspace">
      <section
        aria-label="Performance over time"
        data-testid="performance-chart-panel"
        className="overflow-hidden rounded-xl border border-border bg-card"
      >
        <Controls
          range={series.range}
          onRangeChange={onRangeChange}
          loading={loading}
          view={view}
          onView={setView}
          showFalls={showFalls}
          onShowFalls={setShowFalls}
          drawable={blocked === null}
        />

        <HeadlineStrip series={series} />

        {blocked !== null ? (
          <ExplainPanel explanation={blocked} testId="performance-empty" />
        ) : (
          <>
            <Plot
              plot={plot}
              series={series}
              span={span}
              cursorIndex={cursorIndex}
              onCursor={setCursor}
            />
            {showFalls && plot.view !== "DRAWDOWN" ? (
              <Overlay plot={falls} cursorIndex={cursorIndex} span={span} />
            ) : null}
            <Brush
              series={series}
              span={span}
              cursorIndex={cursorIndex}
              onBrush={setBrush}
              onCursor={setCursor}
              onReset={() => setBrush(null)}
            />
            {readout === null ? null : (
              <ReadoutPanel
                on={readout.on}
                cells={[
                  /* Units are declared, never inferred from a label. `dayChange` is a rupee move
                     that happens to carry a percentage, and a heuristic that read its label got
                     that wrong by a factor of a hundred before this list existed. */
                  { metric: readout.value, unit: "rupees" },
                  { metric: readout.dayChange, unit: "rupees" },
                  { metric: readout.cumulative, unit: "percent" },
                  { metric: readout.benchmark, unit: "percent" },
                  { metric: readout.relative, unit: "percent" },
                  { metric: readout.capital, unit: "rupees" },
                  { metric: readout.drawdown, unit: "percent" },
                ]}
                events={readout.events.map((event) => ({
                  key: `${event.kind}-${event.on}`,
                  glyph: EVENT_GLYPH[event.kind],
                  label: event.label,
                  detail: event.detail,
                }))}
              />
            )}
          </>
        )}

        {benchmarkNote !== null ? (
          <ExplainPanel explanation={benchmarkNote} testId="performance-benchmark-state" subtle />
        ) : null}
      </section>

      <Attribution attribution={bands} />
    </div>
  );
}

/* ------------------------------------------------------------------ controls */

function Controls({
  range,
  onRangeChange,
  loading,
  view,
  onView,
  showFalls,
  onShowFalls,
  drawable,
}: {
  range: NavRange;
  onRangeChange: ((range: NavRange) => void) | undefined;
  loading: boolean;
  view: PerformanceView;
  onView: (view: PerformanceView) => void;
  showFalls: boolean;
  onShowFalls: (value: boolean) => void;
  drawable: boolean;
}) {
  const fixed = onRangeChange === undefined;
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold">How your portfolios have done</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          One mark per trading day, at the official close. Hover the chart, or use the read-out
          slider, to read any single day.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div role="group" aria-label="Chart range" id="performance-range" className="flex gap-1">
          {servableRanges().map((option) => (
            <button
              key={option.key}
              type="button"
              aria-pressed={range === option.key}
              aria-label={option.title}
              disabled={fixed || loading}
              onClick={() => onRangeChange?.(option.key)}
              data-testid={`performance-range-${option.key}`}
              className={cn(
                "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors duration-150",
                range === option.key
                  ? "border-accent bg-accent-muted text-accent"
                  : "border-border/70 bg-background text-muted-foreground hover:text-foreground",
                (fixed || loading) && "cursor-not-allowed opacity-60",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>

        <div role="group" aria-label="Chart units" className="flex gap-1">
          {VIEWS.map((option) => (
            <button
              key={option.key}
              type="button"
              aria-pressed={view === option.key}
              onClick={() => onView(option.key)}
              disabled={!drawable}
              title={option.help}
              data-testid={`performance-view-${option.key}`}
              className={cn(
                "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors duration-150",
                view === option.key
                  ? "border-accent bg-accent-muted text-accent"
                  : "border-border/70 bg-background text-muted-foreground hover:text-foreground",
                !drawable && "cursor-not-allowed opacity-60",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>

        {view === "DRAWDOWN" ? null : (
          <label className="inline-flex cursor-pointer items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={showFalls}
              disabled={!drawable}
              onChange={(event) => onShowFalls(event.target.checked)}
              data-testid="performance-falls-toggle"
              className="size-3.5 accent-[var(--accent)]"
            />
            <span>Falls from the peak beneath</span>
          </label>
        )}
      </div>

      {/* The brief asks for 1D…All. Two of them have no series behind them, so they are named
          here with the reason rather than drawn as pills that do nothing when clicked. */}
      <p
        data-testid="performance-range-unavailable"
        className="flex w-full items-start gap-1.5 text-xs leading-snug text-muted-foreground"
      >
        <CircleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warning" />
        <span>
          <strong className="font-medium text-foreground">
            {blockedRanges()
              .map((option) => option.label)
              .join(" and ")}{" "}
            are not available.
          </strong>{" "}
          {blockedRanges()[0]?.unavailable}
          {fixed
            ? " This screen was also served a single window, so the ranges above cannot be changed from here."
            : ""}
        </span>
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ headline figures */

function HeadlineStrip({ series }: { series: PerformanceSeries }) {
  return (
    <div
      data-testid="performance-headline"
      className="flex flex-wrap divide-x divide-border border-b border-border"
    >
      <div className="min-w-[11rem] flex-1">
        <MetricCell metric={series.totalReturn} percent signed />
      </div>
      <div className="min-w-[11rem] flex-1">
        <MetricCell metric={series.benchmarkReturn} percent signed />
      </div>
      <div className="min-w-[11rem] flex-1">
        <MetricCell metric={series.difference} percent signed />
      </div>
      <div className="min-w-[11rem] flex-1">
        <MetricCell
          metric={
            series.maxDrawdownPeakOn === null
              ? series.maxDrawdown
              : {
                  ...series.maxDrawdown,
                  since: `${formatTradeDate(series.maxDrawdownPeakOn)} to ${formatTradeDate(series.maxDrawdownTroughOn)}`,
                }
          }
          percent
          signed
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ the plot */

interface PlotProps {
  plot: PlotModel;
  series: PerformanceSeries;
  span: SeriesSpan;
  cursorIndex: number;
  onCursor: (index: number) => void;
}

function Plot({ plot, series, span, cursorIndex, onCursor }: PlotProps) {
  const titleId = useId();
  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom;
  const count = plot.dates.length;

  const xScale = scaleLinear<number>({
    domain: [0, Math.max(count - 1, 1)],
    range: [0, innerWidth],
  });
  const yScale = scaleLinear<number>({
    domain: [plot.min, plot.max],
    range: [innerHeight, 0],
    nice: true,
  });
  const ticks = yScale.ticks(4);
  const marks = useMemo(
    () => eventMarkers(series).filter((event) => event.index >= span.from && event.index <= span.to),
    [series, span],
  );

  if (plot.unavailable !== null) {
    return (
      <p
        data-testid="performance-view-unavailable"
        className="m-4 flex items-start gap-2 rounded-lg border border-dashed border-border px-4 py-6 text-sm text-muted-foreground"
      >
        <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
        <span>
          <strong className="font-medium text-foreground">This view cannot be drawn.</strong>{" "}
          {plot.unavailable}
        </span>
      </p>
    );
  }

  const axisLabel = (tick: number): string =>
    plot.unit === "rupees"
      ? `₹${formatNumber(tick, { decimals: 0 })}`
      : `${formatNumber(tick, { decimals: 0, signed: true })}%`;

  /* The pointer is one of three ways to move the cursor, so it only has to resolve a clientX to
     an index. `getBoundingClientRect` is the honest source for that: the SVG scales with its
     container, so the viewBox co-ordinate has to come back through the rendered width. */
  const pointerToIndex = (clientX: number, element: SVGSVGElement): number => {
    const box = element.getBoundingClientRect();
    if (box.width === 0) return cursorIndex;
    const viewX = ((clientX - box.left) / box.width) * WIDTH - MARGIN.left;
    const raw = Math.round(xScale.invert(viewX));
    return Math.min(Math.max(raw, 0), Math.max(count - 1, 0)) + span.from;
  };

  return (
    <figure className="px-4 pt-3">
      <figcaption className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {/* Direct labels sit on the lines; this row exists so each line's stroke pattern is named
            in words as well — three greys in a screenshot are still three named lines. */}
        {plot.lines.map((line) => (
          <Tooltip key={line.key}>
            <TooltipTrigger asChild>
              <span
                data-testid={`performance-line-${line.key}`}
                className="flex cursor-help items-center gap-1.5"
              >
                <span
                  aria-hidden="true"
                  className={cn("inline-block h-0.5 w-4", SWATCH[line.tone])}
                  style={line.dash === null ? undefined : { opacity: 0.75 }}
                />
                <span className="font-medium text-foreground">{line.label}</span>
                <span>({line.dashWord})</span>
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs text-xs leading-relaxed">
              {line.definition}
            </TooltipContent>
          </Tooltip>
        ))}
      </figcaption>

      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-labelledby={titleId}
        data-testid="performance-plot"
        className="w-full touch-none"
        onPointerMove={(event) => onCursor(pointerToIndex(event.clientX, event.currentTarget))}
      >
        <title id={titleId}>
          {`${plot.view === "VALUE" ? "Value" : plot.view === "RETURN" ? "Return" : "Falls from the peak"} from ${formatTradeDate(plot.dates[0])} to ${formatTradeDate(plot.dates[count - 1])}, ${count} trading days.`}
        </title>
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {ticks.map((tick) => (
            <g key={tick} transform={`translate(0,${yScale(tick)})`}>
              <line
                x1={0}
                x2={innerWidth}
                className={tick === 0 && plot.zeroRule ? "stroke-muted-foreground" : "stroke-border"}
                strokeWidth={tick === 0 && plot.zeroRule ? 0.9 : 0.5}
              />
              <text
                x={-8}
                dy="0.32em"
                textAnchor="end"
                className="fill-muted-foreground text-[10px] tabular-nums"
              >
                {axisLabel(tick)}
              </text>
            </g>
          ))}

          {plot.lines.map((line) => {
            const data = line.values
              .map((value, index) => ({ index, value }))
              .filter((entry): entry is { index: number; value: number } => entry.value !== null);
            if (data.length < MIN_MARKS) return null;
            return line.area ? (
              <AreaClosed<{ index: number; value: number }>
                key={line.key}
                data={data}
                x={(entry) => xScale(entry.index)}
                y={(entry) => yScale(entry.value)}
                yScale={yScale}
                curve={curveMonotoneX}
                className={cn("fill-negative/25", STROKE[line.tone])}
                strokeWidth={1}
              />
            ) : (
              <LinePath<{ index: number; value: number }>
                key={line.key}
                data={data}
                x={(entry) => xScale(entry.index)}
                y={(entry) => yScale(entry.value)}
                curve={curveMonotoneX}
                className={STROKE[line.tone]}
                strokeWidth={line.key === "portfolio" ? 1.75 : 1.25}
                strokeDasharray={line.dash ?? undefined}
                fill="none"
              />
            );
          })}

          {/* The brief's "direct labels rather than a large legend": each line is named where it
              ends, so the eye never has to match a colour against a key in a corner. */}
          {plot.lines.map((line) => {
            const lastIndex = line.values.reduce<number>(
              (found, value, index) => (value === null ? found : index),
              -1,
            );
            const last = lastIndex < 0 ? null : (line.values[lastIndex] ?? null);
            if (last === null) return null;
            return (
              <text
                key={line.key}
                x={xScale(lastIndex) + 6}
                y={yScale(last)}
                dy="0.32em"
                data-testid={`performance-direct-label-${line.key}`}
                className={cn("text-[10px] font-medium", LABEL_FILL[line.tone])}
              >
                {line.label}
              </text>
            );
          })}

          {/* Event markers: a glyph on the baseline, each with its own sentence. Never a bare dot. */}
          {marks.map((event) => (
            <text
              key={`${event.kind}-${event.on}`}
              x={xScale(event.index - span.from)}
              y={innerHeight - 2}
              textAnchor="middle"
              data-testid={`performance-event-${event.kind}`}
              className={cn(
                "text-[9px] font-semibold",
                event.kind === "unreconciled" ? "fill-warning" : "fill-muted-foreground",
              )}
            >
              <title>{`${event.label} — ${formatTradeDate(event.on)}. ${event.detail}`}</title>
              {EVENT_GLYPH[event.kind]}
            </text>
          ))}

          <line
            x1={xScale(cursorIndex - span.from)}
            x2={xScale(cursorIndex - span.from)}
            y1={0}
            y2={innerHeight}
            className="stroke-foreground/40"
            strokeWidth={0.75}
            strokeDasharray="2 2"
          />

          <text x={0} y={innerHeight + 18} className="fill-muted-foreground text-[10px]">
            {formatTradeDate(plot.dates[0])}
          </text>
          <text
            x={innerWidth}
            y={innerHeight + 18}
            textAnchor="end"
            className="fill-muted-foreground text-[10px]"
          >
            {formatTradeDate(plot.dates[count - 1])}
          </text>
        </g>
      </svg>

      <p className="mt-1 text-xs leading-snug text-muted-foreground">{plot.caption}</p>
    </figure>
  );
}

/* ------------------------------------------------------------------ drawdown overlay */

function Overlay({
  plot,
  cursorIndex,
  span,
}: {
  plot: PlotModel;
  cursorIndex: number;
  span: SeriesSpan;
}) {
  const titleId = useId();
  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = OVERLAY_HEIGHT - MARGIN.top - MARGIN.bottom;
  const line = plot.lines[0];

  if (plot.unavailable !== null || line === undefined) {
    return (
      <p data-testid="performance-overlay-unavailable" className="px-4 pb-2 text-xs text-muted-foreground">
        {plot.unavailable ?? "No falls-from-peak series for these days."}
      </p>
    );
  }

  const data = line.values
    .map((value, index) => ({ index, value }))
    .filter((entry): entry is { index: number; value: number } => entry.value !== null);
  const xScale = scaleLinear<number>({
    domain: [0, Math.max(plot.dates.length - 1, 1)],
    range: [0, innerWidth],
  });
  const yScale = scaleLinear<number>({
    domain: [Math.min(plot.min, -1), 0],
    range: [innerHeight, 0],
    nice: true,
  });

  return (
    <figure data-testid="performance-overlay" className="px-4">
      <figcaption className="text-xs text-muted-foreground">
        Falls from the peak, on the same days — shaded red and labelled, so the shape is not the
        only cue.
      </figcaption>
      <svg viewBox={`0 0 ${WIDTH} ${OVERLAY_HEIGHT}`} role="img" aria-labelledby={titleId} className="w-full">
        <title id={titleId}>
          {`How far below its own peak the portfolio group sat, ${formatTradeDate(plot.dates[0])} to ${formatTradeDate(
            plot.dates[plot.dates.length - 1],
          )}.`}
        </title>
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {yScale.ticks(3).map((tick) => (
            <g key={tick} transform={`translate(0,${yScale(tick)})`}>
              <line x1={0} x2={innerWidth} className="stroke-border" strokeWidth={0.5} />
              <text
                x={-8}
                dy="0.32em"
                textAnchor="end"
                className="fill-muted-foreground text-[10px] tabular-nums"
              >
                {`${tick.toFixed(0)}%`}
              </text>
            </g>
          ))}
          {data.length >= MIN_MARKS ? (
            <AreaClosed<{ index: number; value: number }>
              data={data}
              x={(entry) => xScale(entry.index)}
              y={(entry) => yScale(entry.value)}
              yScale={yScale}
              curve={curveMonotoneX}
              className="fill-negative/25 stroke-negative"
              strokeWidth={1}
            />
          ) : null}
          <line
            x1={xScale(cursorIndex - span.from)}
            x2={xScale(cursorIndex - span.from)}
            y1={0}
            y2={innerHeight}
            className="stroke-foreground/40"
            strokeWidth={0.75}
            strokeDasharray="2 2"
          />
          <text
            x={innerWidth}
            y={12}
            textAnchor="end"
            data-testid="performance-direct-label-drawdown"
            className="fill-negative text-[10px] font-medium"
          >
            {line.label}
          </text>
        </g>
      </svg>
    </figure>
  );
}

/* ------------------------------------------------------------------ brush */

function Brush({
  series,
  span,
  cursorIndex,
  onBrush,
  onCursor,
  onReset,
}: {
  series: PerformanceSeries;
  span: SeriesSpan;
  cursorIndex: number;
  onBrush: (span: SeriesSpan) => void;
  onCursor: (index: number) => void;
  onReset: () => void;
}) {
  const last = series.points.length - 1;
  const from = series.points[span.from]?.on ?? null;
  const to = series.points[span.to]?.on ?? null;
  const narrowed = isNarrowed(series, span);

  return (
    <div
      data-testid="performance-brush"
      className="mx-4 mt-3 rounded-lg border border-border bg-muted/40 px-3 py-2.5"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <p className="flex items-center gap-1.5 text-xs font-medium">
          <MoveHorizontal aria-hidden="true" className="size-3.5 text-muted-foreground" />
          Window
          <span className="font-normal text-muted-foreground">
            {formatTradeDate(from)} – {formatTradeDate(to)} · {span.to - span.from + 1} trading days
          </span>
        </p>
        {narrowed ? (
          <button
            type="button"
            onClick={onReset}
            data-testid="performance-brush-reset"
            className="text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
          >
            Show the whole range
          </button>
        ) : null}
      </div>

      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        <label className="flex items-center gap-2 text-[0.6875rem] uppercase tracking-wide text-muted-foreground">
          <span className="w-10 shrink-0">Start</span>
          <input
            type="range"
            min={0}
            max={last}
            value={span.from}
            aria-label="Window start"
            data-testid="performance-brush-start"
            onChange={(event) => onBrush({ from: Number(event.target.value), to: span.to })}
            className="w-full accent-[var(--accent)]"
          />
        </label>
        <label className="flex items-center gap-2 text-[0.6875rem] uppercase tracking-wide text-muted-foreground">
          <span className="w-10 shrink-0">End</span>
          <input
            type="range"
            min={0}
            max={last}
            value={span.to}
            aria-label="Window end"
            data-testid="performance-brush-end"
            onChange={(event) => onBrush({ from: span.from, to: Number(event.target.value) })}
            className="w-full accent-[var(--accent)]"
          />
        </label>
        <label className="flex items-center gap-2 text-[0.6875rem] uppercase tracking-wide text-muted-foreground">
          <span className="w-10 shrink-0">Read</span>
          <input
            type="range"
            min={span.from}
            max={span.to}
            value={cursorIndex}
            aria-label="Day under the read-out"
            data-testid="performance-cursor"
            onChange={(event) => onCursor(Number(event.target.value))}
            className="w-full accent-[var(--accent)]"
          />
        </label>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ read-out */

interface ReadoutCell {
  readonly metric: Metric;
  readonly unit: "rupees" | "percent";
}

function ReadoutPanel({
  on,
  cells,
  events,
}: {
  on: string;
  cells: readonly ReadoutCell[];
  events: ReadonlyArray<{ key: string; glyph: string; label: string; detail: string }>;
}) {
  return (
    <section
      aria-label="Read-out for the selected day"
      data-testid="performance-readout"
      className="mt-3 border-t border-border"
    >
      <p className="flex items-center gap-2 px-4 pt-3 text-sm font-semibold">
        {formatTradeDate(on)}
        <span className="text-xs font-normal text-muted-foreground">
          the day under the read-out
        </span>
      </p>
      <div className="flex flex-wrap divide-x divide-border">
        {cells.map((cell) => (
          <div key={cell.metric.label} className="min-w-[11rem] flex-1">
            <MetricCell
              metric={cell.metric}
              /* Value and capital in play are balances, not moves: colouring a balance green
                 because it is positive says nothing, so only the moves are signed. */
              signed={cell.metric.label !== "Value" && cell.metric.label !== "Capital in play"}
              percent={cell.unit === "percent"}
            />
          </div>
        ))}
      </div>
      {events.length > 0 ? (
        <ul data-testid="performance-readout-events" className="space-y-1 px-4 pb-3 text-xs">
          {events.map((event) => (
            <li key={event.key} className="flex items-start gap-1.5 text-muted-foreground">
              <span aria-hidden="true" className="font-semibold text-foreground">
                {event.glyph}
              </span>
              <span>
                <strong className="font-medium text-foreground">{event.label}.</strong>{" "}
                {event.detail}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/* ------------------------------------------------------------------ states */

function ExplainPanel({
  explanation,
  testId,
  subtle = false,
}: {
  explanation: Explanation;
  testId: string;
  subtle?: boolean;
}) {
  return (
    <div
      data-testid={testId}
      className={cn(
        "flex items-start gap-2 px-4 py-4",
        subtle ? "border-t border-border" : "m-4 rounded-lg border border-dashed border-border",
      )}
    >
      <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0">
        <p className="text-sm font-medium">{explanation.headline}</p>
        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{explanation.detail}</p>
        {explanation.action !== null ? (
          <a
            href={explanation.action.href}
            className="mt-1 inline-block text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
          >
            {explanation.action.label}
          </a>
        ) : null}
      </div>
    </div>
  );
}
