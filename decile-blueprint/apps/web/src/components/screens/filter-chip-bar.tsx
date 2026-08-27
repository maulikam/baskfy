"use client";

import type { FactorOut, ScreenDefinition, UniverseOut } from "@baskfy/api-client";
import { ChevronDown, Plus, RotateCcw, X } from "lucide-react";
import * as React from "react";
import { useMemo, useState } from "react";

import { FactorList } from "@/components/data/factor-combobox";
import {
  renderFilterGroup,
  type FilterGroupContext,
} from "@/components/screens/filter-group-body";
import type { Patch } from "@/components/screens/filter-sections";
import { Field } from "@/components/screens/field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Select } from "@/components/ui/select";
import { columnDisplayLabel } from "@/lib/screens/column-display";
import { defaultDefinition } from "@/lib/screens/defaults";
import { FILTER_GROUPS } from "@/lib/screens/groups";
import { universeChipLabel, universeLabelFromName } from "@/lib/screens/universe-label";
import { cn } from "@/lib/utils";

export interface FilterChipBarProps {
  definition: ScreenDefinition;
  patch: Patch;
  factors: readonly FactorOut[];
  universes: readonly UniverseOut[];
  operands: readonly { key: string; label: string }[];
  tradingDays: readonly string[];
  dataStartDate: string | null;
  latestDate: string | null;
  disabled?: boolean;
  onReset: () => void;
  className?: string | undefined;
}

const CHIP_SHORT: Readonly<Record<string, string>> = {
  general: "Liquidity & return",
  "moving-average": "Moving averages",
  "away-from-high": "Away from high",
  "positive-days": "Positive days",
  circuits: "Circuits",
  marketcap: "Marketcap",
  pe: "P/E",
  series: "Series",
  risk: "Risk caps",
  price: "Price",
  "multi-factor": "Multi-factor",
  historical: "As-of date",
  custom: "Custom",
};

/** Copied from GeneralFilters — this file must not import filter-sections. */
const MEDIAN_VOLUME_PRESETS = [
  { value: 1_000_000, label: "₹10 lakh" },
  { value: 2_000_000, label: "₹20 lakh" },
  { value: 5_000_000, label: "₹50 lakh" },
  { value: 10_000_000, label: "₹1 crore" },
  { value: 20_000_000, label: "₹2 crore" },
  { value: 50_000_000, label: "₹5 crore" },
  { value: 100_000_000, label: "₹10 crore" },
] as const;

function compactRupee(volume: number): string | null {
  if (!Number.isFinite(volume) || volume <= 0) return null;
  if (volume >= 10_000_000 && volume % 10_000_000 === 0) {
    return `₹${volume / 10_000_000} crore`;
  }
  if (volume >= 100_000 && volume % 100_000 === 0) {
    return `₹${volume / 100_000} lakh`;
  }
  return null;
}

function liquidityChipValue(volume: number | null): string {
  if (volume === null) return "any";
  const preset = MEDIAN_VOLUME_PRESETS.find((p) => p.value === volume);
  if (preset) return preset.label;
  return compactRupee(volume) ?? "custom";
}

/** Volume lives on the Liquidity chip, so the general filled chip ignores it. */
function generalChipVisible(definition: ScreenDefinition): boolean {
  return definition.apply_filters_on !== "all" || definition.min_return_1y !== null;
}

function generalChipActiveCount(definition: ScreenDefinition): number {
  return (
    (definition.apply_filters_on === "all" ? 0 : 1) +
    (definition.min_return_1y === null ? 0 : 1)
  );
}

const ChipButton = React.forwardRef<
  HTMLButtonElement,
  {
    children: React.ReactNode;
    active?: boolean;
    disabled?: boolean;
    "aria-expanded"?: boolean;
    "data-testid"?: string;
    className?: string;
  }
