import Link from "next/link";

import { AccessBadge } from "@/components/explore/access-badge";
import { ReturnStat } from "@/components/explore/return-stat";
import { VolatilityChip } from "@/components/explore/volatility-chip";
import type { ExploreBasketCard } from "@/lib/explore/fetch";
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

function pitch(description: string | null): string {
  if (!description) return "No one-line summary yet.";
  const plain = description.replace(/[#*_`]/g, "").replace(/\s+/g, " ").trim();
  if (plain.length <= 120) return plain;
  return `${plain.slice(0, 117).trimEnd()}…`;
}

/**
 * Catalog card — docs/smallcase/05 BasketCard.
 * Watchlist toggle lands with SC6; the slot is reserved as a non-mutating placeholder.
 */
export function BasketCard({ basket }: { basket: ExploreBasketCard }) {
  const metrics = basket.metrics;
  return (
    <Link
      href={`/basket/${basket.slug}`}
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4 transition-colors duration-150",
        "hover:border-muted-foreground/40 hover:bg-muted/30",
      )}
    >
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
            <AccessBadge access={basket.access} />
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Managed by {basket.manager.name}
          </p>
        </div>
      </div>

      <p className="line-clamp-2 text-sm leading-relaxed text-muted-foreground">
        {pitch(basket.description_md)}
      </p>

      <div className="mt-auto flex flex-wrap items-end justify-between gap-3 border-t border-border/60 pt-3">
        <div>
          <div className="eyebrow">Min. amount</div>
          <div className="mt-0.5 text-sm font-medium tabular-nums">
            {rupees(metrics?.min_amount)}
          </div>
        </div>
        <ReturnStat label={metrics?.headline_label} value={metrics?.headline_pct} />
        <VolatilityChip bucket={metrics?.volatility_bucket} />
      </div>
    </Link>
  );
}
