"use client";

import { useState } from "react";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { cn } from "@/lib/utils";

/**
 * Save / unsave an instrument on the consumer watchlist (AF I.2).
 *
 * Uses `/api/v1/watchlist/instruments` — separate from basket stars and the swing book.
 */

export function SaveInstrumentButton({
  symbol,
  initiallySaved = false,
  className,
}: {
  symbol: string;
  initiallySaved?: boolean;
  className?: string;
}) {
  const [saved, setSaved] = useState(initiallySaved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    setBusy(true);
    setError(null);
    const next = !saved;
    setSaved(next);
    try {
      const token = await accessToken();
      const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
      const response = next
        ? await fetch(`${apiOrigin()}/api/v1/watchlist/instruments`, {
            method: "POST",
            headers: { ...headers, "Content-Type": "application/json" },
            body: JSON.stringify({ symbol }),
          })
        : await fetch(
            `${apiOrigin()}/api/v1/watchlist/instruments/${encodeURIComponent(symbol)}`,
            { method: "DELETE", headers },
          );
      if (!response.ok && response.status !== 409) {
        setSaved(!next);
        setError(`Could not update watchlist (${response.status})`);
      }
    } catch {
      setSaved(!next);
      setError("Could not update watchlist");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={cn("inline-flex flex-col gap-1", className)}>
      <button
        type="button"
        disabled={busy}
        onClick={() => void toggle()}
        data-testid="save-instrument"
        data-saved={saved ? "true" : "false"}
        className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium text-foreground transition-colors hover:border-accent hover:text-accent disabled:opacity-60"
      >
        {saved ? "Saved" : "Save to watchlist"}
      </button>
      {error ? <span className="text-xs text-destructive">{error}</span> : null}
    </div>
  );
}
