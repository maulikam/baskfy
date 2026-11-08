"use client";

import type { ScreenOut } from "@baskfy/api-client";
import { useState } from "react";

import { CreateBasketForm } from "@/components/create/create-basket-form";
import { CreateFromScreen } from "@/components/create/create-from-screen";
import { cn } from "@/lib/utils";

type Mode = "screen" | "manual";

/**
 * `/create` — screen-first (SB2), with the original SC8 symbol list one tap away.
 *
 * A first login already has the example screens. The default path is: pick one from the
 * dropdown, set an amount and a name count (the holding profile suggests the count; an
 * explicit number beats it), save. Typing symbols by hand is still here because a screen is
 * not the only way a basket starts.
 */
export function CreateWorkspace({
  screens,
  initialScreenId,
}: {
  screens: ScreenOut[];
  initialScreenId: string | null;
}) {
  const [mode, setMode] = useState<Mode>("screen");

  return (
    <div className="flex w-full min-w-0 flex-col gap-6">
      <div
        className="inline-flex w-fit gap-1 rounded-lg border border-border/70 bg-muted/40 p-1"
        role="group"
        aria-label="How to build this basket"
      >
        {(
          [
            ["screen", "From a screen"],
            ["manual", "Pick stocks yourself"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            data-testid={`create-mode-${id}`}
            aria-pressed={mode === id}
            onClick={() => setMode(id)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm transition-colors",
              mode === id
                ? "marker-control font-medium"
                : "text-muted-foreground hover:bg-background hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === "screen" ? (
        <CreateFromScreen screens={screens} initialScreenId={initialScreenId} />
      ) : (
        <CreateBasketForm />
      )}
    </div>
  );
}
