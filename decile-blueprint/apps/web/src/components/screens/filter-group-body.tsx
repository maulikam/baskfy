"use client";

import type { FactorOut, ScreenDefinition } from "@baskfy/api-client";

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

/** Shared context for rendering one filter accordion / chip popover body. */
export interface FilterGroupContext {
  definition: ScreenDefinition;
  patch: Patch;
  disabled: boolean;
  factors: readonly FactorOut[];
  operands: readonly { key: string; label: string }[];
  tradingDays: readonly string[];
  dataStartDate: string | null;
  latestDate: string | null;
}

/** Renders the body of one FILTER_GROUPS entry — used by the rail and the chip bar. */
export function renderFilterGroup(id: string, ctx: FilterGroupContext): React.ReactNode {
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
