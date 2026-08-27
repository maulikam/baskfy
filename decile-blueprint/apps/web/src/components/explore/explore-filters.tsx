import type { Route } from "next";
import Link from "next/link";

import { definedParams } from "@/lib/explore/fetch";
import { cn } from "@/lib/utils";

/**
 * Shareable filter chips — state lives entirely in the URL (05-ui-spec §6.3).
 * Fee Based chip is omitted while subscriptions are off (Track B).
 */

export interface ExploreFilterState {
  max_min_amount?: string;
  access?: string;
  volatility?: string;
  sort?: string;
  order?: string;
  q?: string;
}

function hrefFor(next: ExploreFilterState): Route {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(definedParams(next))) {
    if (value) query.set(key, value);
  }
  const encoded = query.toString();
  return encoded ? `/explore?${encoded}` : "/explore";
}

function Chip({
  label,
  active,
  href,
}: {
  label: string;
  active: boolean;
  href: Route;
}) {
  return (
    <Link
      href={href}
      className={cn(
        "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors duration-150",
        active
          ? "border-accent bg-accent-muted text-accent"
          : "border-border/70 bg-card text-muted-foreground hover:border-muted-foreground/50 hover:text-foreground",
      )}
      aria-current={active ? "true" : undefined}
    >
      {label}
    </Link>
  );
}

export function ExploreFilters({ state }: { state: ExploreFilterState }) {
  const base = definedParams({
    sort: state.sort,
    order: state.order,
    q: state.q,
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Catalog filters">
        <Chip
          label="Under ₹25k"
          active={state.max_min_amount === "25000"}
          href={hrefFor(
            definedParams({
              ...base,
              max_min_amount: state.max_min_amount === "25000" ? undefined : "25000",
              access: state.access,
              volatility: state.volatility,
            }),
          )}
        />
        <Chip
          label="Under ₹5k"
          active={state.max_min_amount === "5000"}
          href={hrefFor(
            definedParams({
              ...base,
              max_min_amount: state.max_min_amount === "5000" ? undefined : "5000",
              access: state.access,
              volatility: state.volatility,
            }),
          )}
        />
        <Chip
          label="Free access"
          active={state.access === "FREE"}
          href={hrefFor(
            definedParams({
              ...base,
              access: state.access === "FREE" ? undefined : "FREE",
              max_min_amount: state.max_min_amount,
              volatility: state.volatility,
            }),
          )}
        />
        <Chip
          label="Low volatility"
          active={state.volatility === "LOW"}
          href={hrefFor(
            definedParams({
              ...base,
              volatility: state.volatility === "LOW" ? undefined : "LOW",
              max_min_amount: state.max_min_amount,
              access: state.access,
            }),
          )}
        />
        <span className="mx-1 h-4 w-px bg-border" aria-hidden="true" />
        <Chip
          label="Sort: name"
          active={!state.sort || state.sort === "name"}
          href={hrefFor(definedParams({ ...state, sort: "name", order: "asc" }))}
        />
        <Chip
          label="Sort: 1Y return"
          active={state.sort === "ret_1y"}
          href={hrefFor(definedParams({ ...state, sort: "ret_1y", order: "desc" }))}
        />
        <Chip
          label="Sort: min amount"
          active={state.sort === "min_amount"}
          href={hrefFor(definedParams({ ...state, sort: "min_amount", order: "asc" }))}
        />
      </div>
      {state.max_min_amount || state.access || state.volatility ? (
        <Link href="/explore" className="text-xs text-accent underline-offset-4 hover:underline">
          Clear filters
        </Link>
      ) : null}
    </div>
  );
}
