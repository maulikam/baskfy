"use client";

import { parseAsStringLiteral, useQueryState } from "nuqs";

import { RANGE_KEYS, RANGE_PRESETS } from "@/lib/market/ranges";
import { cn } from "@/lib/utils";

/**
 * docs/08 §Routes: `/market-health` is "RSC + **client universe switcher**".
 *
 * Both controls write to the URL with `shallow: false`, so the server component re-renders with
 * the new universe or range and refetches. That is the point of putting them in the URL rather
 * than in component state: a market-health view is a thing people paste into a message, and the
 * range determines *what the server fetches*, not just what the client draws — a 5-year chart
 * from a 1-year payload would be a lie drawn in the right colours.
 */
export interface MarketHealthControlsProps {
  universes: readonly { slug: string; name: string }[];
  universe: string;
  range: string;
}

export function MarketHealthControls({
  universes,
  universe,
  range,
}: MarketHealthControlsProps) {
  const [, setUniverse] = useQueryState(
    "universe",
    { defaultValue: universe, shallow: false, history: "push", parse: String },
  );
  const [, setRange] = useQueryState(
    "range",
    parseAsStringLiteral(RANGE_KEYS)
      .withDefault("1y")
      .withOptions({ shallow: false, history: "push" }),
  );

  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="flex flex-col gap-1">
        <label htmlFor="universe" className="text-xs font-medium text-muted-foreground">
          Which stocks
        </label>
        <select
          id="universe"
          value={universe}
          onChange={(event) => void setUniverse(event.target.value)}
          className="h-9 rounded-md border border-border bg-card px-2 text-sm"
        >
          {universes.map((option) => (
            <option key={option.slug} value={option.slug}>
              {option.name}
            </option>
          ))}
        </select>
      </div>

      <div
        role="group"
        aria-label="How far back to look"
        className="flex rounded-md border border-border p-0.5"
      >
        {RANGE_PRESETS.map((preset) => (
          <button
            key={preset.key}
            type="button"
            aria-pressed={range === preset.key}
            onClick={() => void setRange(preset.key)}
            className={cn(
              "rounded-sm px-2.5 py-1 text-xs",
              range === preset.key ? "bg-muted font-medium" : "text-muted-foreground",
            )}
          >
            {preset.label}
          </button>
        ))}
      </div>
    </div>
  );
}
