"use client";

import type { FactorOut } from "@baskfy/api-client";
import { Check, ChevronsUpDown } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { columnDisplayLabel, columnDisplayTooltip } from "@/lib/screens/column-display";
import { cn } from "@/lib/utils";

/**
 * The `Sort By` control — docs/08 §"Screen editor":
 *
 *     "The `Sort By` select has 62 options → use a searchable combobox grouped by family
 *      (Absolute / Sharpe / RSI / Beta-adjusted / Skip-month / Other)."
 *
 * It is 64 options, not 62: docs/01 §3 is headed "62 ranking factors" and enumerates 64, and
 * `baskfy_core.factor_registry` implements all 64 with the discrepancy written up. Either way the
 * point stands — a native `<select>` with that many entries is unusable, and the family grouping
 * is what makes it navigable.
 *
 * The options come from `GET /meta/factors`; docs/06 §"The factor registry" makes that registry
 * "the single source of truth for: the `sort_by` dropdown", so this component never carries a
 * list of its own.
 */
export interface FactorComboboxProps {
  factors: readonly FactorOut[];
  value: string | null;
  onChange: (key: string) => void;
  /** The id of the visible `<label>` that names this control. */
  labelledBy: string;
  placeholder?: string;
  disabled?: boolean | undefined;
  className?: string;
}

/** docs/08's own group names, in its order, keyed by `FactorFamily` from the registry. */
const FAMILY_LABELS: Record<string, string> = {
  absolute_return: "Absolute return",
  sharpe_return: "Sharpe return",
  rsi: "RSI",
  risk_adjusted: "Beta-adjusted",
  skip_month: "Skip-month",
  non_momentum: "Other",
};

const FAMILY_ORDER = [
  "absolute_return",
  "sharpe_return",
  "rsi",
  "risk_adjusted",
  "skip_month",
  "non_momentum",
] as const;

export function groupByFamily(
  factors: readonly FactorOut[],
): Array<{ family: string; label: string; factors: FactorOut[] }> {
  const buckets = new Map<string, FactorOut[]>();
  for (const factor of factors) {
    const bucket = buckets.get(factor.family) ?? [];
    bucket.push(factor);
    buckets.set(factor.family, bucket);
  }
  const known = FAMILY_ORDER.filter((family) => buckets.has(family));
  const unknown = [...buckets.keys()].filter(
    (family) => !FAMILY_ORDER.includes(family as (typeof FAMILY_ORDER)[number]),
  );
  return [...known, ...unknown].map((family) => ({
    family,
    label: FAMILY_LABELS[family] ?? family,
    factors: buckets.get(family) ?? [],
  }));
}

export function matchesQuery(factor: FactorOut, query: string): boolean {
  if (!query) return true;
  const needle = query.toLowerCase();
  return (
    factor.label.toLowerCase().includes(needle) || factor.key.toLowerCase().includes(needle)
  );
}

export function FactorCombobox({
  factors,
  value,
  onChange,
  labelledBy,
  placeholder = "Search 64 factors…",
  disabled = false,
  className,
}: FactorComboboxProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const selected = useMemo(
    () => factors.find((factor) => factor.key === value) ?? null,
    [factors, value],
  );

  const groups = useMemo(
    () =>
      groupByFamily(factors.filter((factor) => matchesQuery(factor, query))).filter(
        (group) => group.factors.length > 0,
      ),
    [factors, query],
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-labelledby={labelledBy}
          disabled={disabled}
          className={cn("w-full justify-between font-normal", className)}
        >
          <span
            className={cn("truncate", !selected && "text-muted-foreground")}
            title={selected ? (columnDisplayTooltip(selected.key) ?? selected.label) : undefined}
          >
            {selected ? columnDisplayLabel(selected.key, selected.label) : "Choose a factor"}
          </span>
          <ChevronsUpDown aria-hidden="true" className="shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-(--radix-popover-trigger-width) p-0">
        <Command shouldFilter={false}>
          <CommandInput value={query} onValueChange={setQuery} placeholder={placeholder} />
          <CommandList className="max-h-72 overflow-y-auto">
            {groups.length === 0 ? (
              <CommandEmpty className="px-3 py-6 text-center text-sm text-muted-foreground">
                No factor matches “{query}”.
              </CommandEmpty>
            ) : null}
            {groups.map((group) => (
              <CommandGroup key={group.family} heading={group.label}>
                {group.factors.map((factor) => (
                  <CommandItem
                    key={factor.key}
                    value={factor.key}
                    onSelect={() => {
                      onChange(factor.key);
                      setOpen(false);
                    }}
                  >
                    <Check
                      aria-hidden="true"
                      className={cn(
                        "size-4 shrink-0",
                        factor.key === value ? "opacity-100" : "opacity-0",
                      )}
                    />
                    <span
                      className="truncate"
                      title={columnDisplayTooltip(factor.key) ?? factor.label}
                    >
                      {columnDisplayLabel(factor.key, factor.label)}
                      <span className="sr-only"> {factor.label}</span>
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
