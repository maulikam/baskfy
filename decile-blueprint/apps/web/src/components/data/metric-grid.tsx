import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"Instrument factsheet":
 *
 *     "Returns/Sharpe/Volatility/RSI render as a compact 5-column grid, one row per family, with
 *      a small bar behind each cell showing that value's percentile within the current universe.
 *      This is a real improvement over the reference product's plain numbers."
 *
 * The bar is the point. A Sharpe of 2.1 means nothing on its own; "83rd percentile of NIFTY 500"
 * means something. It is drawn behind the number rather than beside it so the grid stays compact,
 * and it is `aria-hidden` with the percentile stated in the cell's accessible text instead —
 * docs/11 §Accessibility: colour and length are never the only carriers.
 */
export interface MetricCell {
  /** The formatted value, already rounded by the API (CLAUDE.md house rule 8). */
  display: string;
  /** 0–1 within the current universe. Omit when unknown; the bar is then not drawn. */
  percentile?: number | undefined;
}

export interface MetricRow {
  /** "Absolute return", "Sharpe return", … */
  family: string;
  cells: readonly MetricCell[];
}

export interface MetricGridProps {
  /** The window headings: 1Y, 9M, 6M, 3M, 1M (docs/01 §3's five windows). */
  columns: readonly string[];
  rows: readonly MetricRow[];
  loading?: boolean;
  caption?: string;
  className?: string | undefined;
}

function percentileLabel(percentile: number | undefined): string {
  if (percentile === undefined) return "";
  return `, ${Math.round(percentile * 100)}th percentile in this universe`;
}

export function MetricGrid({
  columns,
  rows,
  loading = false,
  caption,
  className,
}: MetricGridProps) {
  return (
    <table className={cn("w-full border-collapse text-sm", className)}>
      {caption ? <caption className="sr-only">{caption}</caption> : null}
      <thead>
        <tr>
          <th scope="col" className="py-1.5 pr-3 text-left text-xs font-medium text-muted-foreground">
            <span className="sr-only">Factor family</span>
          </th>
          {columns.map((column) => (
            <th
              key={column}
              scope="col"
              className="px-2 py-1.5 text-right text-xs font-medium text-muted-foreground"
            >
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.family} className="border-t border-border">
            <th scope="row" className="py-1.5 pr-3 text-left text-xs font-medium">
              {row.family}
            </th>
            {row.cells.map((cell, index) => (
              <td key={columns[index] ?? index} className="px-2 py-1.5">
                {loading ? (
                  <Skeleton className="ml-auto h-4 w-12" />
                ) : (
                  <span className="relative flex justify-end">
                    {cell.percentile !== undefined ? (
                      <span
                        aria-hidden="true"
                        className="absolute inset-y-0 right-0 rounded-sm bg-accent-muted"
                        style={{ width: `${Math.max(2, Math.round(cell.percentile * 100))}%` }}
                      />
                    ) : null}
                    <span className="relative px-1 tnum">
                      {cell.display}
                      <span className="sr-only">{percentileLabel(cell.percentile)}</span>
                    </span>
                  </span>
                )}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
