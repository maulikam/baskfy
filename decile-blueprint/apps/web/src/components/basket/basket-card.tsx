import Link from "next/link";
import type { Route } from "next";

import { EMPTY_CELL, formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

function monogram(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return `${parts[0]![0] ?? ""}${parts[1]![0] ?? ""}`.toUpperCase();
}

function rupees(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 0 })}`;
}

export interface SharedBasketCardModel {
  name: string;
  thesis: string;
  href: string;
  minAmount?: string | number | null;
  headlinePct?: string | number | null;
  headlineLabel?: string | null;
  topSymbols?: readonly string[];
  badge?: string;
  cagr?: string | number | null;
  volatility?: string | null;
}

/**
 * Shared smallcase-style card (Tree 6 §5.1). Explore catalog and Build auto-baskets both use this.
 */
export function BasketCard({
  basket,
  actions,
}: {
  basket: SharedBasketCardModel;
  actions?: React.ReactNode;
}) {
  const tops = basket.topSymbols?.slice(0, 3) ?? [];
  const more = (basket.topSymbols?.length ?? 0) - tops.length;

  return (
    <article
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4 transition-colors duration-150",
        "hover:border-muted-foreground/40 hover:bg-muted/30",
      )}
    >
      <Link href={basket.href as Route} className="flex flex-col gap-3 focus:outline-none">
        <div className="flex items-start gap-3">
          <span
            className="grid size-10 shrink-0 place-items-center rounded-md bg-muted text-xs font-semibold text-foreground"
            aria-hidden="true"
          >
            {monogram(basket.name)}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate text-sm font-semibold text-foreground">{basket.name}</h2>
              {basket.badge ? (
                <span className="rounded-md border border-border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {basket.badge}
                </span>
              ) : null}
            </div>
            <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
              {basket.thesis}
            </p>
          </div>
        </div>

        {tops.length > 0 ? (
          <p className="text-xs text-muted-foreground">
            {tops.join(" · ")}
            {more > 0 ? ` · +${more} more` : ""}
          </p>
        ) : null}

        <div className="mt-auto flex flex-wrap items-end justify-between gap-3 border-t border-border/60 pt-3">
          <div>
            <div className="eyebrow">Min. amount</div>
            <div className="mt-0.5 text-sm font-medium tabular-nums">
              {rupees(basket.minAmount)}
            </div>
          </div>
          {basket.headlinePct !== null && basket.headlinePct !== undefined ? (
            <div>
              <div className="eyebrow">{basket.headlineLabel ?? "1Y"}</div>
              <div className="mt-0.5 text-sm font-medium tabular-nums">
                {typeof basket.headlinePct === "number"
                  ? `${basket.headlinePct.toFixed(1)}%`
                  : basket.headlinePct}
              </div>
            </div>
          ) : null}
          {basket.volatility ? (
            <div>
              <div className="eyebrow">Swing</div>
              <div className="mt-0.5 text-sm font-medium">{basket.volatility}</div>
            </div>
          ) : null}
        </div>
      </Link>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </article>
  );
}
