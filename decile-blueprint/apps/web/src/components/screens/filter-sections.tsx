"use client";

import {
  AWAY_FROM_HIGH_IGNORE,
  CIRCUITS_IGNORE_ABOVE,
  IGNORE_ABOVE_BETA_IGNORE,
  MAX_CUSTOM_FILTERS,
  POSITIVE_DAYS_IGNORE,
  SERIES_VALUES,
  type FactorOut,
  type ScreenDefinition,
} from "@decile/api-client";
import { Plus, Trash2 } from "lucide-react";

import { FactorCombobox } from "@/components/data/factor-combobox";
import { SentinelNumberInput } from "@/components/data/sentinel-number-input";
import { Field, SwitchRow } from "@/components/screens/field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  CIRCUITS_OFF_VALUE,
  MA_WINDOWS,
  WINDOW_KEYS,
  WINDOW_LABELS,
  rangeActive,
  type WindowKey,
} from "@/lib/screens/defaults";
import { cn } from "@/lib/utils";

/**
 * The bodies of the accordion groups — every field of docs/01 §2.2 through §2.14, in the
 * reference product's order, with the reference product's own microcopy.
 *
 * The sentinel wording is quoted, not paraphrased. docs/08 §"Screen editor": "Sentinel values are
 * explained inline **exactly as the reference does** ('Keep value as 100 if you want to
 * ignore…')". A user who has met the original product should read the same sentence here.
 */

export type Patch = (patch: Partial<ScreenDefinition>) => void;

export interface SectionProps {
  definition: ScreenDefinition;
  patch: Patch;
  disabled?: boolean;
}

// --- §2.2 General Filters ---------------------------------------------------

/** docs/01 §2.2: "All stocks / Top 1–5 deciles / Top 50 / Top 100 **of the selected index**". */
const APPLY_FILTERS_ON_OPTIONS = [
  { value: "all", label: "All stocks of the selected index" },
  { value: "decile_1", label: "Top 1 decile of the selected index" },
  { value: "decile_2", label: "Top 2 deciles of the selected index" },
  { value: "decile_3", label: "Top 3 deciles of the selected index" },
  { value: "decile_4", label: "Top 4 deciles of the selected index" },
  { value: "decile_5", label: "Top 5 deciles of the selected index" },
  { value: "top_50", label: "Top 50 of the selected index" },
  { value: "top_100", label: "Top 100 of the selected index" },
] as const;

/** docs/01 §2.2: "10L, 20L, 50L, 1Cr, 2Cr, 5Cr, 10Cr, Custom", in rupees. */
const MEDIAN_VOLUME_PRESETS = [
  { value: 1_000_000, label: "₹10 lakh" },
  { value: 2_000_000, label: "₹20 lakh" },
  { value: 5_000_000, label: "₹50 lakh" },
  { value: 10_000_000, label: "₹1 crore" },
  { value: 20_000_000, label: "₹2 crore" },
  { value: 50_000_000, label: "₹5 crore" },
  { value: 100_000_000, label: "₹10 crore" },
] as const;

