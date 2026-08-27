import Link from "next/link";
import type { Route } from "next";

import { StrategyMark } from "@/components/discover/strategy-mark";
import { formatReturn, formatRupees } from "@/lib/discover/metrics";
import { cn } from "@/lib/utils";

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
  /** The volatility bucket in words. Never labelled "Swing": that named nothing measurable. */
  volatility?: string | null;
  /** Strategy tags, so the mark can be the strategy's rather than the name's initials. */
  categories?: readonly string[];
}

/**
 * Shared card for Build's saved screens shown as baskets (Tree 6 §5.1).
 *
 * The catalogue has its own richer card now (`components/discover/basket-card.tsx`) — this one
 * survives for screens, which carry a name and a thesis and no metrics at all. Three defects it
 * shared with the old catalogue card are fixed here too, because a screen card sits beside basket
 * cards on the same page and may not contradict them: the two-letter monogram is a strategy mark,
 * the risk column is labelled by its measure rather than "Swing", and a percentage arrives from
 * the API as a string so the unit is applied by `formatReturn` rather than by a `typeof` check
 * that was never true in production.
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
          <StrategyMark categories={basket.categories ?? []} />
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
              {formatRupees(basket.minAmount)}
            </div>
          </div>
          {basket.headlinePct !== null && basket.headlinePct !== undefined ? (
            <div>
              <div className="eyebrow">{basket.headlineLabel ?? "1Y"}</div>
              <div className="mt-0.5 text-sm font-medium tabular-nums">
                {formatReturn(basket.headlinePct)}
              </div>
            </div>
          ) : null}
          {basket.volatility ? (
            <div>
              <div className="eyebrow">Volatility</div>
              <div className="mt-0.5 text-sm font-medium">{basket.volatility}</div>
            </div>
          ) : null}
        </div>
      </Link>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </article>
  );
}
