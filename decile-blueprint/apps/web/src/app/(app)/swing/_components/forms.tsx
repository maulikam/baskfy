"use client";

import { useActionState, useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { searchCatalog } from "@/lib/api/search";
import type { SwingFormResult } from "@/lib/swing/write";
import { cn } from "@/lib/utils";

/**
 * The hub's forms — SW14. Plain `<form action={…}>` bound to a server action through
 * `useActionState`, so each one posts without JavaScript and, with it, renders the action's
 * `{ ok, error? }` beside the control it belongs to instead of navigating away.
 *
 * Nothing here is a confirm. There is no button that confirms everything at once, none that touches an order path,
 * and every action a form can name is one of the five `__tests__/read-only.test.tsx` enumerates.
 */

export type SwingAction = (
  previous: SwingFormResult | null,
  formData: FormData,
) => Promise<SwingFormResult>;

function Outcome({ result, className }: { result: SwingFormResult | null; className?: string }) {
  return (
    <span
      role="status"
      aria-live="polite"
      className={cn(
        "text-xs",
        result?.ok === false ? "text-negative" : "text-muted-foreground",
        className,
      )}
    >
      {result === null ? "" : result.ok ? result.message : result.error}
    </span>
  );
}

/**
 * A one-button form for a row: Watch, Dismiss, Still watching. The hidden fields carry the row
 * the button belongs to, and the outcome renders inline so a refused dismiss says why.
 */
export function RowActionForm({
  action,
  fields,
  label,
  pendingLabel,
  title,
  variant = "outline",
}: {
  action: SwingAction;
  fields: Record<string, string | number>;
  label: string;
  pendingLabel: string;
  title?: string;
  variant?: "outline" | "ghost";
}) {
  const [result, formAction, pending] = useActionState(action, null);
  return (
    <form action={formAction} className="inline-flex flex-wrap items-center gap-2">
      {Object.entries(fields).map(([name, value]) => (
        <input key={name} type="hidden" name={name} value={String(value)} />
      ))}
      <Button type="submit" size="sm" variant={variant} disabled={pending} title={title}>
        {pending ? pendingLabel : label}
      </Button>
      <Outcome result={result} />
    </form>
  );
}

/** The note and the catalyst on a watchlist row, edited in place. Levels are not fields here. */
export function AnnotateForm({
  action,
  id,
  note,
  catalyst,
}: {
  action: SwingAction;
  id: number;
  note: string | null;
  catalyst: string | null;
}) {
  const [result, formAction, pending] = useActionState(action, null);
  const noteId = useId();
  const catalystId = useId();
  return (
    <form action={formAction} className="flex min-w-[14rem] flex-col gap-1.5">
      <input type="hidden" name="id" value={id} />
      <Label htmlFor={noteId} className="sr-only">
        Note
      </Label>
      <Input
        id={noteId}
        name="note"
        defaultValue={note ?? ""}
        placeholder="Note"
        maxLength={2000}
        className="h-8 text-xs"
      />
      <Label htmlFor={catalystId} className="sr-only">
        Catalyst
      </Label>
      <Input
        id={catalystId}
        name="catalyst"
        defaultValue={catalyst ?? ""}
        placeholder="Catalyst"
        maxLength={2000}
        className="h-8 text-xs"
      />
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" variant="ghost" disabled={pending}>
          {pending ? "Saving…" : "Save note"}
        </Button>
        <Outcome result={result} />
      </div>
    </form>
  );
}

/** How long to wait after the last keystroke before asking `/search`. */
const TYPEAHEAD_DELAY_MS = 180;

/**
 * The symbol box, with `/search` behind a `<datalist>`.
 *
 * A plain text input first: without JavaScript a person types the symbol and the action resolves
 * it (`POST /swing/watch` accepts a symbol). With it, the existing catalogue search fills the
 * list's suggestions — stocks only, because a watchlist row is an instrument.
 */
export function SymbolInput({ id }: { id: string }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<readonly { symbol: string; name: string }[]>([]);
  const listId = useId();

  const needle = query.trim();

  useEffect(() => {
    if (needle.length < 2) return undefined;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void searchCatalog(needle, controller.signal, 8).then((outcome) => {
        if (outcome.status !== "ok" || outcome.query !== needle) return;
        setHits(
          outcome.hits
            .filter((hit) => hit.kind === "instrument")
            .map((hit) => ({ symbol: hit.id, name: hit.subtitle ?? hit.title })),
        );
      });
    }, TYPEAHEAD_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [needle]);

  // Suggestions belong to a query of two characters or more; a shorter one shows none.
  const shown = needle.length < 2 ? [] : hits;

  return (
    <>
      <Input
        id={id}
        name="symbol"
        list={listId}
        value={query}
        onChange={(event) => setQuery(event.target.value.toUpperCase())}
        placeholder="Symbol, as NSE lists it"
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        required
        maxLength={32}
      />
      <datalist id={listId}>
        {shown.map((hit) => (
          <option key={hit.symbol} value={hit.symbol}>
            {hit.name}
          </option>
        ))}
      </datalist>
    </>
  );
}

/** `05` §2's "Add manual" form: symbol search, setup, trigger, stop reference. */
export function WatchAddForm({ action }: { action: SwingAction }) {
  const [result, formAction, pending] = useActionState(action, null);
  const symbolId = useId();
  const setupId = useId();
  const triggerId = useId();
  const stopId = useId();
  const noteId = useId();
  return (
    <form action={formAction} className="grid gap-3 sm:grid-cols-[minmax(10rem,1.4fr)_8rem_8rem_8rem_auto] sm:items-end">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={symbolId}>Symbol</Label>
        <SymbolInput id={symbolId} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={setupId}>Setup</Label>
        <Select id={setupId} name="setup" defaultValue="FLAG">
          <option value="FLAG">Flag</option>
          <option value="EP">Episodic pivot</option>
        </Select>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={triggerId}>Trigger</Label>
        <Input
          id={triggerId}
          name="trigger"
          inputMode="decimal"
          placeholder="149.60"
          pattern="\d+(\.\d+)?"
          title="The price it has to clear, in rupees"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={stopId}>Stop reference</Label>
        <Input
          id={stopId}
          name="stop_ref"
          inputMode="decimal"
          placeholder="141.86"
          pattern="\d+(\.\d+)?"
          title="Where the stop would go, in rupees — below the trigger"
        />
      </div>
      <Button type="submit" variant="primary" disabled={pending} className="sm:mb-px">
        {pending ? "Adding…" : "Watch"}
      </Button>
      <div className="flex flex-col gap-1.5 sm:col-span-4">
        <Label htmlFor={noteId} className="sr-only">
          Note
        </Label>
        <Input id={noteId} name="note" placeholder="Why this one (optional)" maxLength={2000} />
      </div>
      <Outcome result={result} className="sm:col-span-5" />
    </form>
  );
}
