"use client";

import type { ScreenRunResponse } from "@baskfy/api-client";
import { useMemo, useState } from "react";

import { BasketDetail } from "@/components/basket/basket-detail";
import { materializeBasket } from "@/lib/basket/materialize";
import { cn } from "@/lib/utils";

/**
 * Default view for a screen run: investable basket first; raw table behind a toggle (Tree 6 §5).
 */
export function ScreenBasketView({
  result,
  screenName,
  topN = 20,
  table,
}: {
  result: ScreenRunResponse;
  screenName: string;
  topN?: number;
  table: React.ReactNode;
}) {
  const [mode, setMode] = useState<"basket" | "table">("basket");

  const basket = useMemo(
    () =>
      materializeBasket({
        name: screenName,
        thesis: "Weighted from your screen rules — equal weight, 5% cash.",
        asOf: result.as_of,
        topN,
        rows: result.rows.map((row) => ({
          symbol: row.symbol,
          name: row.name,
          rank: row.rank,
          sorting_factor:
            typeof row.sorting_factor === "number" ? row.sorting_factor : null,
          close_raw:
            typeof row.close_raw === "number"
              ? row.close_raw
              : typeof row.close === "number"
                ? row.close
                : null,
        })),
        source: "preview",
      }),
    [result, screenName, topN],
  );

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div
        className="inline-flex w-fit gap-1 rounded-lg border border-border/70 bg-muted/40 p-1"
        role="group"
        aria-label="Result view"
      >
        {(
          [
            ["basket", "Basket"],
            ["table", "Table"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            data-testid={`view-mode-${id}`}
            aria-pressed={mode === id}
            onClick={() => setMode(id)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm transition-colors",
              mode === id
                ? "marker-control font-medium"
                : "text-muted-foreground hover:bg-background hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === "basket" ? <BasketDetail basket={basket} /> : table}
    </div>
  );
}
