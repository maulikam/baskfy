"use client";

import { useId } from "react";

import { CheckboxGroup, NumberField, SELECT_CLASS } from "@/components/opportunities/fields";
import { SETUP_LABEL, label } from "@/lib/opportunities/view";
import type { OpportunitiesMeta, SwingScanRequest } from "@/lib/opportunities/types";

type NumericKey = {
  [K in keyof SwingScanRequest]: SwingScanRequest[K] extends number ? K : never;
}[keyof SwingScanRequest];

export function SwingFilters({
  meta,
  value,
  onChange,
}: {
  meta: OpportunitiesMeta;
  value: SwingScanRequest;
  onChange: (next: SwingScanRequest) => void;
}) {
  const universeId = useId();
  const sectorId = useId();
  const num = (key: NumericKey) => (next: number | null) =>
    onChange({ ...value, [key]: next ?? meta.swing_defaults[key] });

  return (
    <div className="grid gap-4 rounded-lg border border-border/70 bg-card p-4" data-testid="swing-filters">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor={universeId} className="text-xs font-medium text-muted-foreground">
            Universe
          </label>
          <select
            id={universeId}
            className={SELECT_CLASS}
            value={value.universe}
            onChange={(event) => onChange({ ...value, universe: event.target.value })}
          >
            {meta.universes.map((universe) => (
              <option key={universe.slug} value={universe.slug}>
                {universe.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={sectorId} className="text-xs font-medium text-muted-foreground">
            Sectors <span className="text-quiet-foreground">(none selected = all)</span>
          </label>
          <select
            id={sectorId}
            multiple
            size={4}
            className={`${SELECT_CLASS} h-auto`}
            value={value.sectors}
            onChange={(event) =>
              onChange({
                ...value,
                sectors: Array.from(event.target.selectedOptions, (option) => option.value),
              })
            }
          >
            {meta.sectors.map((sector) => (
              <option key={sector.slug} value={sector.slug}>
                {sector.name}
              </option>
            ))}
          </select>
        </div>
      </div>
      <CheckboxGroup
        legend="Setup types"
        options={meta.setups.map((setup) => ({ value: setup, label: label(SETUP_LABEL, setup) }))}
        selected={value.setups}
        onChange={(setups) => onChange({ ...value, setups })}
      />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <NumberField label="Min turnover" suffix="₹ cr" step={0.5} min={0} value={value.min_turnover_cr} onChange={num("min_turnover_cr")} />
        <NumberField label="Min price" suffix="₹" min={0} value={value.min_price} onChange={num("min_price")} />
        <NumberField label="Horizon min" suffix="sessions" min={1} value={value.horizon_min} onChange={num("horizon_min")} />
        <NumberField label="Horizon max" suffix="sessions" min={1} value={value.horizon_max} onChange={num("horizon_max")} />
        <NumberField label="ATR% min" step={0.1} min={0} value={value.atr_pct_min} onChange={num("atr_pct_min")} />
        <NumberField label="ATR% max" step={0.1} min={0} value={value.atr_pct_max} onChange={num("atr_pct_max")} />
        <NumberField label="Min R:R" step={0.1} min={0} value={value.min_rr} onChange={num("min_rr")} />
        <NumberField
          label="Limit"
          min={1}
          max={meta.limits.max_results}
          value={value.limit}
          onChange={num("limit")}
        />
      </div>
    </div>
  );
}
