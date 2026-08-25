"use client";

import type { FactorOut, ScreenDefinition, UniverseOut } from "@baskfy/api-client";
import { RotateCcw } from "lucide-react";
import { useState } from "react";

import { FactorCombobox } from "@/components/data/factor-combobox";
import { FilterAccordion, type FilterSection } from "@/components/data/filter-accordion";
import {
  renderFilterGroup,
  type FilterGroupContext,
} from "@/components/screens/filter-group-body";
import type { Patch } from "@/components/screens/filter-sections";
import { Field, SwitchRow } from "@/components/screens/field";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { FILTER_GROUPS, totalActiveFilters } from "@/lib/screens/groups";
import { cn } from "@/lib/utils";

/**
 * Classic left-rail filter form — kept as the fallback when
 * `NEXT_PUBLIC_SCREEN_CHIP_FILTERS=0`.
 */
export interface FilterFormProps {
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

export function FilterForm({
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
}: FilterFormProps) {
  const [showMore, setShowMore] = useState(true);

  const ctx: FilterGroupContext = {
    definition,
    patch,
    disabled,
    factors,
    operands,
    tradingDays,
    dataStartDate,
    latestDate,
  };

  const sections: FilterSection[] = FILTER_GROUPS.map((group) => ({
    id: group.id,
    title: group.title,
    activeCount: group.activeCount(definition),
    content: renderFilterGroup(group.id, ctx),
  }));

  const active = totalActiveFilters(definition);

  return (
    <form
      className={cn("flex min-h-0 flex-col", className)}
      onSubmit={(event) => event.preventDefault()}
      aria-label="Screen filters"
    >
      <div className="space-y-4 border-b border-border pb-4">
        <Field
          label="Index Universe"
          render={({ id }) => (
            <Select
              id={id}
              disabled={disabled}
              value={definition.index}
              data-testid="index-select"
              onChange={(event) =>
                patch({ index: event.currentTarget.value as ScreenDefinition["index"] })
              }
            >
              {universes.map((universe) => (
                <option key={universe.slug} value={universe.slug}>
                  {universe.name}
                </option>
              ))}
            </Select>
          )}
        />

        <div className="space-y-1.5">
          <Label id="sort-by-label">Sort By (Factor)</Label>
          <FactorCombobox
            factors={factors}
            value={definition.sort_by}
            labelledBy="sort-by-label"
            disabled={disabled}
            onChange={(key) => patch({ sort_by: key })}
          />
        </div>

        <Field
          label="Sort Direction"
          render={({ id }) => (
            <Select
              id={id}
              disabled={disabled}
              value={definition.sort_direction}
              data-testid="sort-direction"
              onChange={(event) =>
                patch({ sort_direction: event.currentTarget.value as "asc" | "desc" })
              }
            >
              <option value="desc">Highest to Lowest</option>
              <option value="asc">Lowest to Highest</option>
            </Select>
          )}
        />

        <SwitchRow
          label="Show More Filters"
          hint={active === 0 ? undefined : `${active} filter${active === 1 ? "" : "s"} active`}
          render={({ id, describedBy }) => (
            <Switch
              id={id}
              aria-describedby={describedBy}
              checked={showMore}
              onCheckedChange={setShowMore}
              data-testid="show-more-filters"
            />
          )}
        />
      </div>

      {showMore ? (
        <div className="pr-1">
          <FilterAccordion sections={sections} defaultOpen={["general"]} />
          <Button
            variant="ghost"
            size="sm"
            className="mt-4"
            disabled={disabled}
            onClick={onReset}
            data-testid="reset-filters"
          >
            <RotateCcw aria-hidden="true" />
            Reset to defaults
          </Button>
        </div>
      ) : null}
    </form>
  );
}
