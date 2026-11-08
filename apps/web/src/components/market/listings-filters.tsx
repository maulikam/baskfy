"use client";

import { Search } from "lucide-react";
import { parseAsString, useQueryState } from "nuqs";
import { useEffect, useState } from "react";

import { Input } from "@/components/ui/input";
import { useDebounced } from "@/lib/screens/use-debounced";

/**
 * The listings register's series filter and search box (Prompt 11 deliverable 4).
 *
 * Both write to the URL with `shallow: false`, because the register is cursor-paginated on the
 * server: filtering client-side would filter *the current page*, which is 100 of several thousand
 * rows and would look like the filter had found nothing.
 *
 * Changing either clears the cursor. A cursor is a position in one ordered result set; carrying it
 * into a differently-filtered set would resume from a row that is no longer in it.
 */
export interface ListingsFiltersProps {
  series: string;
  search: string;
  options: readonly string[];
}

const DEBOUNCE_MS = 250;

export function ListingsFilters({ series, search, options }: ListingsFiltersProps) {
  const [, setSeries] = useQueryState("series", {
    defaultValue: "",
    shallow: false,
    history: "push",
    parse: String,
  });
  const [, setSearch] = useQueryState(
    "search",
    parseAsString.withDefault("").withOptions({ shallow: false, history: "push" }),
  );
  const [, setCursor] = useQueryState("cursor", parseAsString.withOptions({ shallow: false }));

  const [draft, setDraft] = useState(search);
  const debounced = useDebounced(draft, DEBOUNCE_MS);

  useEffect(() => {
    if (debounced === search) return;
    void setCursor(null);
    void setSearch(debounced || null);
  }, [debounced, search, setSearch, setCursor]);

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="relative w-full max-w-xs">
        <label htmlFor="listing-search" className="mb-1 block text-xs text-muted-foreground">
          Search
        </label>
        <Search
          aria-hidden="true"
          className="pointer-events-none absolute left-2.5 top-[2.1rem] size-4 text-muted-foreground"
        />
        <Input
          id="listing-search"
          type="search"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Symbol or name"
          className="pl-8"
        />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="listing-series" className="text-xs text-muted-foreground">
          Series
        </label>
        <select
          id="listing-series"
          value={series}
          onChange={(event) => {
            void setCursor(null);
            void setSeries(event.target.value);
          }}
          className="h-9 rounded-md border border-border bg-card px-2 text-sm"
        >
          <option value="">All series</option>
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
