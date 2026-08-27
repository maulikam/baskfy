"use client";

import type { CatalogHitOut, CatalogKind } from "@baskfy/api-client";
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
import { searchCatalog, type CatalogSearchOutcome } from "@/lib/api/search";
import { NAV_ITEMS, type NavItem } from "@/lib/nav";
import { hrefFor, KIND_LABELS, KIND_ORDER } from "@/lib/search/hrefs";
import { readRecents, rememberRecent, type RecentItem } from "@/lib/search/recents";

/**
 * The ⌘K palette — one search across the whole catalog.
 *
 * `baskfynavrefactorreport` §F11 named the defect ("Fragmented search") and §"Global search" named
 * the fix: "⌘K / tap-search opens a command palette searching stocks, indices, baskets, and
 * screens, with recent items. Replaces both existing scoped search boxes as the primary entry."
 * Until now this component searched instruments and filtered `NAV_ITEMS` — two of the five groups
 * below — and the report carried the rest as deferred.
 *
 * Five groups, in the order a person scans them: **Stocks · Indices · Baskets · Screens · Go to**.
 * The first four come from `GET /search` in one round trip (`baskfy_api.search` explains why one
 * and not four). **Go to** stays client-side: navigation targets are a compile-time constant in
 * `lib/nav.ts`, and asking a server which pages this build has would be absurd.
 *
 * Search is debounced and aborts the in-flight request on each keystroke, and every rendered
 * result is stamped with the query that produced it — a typeahead that races its own responses
 * shows results for a prefix the user has already finished typing.
 *
 * With the input empty the palette shows **recent items** and the nav, so it opens onto something
 * useful rather than onto a blank list.
 */
const DEBOUNCE_MS = 200;
const MIN_QUERY_LENGTH = 1;

/** What the palette renders in one group: catalog hits and recents share this shape. */
type Row = Pick<CatalogHitOut, "kind" | "id" | "title" | "subtitle">;

