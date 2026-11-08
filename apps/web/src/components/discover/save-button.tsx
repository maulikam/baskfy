"use client";

import { useState } from "react";

import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

/**
 * Save a basket to the watchlist, from wherever the reader met it.
 *
 * The watchlist has existed end to end since SC6 — `cb_watchlist_item`, `GET/POST/DELETE
 * /api/v1/watchlist`, and a page at `/portfolio/watchlist` whose own empty state reads "Star a basket
 * from Explore **when the toggle lands**". This is that toggle. Nothing new is stored: a second
 * client-side list would drift from the server's the moment a reader used two devices.
 *
 * **Optimistic, and honest when the optimism was wrong.** The button flips immediately, because
 * a save that waits on a round trip feels broken. If the request then fails it flips back and
 * says why, rather than leaving a reader believing something was saved that was not. A 409 from
 * a double-click is treated as success — the desired state is what the reader asked for, and it
 * is what the server now holds.
 */

export type SaveState = "idle" | "saving" | "error";

export function SaveButton({
  slug,
  name,
  initiallySaved = false,
  className,
}: {
  slug: string;
  /** Named in the accessible label, so a list of Save buttons is navigable by screen reader. */
  name: string;
  initiallySaved?: boolean;
  className?: string;
}) {
  const [saved, setSaved] = useState(initiallySaved);
  const [state, setState] = useState<SaveState>("idle");

  async function save(): Promise<void> {
    const next = !saved;
    setSaved(next);
    setState("saving");
    try {
      const api = browserApi();
      const response = next
        ? await api.POST("/api/v1/watchlist", { body: { basket_slug: slug } })
        : await api.DELETE("/api/v1/watchlist/{slug}", { params: { path: { slug } } });
      // 409 on add and 404 on remove both mean the server already holds what was asked for.
      const status = response.response.status;
      const alreadyThere = next ? status === 409 : status === 404;
      if (response.error && !alreadyThere) throw new Error(String(status));
      setState("idle");
    } catch {
      setSaved(!next);
      setState("error");
    }
  }

  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      <button
        type="button"
        onClick={() => void save()}
        disabled={state === "saving"}
        aria-pressed={saved}
        aria-label={saved ? `Remove ${name} from your watchlist` : `Save ${name} to your watchlist`}
        data-testid="save-button"
        data-saved={saved ? "true" : "false"}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-medium transition-colors duration-150",
          saved
            ? "border-accent/60 bg-accent/10 text-accent"
            : "border-border text-muted-foreground hover:border-muted-foreground/50 hover:text-foreground",
          state === "saving" && "opacity-60",
          className,
        )}
      >
        <span aria-hidden="true">{saved ? "★" : "☆"}</span>
        {saved ? "Saved" : "Save"}
      </button>
      {state === "error" ? (
        <span role="status" className="text-[11px] text-destructive" data-testid="save-error">
          Could not reach the watchlist. Nothing was changed.
        </span>
      ) : null}
    </span>
  );
}
