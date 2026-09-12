"use client";

import { useEffect, useMemo, useState } from "react";

import type { ExploreBasketCard } from "@/lib/explore/fetch";
import { cn } from "@/lib/utils";

/**
 * Client-side basket search over an already-fetched catalogue (AF I.3).
 *
 * Global ⌘K already searches the plant; this filters the Discover list in place without a
 * second round trip. Matching is case-insensitive on name, slug, manager and categories.
 */

export function filterBaskets(items: ExploreBasketCard[], query: string): ExploreBasketCard[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return items;
  return items.filter((item) => {
    const haystack = [
      item.name,
      item.slug,
      item.manager.name,
      item.manager.slug,
      ...item.categories,
      item.description_md ?? "",
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  });
}

export function BasketSearch({
  items,
  onFiltered,
  className,
}: {
  items: ExploreBasketCard[];
  onFiltered?: (items: ExploreBasketCard[]) => void;
  className?: string;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => filterBaskets(items, query), [items, query]);

  useEffect(() => {
    onFiltered?.(filtered);
  }, [filtered, onFiltered]);

  return (
    <div className={cn("w-full", className)} data-testid="discover-basket-search">
      <label className="block text-sm">
        <span className="mb-1.5 block text-xs text-muted-foreground">Search baskets</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Name, manager or category"
          className="w-full rounded-md border border-border bg-card px-3 py-2 text-sm"
          data-testid="discover-basket-search-input"
        />
      </label>
      {query.trim() ? (
        <p className="mt-1.5 text-xs text-muted-foreground" data-testid="discover-search-count">
          {filtered.length} of {items.length} basket{items.length === 1 ? "" : "s"}
        </p>
      ) : null}
    </div>
  );
}