export function CommandPalette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [outcome, setOutcome] = useState<{ query: string; result: CatalogSearchOutcome } | null>(
    null,
  );
  const [searching, setSearching] = useState(false);
  const [recents, setRecents] = useState<readonly RecentItem[]>([]);

  /*
   * Recents are read here, in the event handler, rather than in an effect keyed on `open`:
   * `localStorage` is unavailable during the server render, and setting state from an effect body
   * costs a second render for a list of at most five strings. Reading on every ⌘K — including the
   * press that closes the dialog — is cheaper than the render it saves.
   */
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setRecents(readRecents());
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
    /* An empty box is not a query. Any request already in flight is aborted by the cleanup below
       and clears `searching` in its own `finally`, so there is nothing to reset here. */
    if (trimmed.length < MIN_QUERY_LENGTH) return;

    const controller = new AbortController();
    const timer = setTimeout(() => {
      setSearching(true);
      void searchCatalog(trimmed, controller.signal)
        .then((result) => setOutcome({ query: trimmed, result }))
        .finally(() => setSearching(false));
    }, DEBOUNCE_MS);

    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [query]);

  /**
   * Nav destinations matching the query, by their current name **or the one they used to have**.
   *
   * `formerly` has been on `NavItem` since Tree 6 and was only ever rendered. It has to be
   * searched too: the moment "Baskets" became "Discover", every reader who knew the old word
   * typed it into this palette and got nothing back. A rename is not supposed to orphan the
   * people who learned the previous name — that is the entire reason the field exists.
   */
  const navMatches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return NAV_ITEMS;
    return NAV_ITEMS.filter(
      (item) =>
        item.label.toLowerCase().includes(needle) ||
        (item.formerly?.toLowerCase().includes(needle) ?? false),
    );
  }, [query]);

  const dismiss = useCallback(() => {
    setOpen(false);
    setQuery("");
    setOutcome(null);
  }, []);

  /**
   * Only `ready` destinations navigate. A planned item is listed so the palette can say when it
   * arrives, but selecting it would go to a 404 — and `NavItem` types `href` as a real `Route`
   * only for the ready ones, so this is checked rather than remembered.
   */
  const go = useCallback(
    (item: NavItem) => {
      if (item.status !== "ready") return;
      dismiss();
      router.push(item.href);
    },
    [dismiss, router],
  );

  const openHit = useCallback(
    (hit: Row) => {
      // Remembered *before* navigating: the route change unmounts this component, and a write
      // scheduled after `router.push` is a race with React's own teardown.
      setRecents(rememberRecent(hit));
      dismiss();
      // `hrefFor` builds `/market/today?q=…` for an index, so this is not always a bare pathname.
      // `push` takes a string; the typed `Route` guarantee belongs to `lib/nav.ts`'s constants,
      // and `lib/search/hrefs.ts` is where these four are checked instead.
      router.push(hrefFor(hit));
    },
    [dismiss, router],
  );

  const trimmed = query.trim();
  const current =
    outcome && outcome.query === trimmed && trimmed.length >= MIN_QUERY_LENGTH
      ? outcome.result
      : null;
  const grouped = useMemo(() => {
    const hits: readonly Row[] = current?.status === "ok" ? current.hits : [];
    const byKind = new Map<CatalogKind, Row[]>();
    for (const hit of hits) {
      const bucket = byKind.get(hit.kind);
      if (bucket) bucket.push(hit);
      else byKind.set(hit.kind, [hit]);
    }
    return KIND_ORDER.map((kind) => ({ kind, rows: byKind.get(kind) ?? [] })).filter(
      (group) => group.rows.length > 0,
    );
  }, [current]);

  const showRecents = trimmed.length < MIN_QUERY_LENGTH && recents.length > 0;
  /*
   * "Nothing matches" is only true when the search actually answered. On `failed` or
   * `not-implemented` the palette already says what happened, and stacking a second message under
   * it would tell the user their query found nothing when in fact nothing was asked.
   */
  const nothingFound =
    !searching &&
    current?.status === "ok" &&
    grouped.length === 0 &&
    navMatches.length === 0;

  return (
    <CommandDialog
      open={open}
      onOpenChange={(next) => (next ? setOpen(true) : dismiss())}
      title="Search"
      description="Search stocks, indices, baskets and screens, or jump to a page."
    >
      <CommandInput
        value={query}
        onValueChange={setQuery}
        placeholder="Search stocks, indices, baskets, screens…"
      />
      <CommandList className="max-h-80 overflow-y-auto">
        {searching ? (
          <CommandLoading className="px-3 py-2 text-sm text-muted-foreground">
            Searching…
          </CommandLoading>
        ) : null}

        {current?.status === "not-implemented" ? (
          <p className="px-3 py-2 text-sm text-muted-foreground">
            Search is not available on this server. Page navigation works now.
          </p>
        ) : null}

        {current?.status === "failed" ? (
          <p className="px-3 py-2 text-sm text-muted-foreground">Search is unavailable right now.</p>
        ) : null}

        {showRecents ? (
          <CommandGroup heading="Recent">
            {recents.map((hit) => (
              <CommandItem
                key={`recent-${hit.kind}-${hit.id}`}
                value={`recent-${hit.kind}-${hit.id}`}
                onSelect={() => openHit(hit)}
              >
                <span className="font-medium">{hit.title}</span>
                {hit.subtitle ? (
                  <span className="truncate text-muted-foreground">{hit.subtitle}</span>
                ) : null}
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                  {KIND_LABELS[hit.kind]}
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        ) : null}

        {grouped.map((group) => (
          <CommandGroup key={group.kind} heading={KIND_LABELS[group.kind]}>
            {group.rows.map((hit) => (
              <CommandItem
                key={`${hit.kind}-${hit.id}`}
                value={`${hit.kind}-${hit.id}`}
                onSelect={() => openHit(hit)}
              >
                <span className="font-medium">{hit.title}</span>
                {hit.subtitle ? (
                  <span className="truncate text-muted-foreground">{hit.subtitle}</span>
                ) : null}
              </CommandItem>
            ))}
          </CommandGroup>
        ))}

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

        {nothingFound ? (
          <CommandEmpty className="px-3 py-6 text-center text-sm text-muted-foreground">
            Nothing matches “{query}”.
          </CommandEmpty>
        ) : null}
      </CommandList>
    </CommandDialog>
  );
}
