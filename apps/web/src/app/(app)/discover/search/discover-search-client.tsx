"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { filterBaskets } from "@/components/discover/basket-search";
import type { ExploreBasketCard } from "@/lib/explore/fetch";
import { EMPTY_CELL, formatPercent } from "@/lib/format";

/**
 * Self-contained Discover search surface (AF I.3).
 *
 * Lives at `/discover/search` so the hub page (owned by other lanes) does not need an edit.
 * Filters client-side over the catalogue payload passed from the server page.
 */

export function DiscoverSearchClient({ items }: { items: ExploreBasketCard[] }) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => filterBaskets(items, query), [items, query]);

  return (
    <div className="flex flex-col gap-4" data-testid="discover-search-page">
      <label className="block text-sm">
        <span className="mb-1.5 block text-xs text-muted-foreground">Search baskets</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Name, manager or category"
          className="w-full max-w-xl rounded-md border border-border bg-card px-3 py-2 text-sm"
          data-testid="discover-basket-search-input"
        />
      </label>
      <p className="text-xs text-muted-foreground" data-testid="discover-search-count">
        {filtered.length} of {items.length} basket{items.length === 1 ? "" : "s"}
      </p>
      <ul className="divide-y divide-border/60 rounded-xl border border-border/70 bg-card">
        {filtered.map((item) => (
          <li key={item.slug} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
            <div>
              <Link
                href={`/basket/${item.slug}`}
                className="text-sm font-semibold underline-offset-4 hover:underline"
              >
                {item.name}
              </Link>
              <p className="text-xs text-muted-foreground">
                {item.manager.name}
                {item.categories.length ? ` · ${item.categories.join(", ")}` : ""}
              </p>
            </div>
            <span className="text-sm tabular-nums">
              {item.metrics?.headline_pct != null
                ? formatPercent(item.metrics.headline_pct)
                : EMPTY_CELL}
            </span>
          </li>
        ))}
        {filtered.length === 0 ? (
          <li className="px-4 py-8 text-center text-sm text-muted-foreground">
            No baskets match “{query.trim()}”.
          </li>
        ) : null}
      </ul>
    </div>
  );
}
