"use client";

import {
  formatFact,
  visibleFactColumns,
  type HoldingFacts,
} from "@/lib/basket/holding-facts";
import {
  fillMissingCustomWeights,
  METHOD_BLURBS,
  METHOD_LABELS,
  WEIGHT_METHODS,
  type WeightMethod,
} from "@/lib/basket/methods";
import { cn } from "@/lib/utils";

export interface WeightMethodRow {
  symbol: string;
  name: string;
  facts: HoldingFacts;
}

/**
 * How the deployed money is split (SB4 / SB6).
 *
 * Five suggestions — equal, rank, the screen's score, inverse-vol — plus a true custom path.
 * Custom cannot add a name the screen did not select. The custom table shows the same facts
 * as the holdings table so a typed weight sits next to market cap, bumpiness and liquidity.
 */
export function WeightMethodControls({
  method,
  onMethodChange,
  rows,
  customWeights,
  onCustomWeightsChange,
  note,
  className,
}: {
  method: WeightMethod;
  onMethodChange: (value: WeightMethod) => void;
  rows: readonly WeightMethodRow[];
  customWeights: Readonly<Record<string, number>>;
  onCustomWeightsChange: (value: Record<string, number>) => void;
  note?: string | null;
  className?: string;
}) {
  const filled = fillMissingCustomWeights(
    rows.map((row) => row.symbol),
    customWeights,
  );
  const typedTotal = rows.reduce((sum, row) => sum + (filled[row.symbol] ?? 0), 0);
  const factColumns = visibleFactColumns(rows.map((row) => row.facts));

  return (
    <fieldset className={cn("flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4", className)}>
      <legend className="px-1 text-sm font-medium">How to split the money?</legend>
      <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Weight method">
        {WEIGHT_METHODS.map((option) => (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={method === option}
            data-testid={`method-${option.toLowerCase()}`}
            onClick={() => {
              onMethodChange(option);
              if (option === "CUSTOM") {
                onCustomWeightsChange(filled);
              }
            }}
            className={cn(
              "rounded-lg border px-3 py-2 text-left text-sm transition-colors",
              method === option
                ? "border-transparent marker-control font-medium"
                : "border-border/70 text-muted-foreground hover:bg-muted/50 hover:text-foreground",
            )}
          >
            {METHOD_LABELS[option]}
          </button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">{METHOD_BLURBS[method]}</p>
      {note ? (
        <p className="text-xs text-warning-foreground" data-testid="method-preview-note">
          {note}
        </p>
      ) : null}

      {method === "CUSTOM" ? (
        <div className="flex flex-col gap-2" data-testid="custom-weights">
          <p className="text-xs text-muted-foreground">
            Share of the stocks allocation. Numbers are scaled to 100 — they do not have to add up
            while you type. Current total {typedTotal.toFixed(1)}.
          </p>
          <div className="w-full min-w-0 overflow-hidden rounded-lg border border-border/70">
            <div className="w-full overflow-x-auto">
              <table className="w-full min-w-full table-auto text-sm tabular-nums">
                <caption className="sr-only">Your weights on the screen’s names</caption>
                <thead>
                  <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
                    <th className="px-3 py-2">Stock</th>
                    {factColumns.map((column) => (
                      <th key={column.key} className="px-3 py-2 text-right" title={column.title}>
                        {column.label}
                      </th>
                    ))}
                    <th className="px-3 py-2 text-right">Your weight</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr
                      key={row.symbol}
                      className="border-b border-border/60 last:border-0 hover:bg-muted/40"
                    >
                      <td className="px-3 py-2 font-medium">
                        {row.symbol}
                        {row.name !== row.symbol ? (
                          <span className="ml-2 text-xs font-normal text-muted-foreground">
                            {row.name}
                          </span>
                        ) : null}
                      </td>
                      {factColumns.map((column) => (
                        <td key={column.key} className="px-3 py-2 text-right">
                          {formatFact(column.key, row.facts[column.key])}
                        </td>
                      ))}
                      <td className="px-3 py-2 text-right">
                        <input
                          type="number"
                          inputMode="decimal"
                          min={0}
                          step={0.1}
                          value={filled[row.symbol] ?? 0}
                          onChange={(event) => {
                            const next = Number(event.target.value);
                            onCustomWeightsChange({
                              ...filled,
                              [row.symbol]: Number.isFinite(next) ? next : 0,
                            });
                          }}
                          aria-label={`${row.symbol} weight`}
                          className="w-24 rounded-md border border-border bg-background px-2 py-1 text-right tabular-nums"
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      ) : null}
    </fieldset>
  );
}
