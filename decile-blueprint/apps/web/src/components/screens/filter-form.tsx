"use client";

import type { FactorOut, ScreenDefinition, UniverseOut } from "@decile/api-client";
import { RotateCcw } from "lucide-react";
import { useState } from "react";

import { FactorCombobox } from "@/components/data/factor-combobox";
import { FilterAccordion, type FilterSection } from "@/components/data/filter-accordion";
import {
  AwayFromHighFilters,
  CircuitFilters,
  CustomFilters,
  GeneralFilters,
  MovingAverageFilters,
  MultiFactorFilters,
  PeFilter,
  PositiveDaysFilters,
  RangeFilter,
  RiskFilters,
  SeriesFilter,
  type Patch,
} from "@/components/screens/filter-sections";
import { HistoricalRanks } from "@/components/screens/historical-ranks";
import { Field, SwitchRow } from "@/components/screens/field";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { FILTER_GROUPS, totalActiveFilters } from "@/lib/screens/groups";
import { cn } from "@/lib/utils";

/**
 * The left pane — docs/08 §"Screen editor":
 *
 *     "Two-pane layout: **filter form (left, ~380px, scrollable, sticky actions)** + results
 *      (right, fills)."
 *
 *     "1. Always visible: Index Universe · Sort By (Factor) · Sort Direction · `Show More Filters`"
 *
 * The group order and the active-count logic live in `src/lib/screens/groups.ts`, where a test
 * pins them against the document. This file renders them.
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
  // docs/08 lists `Show More Filters` among the always-visible controls; it is the reference
  // product's own affordance for keeping a twenty-field form calm on first sight.
  const [showMore, setShowMore] = useState(true);

  const sections: FilterSection[] = FILTER_GROUPS.map((group) => ({
    id: group.id,
    title: group.title,
    activeCount: group.activeCount(definition),
    content: renderGroup(group.id, {
      definition,
      patch,
      disabled,
      factors,
      operands,
      tradingDays,
      dataStartDate,
      latestDate,
    }),
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
        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
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

interface GroupContext {
  definition: ScreenDefinition;
  patch: Patch;
  disabled: boolean;
  factors: readonly FactorOut[];
  operands: readonly { key: string; label: string }[];
  tradingDays: readonly string[];
  dataStartDate: string | null;
  latestDate: string | null;
}

function renderGroup(id: string, ctx: GroupContext): React.ReactNode {
  const { definition, patch, disabled } = ctx;
  switch (id) {
    case "general":
      return <GeneralFilters definition={definition} patch={patch} disabled={disabled} />;
    case "moving-average":
      return <MovingAverageFilters definition={definition} patch={patch} disabled={disabled} />;
    case "away-from-high":
      return <AwayFromHighFilters definition={definition} patch={patch} disabled={disabled} />;
    case "positive-days":
      return <PositiveDaysFilters definition={definition} patch={patch} disabled={disabled} />;
    case "circuits":
      return <CircuitFilters definition={definition} patch={patch} disabled={disabled} />;
    case "marketcap":
      return (
        <RangeFilter
          definition={definition}
          patch={patch}
          disabled={disabled}
          field="marketcap"
          label="Marketcap range"
          hint="Inclusive, in ₹ crore, applied within the selected index."
          unit="₹ cr"
        />
      );
    case "pe":
      return <PeFilter definition={definition} patch={patch} disabled={disabled} />;
    case "series":
      return <SeriesFilter definition={definition} patch={patch} disabled={disabled} />;
    case "risk":
      return <RiskFilters definition={definition} patch={patch} disabled={disabled} />;
    case "price":
      return (
        <RangeFilter
          definition={definition}
          patch={patch}
          disabled={disabled}
          field="price"
          label="Price (CMP) range"
          hint="Inclusive, on the last exchange close."
          unit="₹"
        />
      );
    case "multi-factor":
      return (
        <MultiFactorFilters
          definition={definition}
          patch={patch}
          disabled={disabled}
          factors={ctx.factors}
        />
      );
    case "historical":
      return (
        <HistoricalRanks
          definition={definition}
          patch={patch}
          disabled={disabled}
          tradingDays={ctx.tradingDays}
          dataStartDate={ctx.dataStartDate}
          latestDate={ctx.latestDate}
        />
      );
    case "custom":
      return (
        <CustomFilters
          definition={definition}
          patch={patch}
          disabled={disabled}
          operands={ctx.operands}
        />
      );
    default:
      return null;
  }
}
