import type { CellOut, IndexMembershipOut } from "@decile/api-client";

import { MetricGrid, type MetricRow } from "@/components/data/metric-grid";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { display, percentileOf } from "@/lib/instrument/present";

/**
 * docs/01 §5 blocks 6–9 in the shape docs/08 asks for:
 *
 *     "Returns/Sharpe/Volatility/RSI render as a compact 5-column grid, one row per family, with
 *      a small bar behind each cell showing that value's percentile within the current universe.
 *      This is a real improvement over the reference product's plain numbers."
 *
 * The tooltip is the other half of that improvement. A bar with no stated comparison set is a
 * decoration; "percentile within NIFTY MICROCAP 250 on 18 Aug 2026" is a claim. The universe is
 * chosen by the API — the narrowest index the instrument belongs to that day — and named here so
 * the reader never has to assume which one it was.
 *
 * The two skip-month returns (docs/01 §5 block 6: "12M−1M, 12M−2M") do not fit the five-window
 * grid and are rendered under it rather than dropped.
 */
export interface FactorGridProps {
  returns: readonly CellOut[];
  sharpeReturns: readonly CellOut[];
  volatility: readonly CellOut[];
  rsi: readonly CellOut[];
  universe: IndexMembershipOut | null | undefined;
  asOf: string;
}

const WINDOW_COUNT = 5;

function toRow(family: string, cells: readonly CellOut[]): MetricRow {
  return {
    family,
    cells: cells.slice(0, WINDOW_COUNT).map((cell) => ({
      display: display(cell),
      percentile: percentileOf(cell),
    })),
  };
}

export function FactorGrid({
  returns,
  sharpeReturns,
  volatility,
  rsi,
  universe,
  asOf,
}: FactorGridProps) {
  const columns = returns.slice(0, WINDOW_COUNT).map((cell) => cell.label);
  const skipMonth = returns.slice(WINDOW_COUNT);

  const explanation = universe
    ? `The bar behind each number is that value's percentile among ${universe.name} members on ${asOf}. A longer bar is a higher rank within that universe.`
    : `This instrument was in no selectable universe on ${asOf}, so there is nothing to rank it against and no bars are drawn.`;

  return (
    <div className="flex flex-col gap-3">
      <Tooltip>
        <TooltipTrigger asChild>
          <p className="w-fit cursor-help text-xs text-muted-foreground underline decoration-dotted underline-offset-2">
            Ranked against {universe ? universe.name : "no universe"}
          </p>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{explanation}</TooltipContent>
      </Tooltip>

      <MetricGrid
        columns={columns}
        caption="Returns, Sharpe returns, volatility and RSI across the five windows, with each value's percentile in the comparison universe"
        rows={[
          toRow("Returns", returns),
          toRow("Sharpe", sharpeReturns),
          toRow("Volatility", volatility),
          toRow("RSI", rsi),
        ]}
      />

      {skipMonth.length > 0 ? (
        <dl className="flex flex-wrap gap-x-6 gap-y-1 border-t border-border pt-2 text-xs">
          {skipMonth.map((cell) => (
            <div key={cell.key} className="flex items-baseline gap-1.5">
              <dt className="text-muted-foreground">{cell.label}</dt>
              <dd className="font-medium tnum">{display(cell)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
