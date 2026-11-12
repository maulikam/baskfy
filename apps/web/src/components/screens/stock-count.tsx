"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { MAX_INVEST_COUNT, checkInvestCount } from "@/lib/screens/invest";
import { cn } from "@/lib/utils";

/** "Stocks to show / invest": the screen's saved count, shared by its list and the invest default. */
export function StockCountControl({
  value,
  available,
  canEdit,
  saving,
  error,
  onSave,
}: {
  value: number | null;
  available: number | null;
  canEdit: boolean;
  saving: boolean;
  error?: string | null;
  onSave: (next: number | null) => void;
}) {
  const [draft, setDraft] = useState<string>(value === null ? "" : String(value));
  const [source, setSource] = useState(value);
  if (source !== value) {
    setSource(value);
    setDraft(value === null ? "" : String(value));
  }
  const parsed = draft.trim() === "" ? null : Number(draft);
  const problem = parsed === null ? null : checkInvestCount(parsed, null);
  const dirty = parsed !== value;

  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="stock-count">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="stock-count-input" className="font-medium">
          Stocks to show / invest
        </label>
        <input
          id="stock-count-input"
          type="number"
          inputMode="numeric"
          min={1}
          max={MAX_INVEST_COUNT}
          step={1}
          placeholder="All"
          value={draft}
          disabled={!canEdit || saving}
          onChange={(event) => setDraft(event.target.value)}
          aria-invalid={problem !== null}
          aria-describedby="stock-count-help"
          data-testid="stock-count-input"
          className="w-20 rounded-md border border-input bg-card px-2 py-1 text-sm tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
        />
        {canEdit ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-8"
            disabled={!dirty || problem !== null || saving}
            data-testid="stock-count-save"
            onClick={() => onSave(parsed === null ? null : Math.trunc(parsed))}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        ) : null}
      </div>
      <p
        id="stock-count-help"
        className={cn("text-xs", problem || error ? "text-warning-foreground" : "text-muted-foreground")}
      >
        {problem ??
          error ??
          (canEdit
            ? value === null
              ? `Blank shows every match${available ? ` (${available})` : ""}; invest defaults to 20.`
              : `The list shows the top ${value}, and Invest sizes ${value} by default.`
            : "Duplicate this screen to save a count.")}
      </p>
    </div>
  );
}