>(function ChipButton(
  {
    children,
    active = false,
    disabled,
    "aria-expanded": ariaExpanded,
    "data-testid": testId,
    className,
    ...rest
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled}
      aria-expanded={ariaExpanded}
      data-testid={testId}
      className={cn(
        "inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full px-3.5 text-xs font-medium outline-none",
        "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        "motion-safe:transition-[transform,opacity,background-color,border-color,box-shadow] motion-safe:duration-150",
        active
          ? "border-transparent bg-foreground text-background shadow-sm"
          : "vaaya-pill border-border text-foreground hover:shadow-md",
        disabled && "pointer-events-none opacity-50",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
});
ChipButton.displayName = "ChipButton";

export function FilterChipBar({
  definition,
  patch,
  factors,
  universes,
  operands,
  tradingDays,
  dataStartDate,
  latestDate,
  disabled = false,
  onReset,
  className,
}: FilterChipBarProps) {
  const [openChip, setOpenChip] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addQuery, setAddQuery] = useState("");
  /** Group opened from "+ Filter" that is not yet active (no filled chip). */
  const [draftGroup, setDraftGroup] = useState<string | null>(null);

  const groupCtx: FilterGroupContext = {
    definition,
    patch,
    disabled,
    factors,
    operands,
    tradingDays,
    dataStartDate,
    latestDate,
  };

  /* `/meta/universes` is its own fetch, so `universes` is empty on the first paint of every
     visit. The label is derived from the slug until it lands and is the same string afterwards,
     which is why nothing here waits on `universes.length`. */
  const universeLabel = universeChipLabel(definition.index, universes);

  const sortFactor = factors.find((f) => f.key === definition.sort_by);
  const sortLabel = columnDisplayLabel(
    definition.sort_by,
    sortFactor?.label ?? definition.sort_by,
  );

  const directionLabel =
    definition.sort_direction === "desc" ? "High → Low" : "Low → High";

  const volumePreset = MEDIAN_VOLUME_PRESETS.find(
    (p) => p.value === definition.median_volume_1y,
  );
  const volumeIsCustom =
    definition.median_volume_1y !== null && volumePreset === undefined;
  const liquidityValue = liquidityChipValue(definition.median_volume_1y);

  const activeGroups = useMemo(
    () =>
      FILTER_GROUPS.filter((g) => {
        if (g.id === "general") return generalChipVisible(definition);
        return g.activeCount(definition) > 0;
      }),
    [definition],
  );

  const addCandidates = useMemo(() => {
    const needle = addQuery.trim().toLowerCase();
    return FILTER_GROUPS.filter((g) => {
      if (!needle) return true;
      return (
        g.title.toLowerCase().includes(needle) ||
        (CHIP_SHORT[g.id] ?? "").toLowerCase().includes(needle) ||
        g.id.includes(needle)
      );
    });
  }, [addQuery]);

  const draftMeta = draftGroup
    ? FILTER_GROUPS.find((g) => g.id === draftGroup)
    : undefined;

  function clearGroup(groupId: string) {
    const group = FILTER_GROUPS.find((g) => g.id === groupId);
    if (!group) return;
    const defaults = defaultDefinition();
    const partial: Partial<ScreenDefinition> = {};
    for (const field of group.fields) {
      /* Liquidity owns median_volume_1y; clearing "Liquidity & return" must not wipe it. */
      if (groupId === "general" && field === "median_volume_1y") continue;
      (partial as Record<string, unknown>)[field] = defaults[field];
    }
    patch(partial);
  }

  function groupChipCount(groupId: string): number {
    if (groupId === "general") return generalChipActiveCount(definition);
    const group = FILTER_GROUPS.find((g) => g.id === groupId);
    return group ? group.activeCount(definition) : 0;
  }

  return (
    <div
      role="toolbar"
      aria-label="Screen filters"
      data-testid="filter-chip-bar"
      className={cn(
        "vaaya-pill-bar flex items-center gap-1.5 overflow-x-auto px-2 py-1.5 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
        className,
      )}
    >
      <Popover
        open={openChip === "index"}
        onOpenChange={(open) => setOpenChip(open ? "index" : null)}
      >
        <PopoverTrigger asChild>
          <ChipButton
            active
            disabled={disabled}
            aria-expanded={openChip === "index"}
            data-testid="chip-index"
          >
            {/* The only text node in the button, so the accessible name is this exact string —
                the chevron is aria-hidden. */}
            <span className="max-w-[10rem] truncate">{universeLabel}</span>
            <ChevronDown aria-hidden="true" className="size-3.5 opacity-70" />
          </ChipButton>
        </PopoverTrigger>
        <PopoverContent className="w-72 p-3" align="start">
          <Field
            label="Index Universe"
            render={({ id }) => (
              <Select
                id={id}
                disabled={disabled}
                value={definition.index}
                data-testid="index-select"
                onChange={(event) => {
                  patch({ index: event.currentTarget.value as ScreenDefinition["index"] });
                  setOpenChip(null);
                }}
              >
                {universes.length === 0 ? (
                  /* Opened before `/meta/universes` answered: a select with no option at all
                     would render blank against a definition that does have an index. */
                  <option value={definition.index}>{universeLabel}</option>
                ) : (
                  universes.map((universe) => (
                    <option key={universe.slug} value={universe.slug}>
                      {universeLabelFromName(universe.name)}
                    </option>
                  ))
                )}
              </Select>
            )}
          />
        </PopoverContent>
      </Popover>

      <Popover
        open={openChip === "sort"}
        onOpenChange={(open) => setOpenChip(open ? "sort" : null)}
      >
        <PopoverTrigger asChild>
          <ChipButton
            disabled={disabled}
            aria-expanded={openChip === "sort"}
            data-testid="chip-sort"
          >
            <span className="text-muted-foreground">Sorted by:</span>
            <span className="max-w-[9rem] truncate">{sortLabel}</span>
            <ChevronDown aria-hidden="true" className="size-3.5 opacity-70" />
          </ChipButton>
        </PopoverTrigger>
        {/*
          The list renders **inline**, not as a second popover inside this one.

          `FactorCombobox` opens a popover of its own, and a Radix popover portals its content to
          `document.body` — outside this popover's DOM subtree. Every click in that nested list was
          therefore an *outside* click as far as this chip was concerned: the chip closed, the list
          unmounted with it, and the factors appeared and vanished before one could be picked. It
          was the only place in the app that nested them, which is why the same control has always
          worked in the filter panel.

          `w-96` rather than `w-80`: the list is now inside this width instead of sizing itself to
          its own trigger, and factor labels like "12-month return, skipping the last month" need
          the room. `FactorList` bounds its own height and scrolls.
        */}
        <PopoverContent className="w-96 p-0" align="start">
          <p
            id="chip-sort-by-label"
            className="border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground"
          >
            Sort By (Factor)
          </p>
          <FactorList
            factors={factors}
            value={definition.sort_by}
            onChange={(key) => {
              patch({ sort_by: key });
              setOpenChip(null);
            }}
          />
        </PopoverContent>
      </Popover>

      <Popover
        open={openChip === "direction"}
        onOpenChange={(open) => setOpenChip(open ? "direction" : null)}
      >
        <PopoverTrigger asChild>
          <ChipButton
            disabled={disabled}
            aria-expanded={openChip === "direction"}
            data-testid="chip-direction"
          >
            {directionLabel}
            <ChevronDown aria-hidden="true" className="size-3.5 opacity-70" />
          </ChipButton>
        </PopoverTrigger>
        <PopoverContent className="w-56 p-3" align="start">
          <Field
            label="Sort Direction"
            render={({ id }) => (
              <Select
                id={id}
                disabled={disabled}
                value={definition.sort_direction}
                data-testid="sort-direction"
                onChange={(event) => {
                  patch({
                    sort_direction: event.currentTarget.value as "asc" | "desc",
                  });
                  setOpenChip(null);
                }}
              >
                <option value="desc">High → Low</option>
                <option value="asc">Low → High</option>
              </Select>
            )}
          />
        </PopoverContent>
      </Popover>

      <Popover
        open={openChip === "liquidity"}
        onOpenChange={(open) => setOpenChip(open ? "liquidity" : null)}
      >
        <PopoverTrigger asChild>
          <ChipButton
            active={definition.median_volume_1y !== null}
            disabled={disabled}
            aria-expanded={openChip === "liquidity"}
            data-testid="chip-liquidity"
          >
            <span>{`Liquidity: ${liquidityValue}`}</span>
            <ChevronDown aria-hidden="true" className="size-3.5 opacity-70" />
          </ChipButton>
        </PopoverTrigger>
        <PopoverContent className="w-72 p-3" align="start">
          <Field
            label="Median volume, 1 year"
            hint="The median daily traded value over the last year, in rupees."
            off={definition.median_volume_1y === null}
            render={({ id, describedBy }) => (
              <Select
                id={id}
                aria-describedby={describedBy}
                disabled={disabled}
                value={volumeIsCustom ? "custom" : (volumePreset?.value.toString() ?? "")}
                data-testid="chip-liquidity-select"
                onChange={(event) => {
                  const raw = event.currentTarget.value;
                  if (raw === "") {
                    patch({ median_volume_1y: null });
                    setOpenChip(null);
                  } else if (raw === "custom") {
                    patch({ median_volume_1y: definition.median_volume_1y ?? 0 });
                  } else {
                    patch({ median_volume_1y: Number(raw) });
                    setOpenChip(null);
                  }
                }}
              >
                <option value="">No minimum</option>
                {MEDIAN_VOLUME_PRESETS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
                <option value="custom">Custom</option>
              </Select>
            )}
          />
          {volumeIsCustom ? (
            <Field
              label="Custom median volume (₹)"
              render={({ id }) => (
                <Input
                  id={id}
                  type="number"
                  inputMode="numeric"
                  disabled={disabled}
                  className="tnum"
                  data-testid="chip-liquidity-custom"
                  value={String(definition.median_volume_1y ?? 0)}
                  onChange={(event) =>
                    patch({ median_volume_1y: Number(event.currentTarget.value) })
                  }
                />
              )}
            />
          ) : null}
        </PopoverContent>
      </Popover>

      {activeGroups.map((group) => {
        const filledCount = groupChipCount(group.id);
        return (
          <Popover
            key={group.id}
            open={openChip === group.id}
            onOpenChange={(open) => setOpenChip(open ? group.id : null)}
          >
          <div className="inline-flex items-center motion-safe:animate-in motion-safe:fade-in motion-safe:zoom-in-95 motion-safe:duration-150">
            <PopoverTrigger asChild>
              <ChipButton
                active
                disabled={disabled}
                aria-expanded={openChip === group.id}
                data-testid={`chip-filter-${group.id}`}
                className="rounded-r-none border-r-0 pr-2"
              >
                <span className="max-w-[8rem] truncate">
                  {CHIP_SHORT[group.id] ?? group.title}
                  {filledCount > 1 ? ` · ${filledCount}` : ""}
                </span>
              </ChipButton>
            </PopoverTrigger>
            <button
              type="button"
              disabled={disabled}
              aria-label={`Clear ${group.title}`}
              data-testid={`chip-clear-${group.id}`}
              className={cn(
                "inline-flex h-8 items-center rounded-r-full border border-l-0 border-foreground/20 bg-foreground px-2 text-background",
                "focus-visible:ring-2 focus-visible:ring-ring",
                disabled && "pointer-events-none opacity-50",
              )}
              onClick={(event) => {
                event.preventDefault();
                clearGroup(group.id);
                if (openChip === group.id) setOpenChip(null);
              }}
            >
              <X aria-hidden="true" className="size-3.5" />
            </button>
          </div>
          <PopoverContent
            className="max-h-[min(24rem,70vh)] w-80 overflow-y-auto p-3"
            align="start"
          >
            <p className="mb-2 text-xs font-medium text-muted-foreground">{group.title}</p>
            {renderFilterGroup(group.id, groupCtx)}
          </PopoverContent>
        </Popover>
        );
      })}

      <Popover open={addOpen} onOpenChange={setAddOpen}>
        <PopoverTrigger asChild>
          <ChipButton disabled={disabled} aria-expanded={addOpen} data-testid="chip-add-filter">
            <Plus aria-hidden="true" className="size-3.5" />
            Filter
          </ChipButton>
        </PopoverTrigger>
        <PopoverContent className="w-80 p-0" align="start">
          <Command shouldFilter={false}>
            <CommandInput
              value={addQuery}
              onValueChange={setAddQuery}
              placeholder="Search filters…"
              data-testid="filter-search"
            />
            <CommandList className="max-h-72 overflow-y-auto">
              {addCandidates.length === 0 ? (
                <CommandEmpty className="px-3 py-6 text-center text-sm text-muted-foreground">
                  No filter matches “{addQuery}”.
                </CommandEmpty>
              ) : (
                <CommandGroup heading="All filters">
                  {addCandidates.map((group) => {
                    const active = groupChipCount(group.id);
                    return (
                      <CommandItem
                        key={group.id}
                        value={group.id}
                        onSelect={() => {
                          setAddOpen(false);
                          setAddQuery("");
                          if (active > 0) {
                            setOpenChip(group.id);
                            setDraftGroup(null);
                          } else {
                            setDraftGroup(group.id);
                            setOpenChip(null);
                          }
                        }}
                      >
                        <span className="flex-1 truncate">{group.title}</span>
                        {active > 0 ? (
                          <span className="text-xs text-muted-foreground tabular-nums">
                            {active}
                          </span>
                        ) : null}
                      </CommandItem>
                    );
                  })}
                </CommandGroup>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>

      <Dialog
        open={draftGroup !== null}
        onOpenChange={(open) => {
          if (!open) setDraftGroup(null);
        }}
      >
        <DialogContent className="max-h-[min(28rem,80vh)] max-w-md overflow-y-auto" data-testid="draft-filter-dialog">
          <DialogTitle className="text-base font-semibold">
            {draftMeta?.title ?? "Filter"}
          </DialogTitle>
          <DialogDescription className="sr-only">
            Configure {draftMeta?.title ?? "this filter"} for the screen.
          </DialogDescription>
          {draftMeta ? renderFilterGroup(draftMeta.id, groupCtx) : null}
        </DialogContent>
      </Dialog>

      <Button
        variant="ghost"
        size="sm"
        className="h-8 shrink-0 rounded-full px-2 text-xs"
        disabled={disabled}
        onClick={onReset}
        data-testid="reset-filters"
      >
        <RotateCcw aria-hidden="true" className="size-3.5" />
        Reset
      </Button>
    </div>
  );
}
