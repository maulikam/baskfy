"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandLoading,
} from "@/components/ui/command";
import { searchInstruments, type InstrumentSearchOutcome } from "@/lib/api/instruments";
import { NAV_ITEMS, type NavItem } from "@/lib/nav";

/**
 * docs/08 §"App shell": "Top bar: global instrument search (`⌘K`)".
 *
 * Two sections. **Instruments** comes from `GET /instruments?search=` — an endpoint Prompt 10
 * delivers, so today it reports itself as unavailable rather than showing an empty result set that
 * looks like "no such stock" (`src/lib/api/instruments.ts`). **Go to** is the navigation half,
 * which is what makes the palette useful in the meantime and is standard for a ⌘K anyway.
 *
 * Search is debounced and aborts the in-flight request on each keystroke: a typeahead that races
 * its own responses shows the results for a prefix the user has already finished typing.
 */
const DEBOUNCE_MS = 200;
const MIN_QUERY_LENGTH = 1;

export function CommandPalette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [outcome, setOutcome] = useState<{ query: string; result: InstrumentSearchOutcome } | null>(
    null,
  );
  const [searching, setSearching] = useState(false);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setOpen((current) => !current);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  /*
   * The effect only *schedules*; it never sets state synchronously in its body. That keeps it off
   * React's cascading-render path and, more usefully, means a keystroke costs one render rather
   * than three. `outcome` is stamped with the query that produced it, so a stale response for a
   * prefix the user has finished typing is ignored by the derivation below rather than rendered.
   */
  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < MIN_QUERY_LENGTH) return;

    const controller = new AbortController();
    const timer = setTimeout(() => {
      setSearching(true);
      void searchInstruments(trimmed, controller.signal)
        .then((result) => setOutcome({ query: trimmed, result }))
        .finally(() => setSearching(false));
    }, DEBOUNCE_MS);

    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [query]);

  const navMatches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return NAV_ITEMS;
    return NAV_ITEMS.filter((item) => item.label.toLowerCase().includes(needle));
  }, [query]);

  /**
   * Only `ready` destinations navigate. A planned item is listed so the palette can say when it
   * arrives, but selecting it would go to a 404 — and `NavItem` types `href` as a real `Route`
   * only for the ready ones, so this is checked rather than remembered.
   */
  const go = useCallback(
    (item: NavItem) => {
      if (item.status !== "ready") return;
      setOpen(false);
      setQuery("");
      router.push(item.href);
    },
    [router],
  );

  const goToInstrument = useCallback(
    (symbol: string) => {
      setOpen(false);
      setQuery("");
      router.push(`/instruments/${symbol}`);
    },
    [router],
  );

  const trimmed = query.trim();
  const current =
    outcome && outcome.query === trimmed && trimmed.length >= MIN_QUERY_LENGTH
      ? outcome.result
      : null;
  const instruments = current?.status === "ok" ? current.hits : [];

  return (
    <CommandDialog
      open={open}
      onOpenChange={setOpen}
      title="Search"
      description="Search instruments by symbol or name, or jump to a page."
    >
      <CommandInput
        value={query}
        onValueChange={setQuery}
        placeholder="Search instruments, or jump to a page…"
      />
      <CommandList className="max-h-80 overflow-y-auto">
        {searching ? (
          <CommandLoading className="px-3 py-2 text-sm text-muted-foreground">
            Searching…
          </CommandLoading>
        ) : null}

        {current?.status === "not-implemented" ? (
          <p className="px-3 py-2 text-sm text-muted-foreground">
            Instrument search is not available on this server. Page navigation works now.
          </p>
        ) : null}

        {current?.status === "failed" ? (
          <p className="px-3 py-2 text-sm text-muted-foreground">
            Instrument search is unavailable right now.
          </p>
        ) : null}

        {instruments.length > 0 ? (
          <CommandGroup heading="Instruments">
            {instruments.map((hit) => (
              <CommandItem
                key={hit.symbol}
                value={hit.symbol}
                onSelect={() => goToInstrument(hit.symbol)}
              >
                <span className="font-medium">{hit.symbol}</span>
                <span className="truncate text-muted-foreground">{hit.name}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        ) : null}

        {navMatches.length > 0 ? (
          <CommandGroup heading="Go to">
            {navMatches.map((item) => (
              <CommandItem
                key={item.href}
                value={item.href}
                disabled={item.status !== "ready"}
                onSelect={() => go(item)}
              >
                <span className="flex-1">{item.label}</span>
                {item.status === "planned" ? (
                  <span className="text-xs text-muted-foreground">{item.arrivesIn}</span>
                ) : null}
              </CommandItem>
            ))}
          </CommandGroup>
        ) : null}

        {!searching && navMatches.length === 0 && instruments.length === 0 && query ? (
          <CommandEmpty className="px-3 py-6 text-center text-sm text-muted-foreground">
            Nothing matches “{query}”.
          </CommandEmpty>
        ) : null}
      </CommandList>
    </CommandDialog>
  );
}