export function GeneralFilters({ definition, patch, disabled }: SectionProps) {
  const preset = MEDIAN_VOLUME_PRESETS.find((p) => p.value === definition.median_volume_1y);
  const isCustom = definition.median_volume_1y !== null && preset === undefined;

  return (
    <>
      <Field
        label="Apply filters on"
        hint="Ranks the index by marketcap first, then applies every other filter inside that slice."
        render={({ id, describedBy }) => (
          <Select
            id={id}
            aria-describedby={describedBy}
            disabled={disabled}
            value={definition.apply_filters_on}
            onChange={(event) =>
              patch({
                apply_filters_on: event.currentTarget
                  .value as ScreenDefinition["apply_filters_on"],
              })
            }
          >
            {APPLY_FILTERS_ON_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        )}
      />

      <Field
        label="Minimum 1 year return (%)"
        hint="Leave blank to ignore. Instruments listed less than a year ago have no 1-year return and are excluded by any 1-year filter."
        off={definition.min_return_1y === null}
        render={({ id, describedBy }) => (
          <Input
            id={id}
            aria-describedby={describedBy}
            type="number"
            inputMode="decimal"
            disabled={disabled}
            className="tnum"
            value={definition.min_return_1y === null ? "" : String(definition.min_return_1y)}
            onChange={(event) =>
              patch({
                min_return_1y: event.currentTarget.value === "" ? null : event.currentTarget.value,
              })
            }
          />
        )}
      />

      <Field
        label="Median volume, 1 year"
        hint="The median daily traded value over the last year, in rupees."
        off={definition.median_volume_1y === null}
        render={({ id, describedBy }) => (
          <Select
            id={id}
            aria-describedby={describedBy}
            disabled={disabled}
            value={isCustom ? "custom" : (preset?.value.toString() ?? "")}
            onChange={(event) => {
              const raw = event.currentTarget.value;
              if (raw === "") patch({ median_volume_1y: null });
              else if (raw === "custom") patch({ median_volume_1y: definition.median_volume_1y ?? 0 });
              else patch({ median_volume_1y: Number(raw) });
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

      {isCustom ? (
        <Field
          label="Custom median volume (₹)"
          render={({ id }) => (
            <Input
              id={id}
              type="number"
              inputMode="numeric"
              disabled={disabled}
              className="tnum"
              value={String(definition.median_volume_1y ?? 0)}
              onChange={(event) => patch({ median_volume_1y: Number(event.currentTarget.value) })}
            />
          )}
        />
      ) : null}
    </>
  );
}

// --- §2.3 Moving Average Filters --------------------------------------------

export function MovingAverageFilters({ definition, patch, disabled }: SectionProps) {
  const ma = definition.moving_average;
  const set = (key: keyof ScreenDefinition["moving_average"], value: boolean) =>
    patch({ moving_average: { ...ma, [key]: value } });

  return (
    <>
      <SwitchRow
        label="Apply Moving Average Filters"
        hint="Eight independent switches, all AND-combined."
        render={({ id, describedBy }) => (
          <Switch
            id={id}
            aria-describedby={describedBy}
            disabled={disabled}
            checked={ma.enabled}
            onCheckedChange={(checked) => set("enabled", checked)}
          />
        )}
      />
      <div
        className={cn(
          "grid grid-cols-2 gap-x-4 gap-y-2",
          !ma.enabled && "pointer-events-none opacity-50",
        )}
      >
        {MA_WINDOWS.map((window) => (
          <div key={window} className="contents">
            <SwitchRow
              label={`Above ${window}-day MA`}
              render={({ id }) => (
                <Switch
                  id={id}
                  disabled={disabled || !ma.enabled}
                  checked={ma[`above_${window}` as const]}
                  onCheckedChange={(checked) => set(`above_${window}` as const, checked)}
                />
              )}
            />
            <SwitchRow
              label={`Below ${window}-day MA`}
              render={({ id }) => (
                <Switch
                  id={id}
                  disabled={disabled || !ma.enabled}
                  checked={ma[`below_${window}` as const]}
                  onCheckedChange={(checked) => set(`below_${window}` as const, checked)}
                />
              )}
            />
          </div>
        ))}
      </div>
    </>
  );
}

// --- §2.4 Away from High ----------------------------------------------------

export function AwayFromHighFilters({ definition, patch, disabled }: SectionProps) {
  const away = definition.away_from_high;
  return (
    <>
      <SentinelNumberInput
        label="Within % of all-time high"
        value={away.ath}
        onChange={(value) => patch({ away_from_high: { ...away, ath: value } })}
        sentinel={AWAY_FROM_HIGH_IGNORE}
        sentinelHint="Keep value as 100 if you want to ignore this filter."
        min={0}
        max={AWAY_FROM_HIGH_IGNORE}
        suffix="%"
        disabled={disabled}
      />
      <SentinelNumberInput
        label="Within % of 1 year high"
        value={away.one_year}
        onChange={(value) => patch({ away_from_high: { ...away, one_year: value } })}
        sentinel={AWAY_FROM_HIGH_IGNORE}
        sentinelHint="Keep value as 100 if you want to ignore this filter."
        min={0}
        max={AWAY_FROM_HIGH_IGNORE}
        suffix="%"
        disabled={disabled}
      />
    </>
  );
}

// --- §2.5 Percentage of Positive Days ---------------------------------------

export function PositiveDaysFilters({ definition, patch, disabled }: SectionProps) {
  const days = definition.positive_days;
  return (
    <>
      {WINDOW_KEYS.map((key: WindowKey) => (
        <SentinelNumberInput
          key={key}
          label={`Minimum positive days, ${WINDOW_LABELS[key]}`}
          value={days[key]}
          onChange={(value) => patch({ positive_days: { ...days, [key]: value } })}
          sentinel={POSITIVE_DAYS_IGNORE}
          sentinelHint="Keep value as 0 if you want to ignore this filter."
          min={0}
          max={100}
          suffix="%"
          disabled={disabled}
        />
      ))}
    </>
  );
}

// --- §2.6 Circuit Filters ---------------------------------------------------

export function CircuitFilters({ definition, patch, disabled }: SectionProps) {
  const circuits = definition.circuits;
  return (
    <>
      <p className="text-xs text-muted-foreground">
        Stocks that exceed the cap are excluded from the ranking, not merely flagged.
      </p>
      {WINDOW_KEYS.map((key: WindowKey) => (
        <SentinelNumberInput
          key={key}
          label={`Maximum circuit days, ${WINDOW_LABELS[key]}`}
          value={circuits[key]}
          onChange={(value) => patch({ circuits: { ...circuits, [key]: value } })}
          sentinel={CIRCUITS_IGNORE_ABOVE}
          mode="above"
          sentinelHint={`Keep the value above ${CIRCUITS_IGNORE_ABOVE} if you want to ignore this filter — a year has fewer trading days than that.`}
          min={0}
          disabled={disabled}
        />
      ))}
      <Button
        variant="ghost"
        size="sm"
        disabled={disabled}
        onClick={() =>
          patch({
            circuits: {
              m12: CIRCUITS_OFF_VALUE,
              m9: CIRCUITS_OFF_VALUE,
              m6: CIRCUITS_OFF_VALUE,
              m3: CIRCUITS_OFF_VALUE,
              m1: CIRCUITS_OFF_VALUE,
            },
          })
        }
      >
        Ignore all circuit filters
      </Button>
    </>
  );
}

// --- §2.7 / §2.11 Ranges ----------------------------------------------------

interface RangeProps extends SectionProps {
  field: "marketcap" | "price";
  label: string;
  hint: string;
  unit: string;
}

export function RangeFilter({ definition, patch, disabled, field, label, hint, unit }: RangeProps) {
  const range = definition[field];
  const update = (side: "from" | "to", raw: string) =>
    patch({ [field]: { ...range, [side]: raw === "" ? null : raw } });

  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <div className="grid grid-cols-2 gap-2">
        <Input
          type="number"
          inputMode="decimal"
          aria-label={`${label} from (${unit})`}
          placeholder={`From (${unit})`}
          disabled={disabled}
          className="tnum"
          value={range.from === null ? "" : String(range.from)}
          onChange={(event) => update("from", event.currentTarget.value)}
        />
        <Input
          type="number"
          inputMode="decimal"
          aria-label={`${label} to (${unit})`}
          placeholder={`To (${unit})`}
          disabled={disabled}
          className="tnum"
          value={range.to === null ? "" : String(range.to)}
          onChange={(event) => update("to", event.currentTarget.value)}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        {hint}
        {rangeActive(range) ? "" : " Leave both blank to ignore."}
      </p>
    </div>
  );
}

// --- §2.8 P/E Range ---------------------------------------------------------

export function PeFilter({ definition, patch, disabled }: SectionProps) {
  const pe = definition.pe;
  return (
    <>
      <SwitchRow
        label="Apply Price to Earnings Filter"
        hint="Stocks whose P/E NSE does not publish are excluded while this is on."
        render={({ id, describedBy }) => (
          <Switch
            id={id}
            aria-describedby={describedBy}
            disabled={disabled}
            checked={pe.enabled}
            onCheckedChange={(checked) => patch({ pe: { ...pe, enabled: checked } })}
          />
        )}
      />
      <div className={cn("grid grid-cols-2 gap-2", !pe.enabled && "opacity-50")}>
        <Input
          type="number"
          inputMode="decimal"
          aria-label="Price to earnings from"
          placeholder="From"
          className="tnum"
          disabled={disabled || !pe.enabled}
          value={pe.from === null ? "" : String(pe.from)}
          onChange={(event) =>
            patch({ pe: { ...pe, from: event.currentTarget.value === "" ? null : event.currentTarget.value } })
          }
        />
        <Input
          type="number"
          inputMode="decimal"
          aria-label="Price to earnings to"
          placeholder="To"
          className="tnum"
          disabled={disabled || !pe.enabled}
          value={pe.to === null ? "" : String(pe.to)}
          onChange={(event) =>
            patch({ pe: { ...pe, to: event.currentTarget.value === "" ? null : event.currentTarget.value } })
          }
        />
      </div>
    </>
  );
}

// --- §2.9 Series ------------------------------------------------------------

const SERIES_HINTS: Record<(typeof SERIES_VALUES)[number], string> = {
  EQ: "Delivery and intraday.",
  BE: "Delivery only (trade-to-trade).",
};

export function SeriesFilter({ definition, patch, disabled }: SectionProps) {
  const toggle = (value: (typeof SERIES_VALUES)[number], checked: boolean) => {
    const next = checked
      ? [...definition.series, value]
      : definition.series.filter((entry) => entry !== value);
    patch({ series: next.length === 0 ? definition.series : next });
  };

  return (
    <>
      {SERIES_VALUES.map((value) => (
        <SwitchRow
          key={value}
          label={value}
          hint={SERIES_HINTS[value]}
          render={({ id, describedBy }) => (
            <Switch
              id={id}
              aria-describedby={describedBy}
              disabled={disabled}
              checked={definition.series.includes(value)}
              onCheckedChange={(checked) => toggle(value, checked)}
            />
          )}
        />
      ))}
      <p className="text-xs text-muted-foreground">
        At least one series stays selected — a screen with neither would match nothing.
      </p>
    </>
  );
}

// --- §2.10 Ignore Top Beta / Volatility -------------------------------------

export function RiskFilters({ definition, patch, disabled }: SectionProps) {
  return (
    <>
      <SwitchRow
        label="Ignore Top Beta Stocks"
        hint="Excludes the highest-beta decile of the selected index, computed nightly across the whole universe."
        render={({ id, describedBy }) => (
          <Switch
            id={id}
            aria-describedby={describedBy}
            disabled={disabled}
            checked={definition.ignore_top_beta.enabled}
            onCheckedChange={(checked) =>
              patch({ ignore_top_beta: { ...definition.ignore_top_beta, enabled: checked } })
            }
          />
        )}
      />
      <SwitchRow
        label="Ignore Top Volatility Stocks"
        hint="The same cut, by 1-year volatility."
        render={({ id, describedBy }) => (
          <Switch
            id={id}
            aria-describedby={describedBy}
            disabled={disabled}
            checked={definition.ignore_top_volatility.enabled}
            onCheckedChange={(checked) =>
              patch({
                ignore_top_volatility: { ...definition.ignore_top_volatility, enabled: checked },
              })
            }
          />
        )}
      />
      <SentinelNumberInput
        label="Ignore stocks above beta"
        value={definition.ignore_above_beta}
        onChange={(value) => patch({ ignore_above_beta: value })}
        sentinel={IGNORE_ABOVE_BETA_IGNORE}
        sentinelHint="Keep value as 100 if you want to ignore this filter."
        min={0}
        disabled={disabled}
      />
    </>
  );
}

// --- §2.12 Multi-Factor Combined Ranking ------------------------------------

export interface MultiFactorProps extends SectionProps {
  factors: readonly FactorOut[];
}

/**
 * docs/06 §step 5-6, in the reference product's own four steps (docs/01 §2.12). Prompt 9
 * deliverable 7 asks for this explanation inline, "copied in substance from docs/06".
 */
function CombinedRankExplanation() {
  return (
    <ol className="list-decimal space-y-1 rounded-md bg-muted/60 p-3 pl-7 text-xs text-muted-foreground">
      <li>Every filter except Sort By is applied to the stocks in the selected index.</li>
      <li>The survivors are ranked by factor one.</li>
      <li>They are independently ranked by factor two, then factor three.</li>
      <li>The three ranks are summed, and the final sort is ascending on that sum.</li>
    </ol>
  );
}

export function MultiFactorFilters({ definition, patch, disabled, factors }: MultiFactorProps) {
  return (
    <>
      <CombinedRankExplanation />
      {(["factor_two", "factor_three"] as const).map((slot, index) => {
        const extra = definition[slot];
        const ordinal = index === 0 ? "Two" : "Three";
        // docs/01 §2.12: factor three is revealed by, and ranks after, factor two.
        const blocked = slot === "factor_three" && !definition.factor_two.enabled;
        return (
          <div key={slot} className="space-y-2 border-t border-border pt-3 first:border-0 first:pt-0">
            <SwitchRow
              label={`Apply Factor ${ordinal}`}
              hint={blocked ? "Enable Factor Two first." : undefined}
              render={({ id, describedBy }) => (
                <Switch
                  id={id}
                  aria-describedby={describedBy}
                  disabled={disabled || blocked}
                  checked={extra.enabled}
                  onCheckedChange={(checked) =>
                    patch({
                      [slot]: { ...extra, enabled: checked },
                      ...(slot === "factor_two" && !checked
                        ? { factor_three: { ...definition.factor_three, enabled: false } }
                        : {}),
                    })
                  }
                />
              )}
            />
            {extra.enabled ? (
              <div className="space-y-2">
                <Label id={`${slot}-label`}>Factor {ordinal}</Label>
                <FactorCombobox
                  factors={factors}
                  value={extra.sort_by}
                  labelledBy={`${slot}-label`}
                  disabled={disabled}
                  onChange={(key) => patch({ [slot]: { ...extra, sort_by: key } })}
                />
                <Select
                  aria-label={`Factor ${ordinal} direction`}
                  disabled={disabled}
                  value={extra.sort_direction}
                  onChange={(event) =>
                    patch({
                      [slot]: {
                        ...extra,
                        sort_direction: event.currentTarget.value as "asc" | "desc",
                      },
                    })
                  }
                >
                  <option value="desc">Highest to Lowest</option>
                  <option value="asc">Lowest to Highest</option>
                </Select>
              </div>
            ) : null}
          </div>
        );
      })}
    </>
  );
}

// --- §2.14 Custom Filters ---------------------------------------------------

export interface CustomFiltersProps extends SectionProps {
  operands: readonly { key: string; label: string }[];
}

const OPERATORS = [
  { value: ">=", label: "is at least (≥)" },
  { value: "<=", label: "is at most (≤)" },
  { value: "=", label: "equals (=)" },
] as const;

export function CustomFilters({ definition, patch, disabled, operands }: CustomFiltersProps) {
  const slots = definition.custom_filters;
  const first = operands[0]?.key ?? "close";
  const second = operands[1]?.key ?? first;

  const update = (index: number, next: Partial<(typeof slots)[number]>) =>
    patch({
      custom_filters: slots.map((slot, i) => (i === index ? { ...slot, ...next } : slot)),
    });

  return (
    <>
      <p className="text-xs text-muted-foreground">
        Each slot compares two fields, so you can express things like “MA 50 ≥ MA 200” or “volume
        this week ≥ volume this year”.
      </p>

      {slots.map((slot, index) => (
        <div key={index} className="space-y-2 rounded-md border border-border p-3">
          <div className="flex items-center justify-between">
            <SwitchRow
              label={`Slot ${index + 1}`}
              render={({ id }) => (
                <Switch
                  id={id}
                  disabled={disabled}
                  checked={slot.enabled}
                  onCheckedChange={(checked) => update(index, { enabled: checked })}
                />
              )}
            />
            <Button
              variant="ghost"
              size="icon"
              className="size-8"
              aria-label={`Remove custom filter slot ${index + 1}`}
              disabled={disabled}
              onClick={() =>
                patch({ custom_filters: slots.filter((_, i) => i !== index) })
              }
            >
              <Trash2 aria-hidden="true" className="size-4" />
            </Button>
          </div>
          <Select
            aria-label={`Custom filter ${index + 1} left field`}
            disabled={disabled}
            value={slot.left}
            onChange={(event) => update(index, { left: event.currentTarget.value })}
          >
            {operands.map((operand) => (
              <option key={operand.key} value={operand.key}>
                {operand.label}
              </option>
            ))}
          </Select>
          <Select
            aria-label={`Custom filter ${index + 1} operator`}
            disabled={disabled}
            value={slot.op}
            onChange={(event) =>
              update(index, { op: event.currentTarget.value as (typeof OPERATORS)[number]["value"] })
            }
          >
            {OPERATORS.map((operator) => (
              <option key={operator.value} value={operator.value}>
                {operator.label}
              </option>
            ))}
          </Select>
          <Select
            aria-label={`Custom filter ${index + 1} right field`}
            disabled={disabled}
            value={slot.right}
            onChange={(event) => update(index, { right: event.currentTarget.value })}
          >
            {operands.map((operand) => (
              <option key={operand.key} value={operand.key}>
                {operand.label}
              </option>
            ))}
          </Select>
        </div>
      ))}

      {slots.length < MAX_CUSTOM_FILTERS ? (
        <Button
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() =>
            patch({
              custom_filters: [
                ...slots,
                { enabled: true, left: first, op: ">=", right: second },
              ],
            })
          }
        >
          <Plus aria-hidden="true" />
          Add a slot ({slots.length} of {MAX_CUSTOM_FILTERS})
        </Button>
      ) : (
        <p className="text-xs text-muted-foreground">
          All {MAX_CUSTOM_FILTERS} slots are in use.
        </p>
      )}
    </>
  );
}
