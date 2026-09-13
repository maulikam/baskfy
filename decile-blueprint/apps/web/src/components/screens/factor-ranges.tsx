"use client";

import {
  MAX_FACTOR_RANGES,
  REGIME_VALUES,
  type ScreenDefinition,
} from "@baskfy/api-client";
import { Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";

import { InlineFactorPicker } from "@/components/data/factor-combobox";
import { DraftNumberInput } from "@/components/screens/draft-number-input";
import { SwitchRow } from "@/components/screens/field";
import type { SectionProps } from "@/components/screens/filter-sections";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  EXCESS_RETURN_WINDOWS,
  beatBenchmarkIndex,
  canAddRange,
  checkPatch,
  unitHint,
  withBeatBenchmark,
  withRegimeToggled,
  type ExcessReturnKey,
  type FactorRange,
  type RankingFactorMeta,
  type RegimeLabel,
} from "@/lib/screens/ranking";

/**
 * Factor Ranges and Market Regime — docs/ranking/PLAN.md C3, gates/ranking-2.H-web.md G2.
 *
 * Both are eligibility filters: they decide which stocks appear, never their order. Any factor may
 * be ranged, including the ones that cannot rank (`excess_ret_*` is the reason this exists: a
 * common NIFTY 500 subtraction cannot reorder a list, but "beat the index" is a real gate).
 */

export interface FactorRangesProps extends SectionProps {
  factors: readonly RankingFactorMeta[];
}

function useCommit(definition: ScreenDefinition, patch: SectionProps["patch"]) {
  const [error, setError] = useState<string | null>(null);
  const commit = (partial: Partial<ScreenDefinition>) => {
    const problem = checkPatch(definition, partial);
    setError(problem);
    if (problem === null) patch(partial);
  };
  return { error, setError, commit };
}

export function FactorRangesFilter({ definition, patch, disabled, factors }: FactorRangesProps) {
  const { error, setError, commit } = useCommit(definition, patch);
  const ranges = definition.factor_ranges;
  const beatIndex = beatBenchmarkIndex(ranges);
  // The "beat NIFTY 500" range is edited in its own control, so it is not listed twice.
  const visible = ranges
    .map((range, index) => ({ range, index }))
    .filter(({ index }) => index !== beatIndex);
  /* The draft row: a range is not valid until it has a bound, so a new one lives here until the
     user types a minimum or a maximum. */
  const [draftFactor, setDraftFactor] = useState<string | null>(null);

  const update = (index: number, next: Partial<FactorRange>) =>
    commit({
      factor_ranges: ranges.map((range, i) => (i === index ? { ...range, ...next } : range)),
    });

  return (
    <div className="space-y-4" data-testid="factor-ranges">
      <p className="text-xs text-muted-foreground">
        Keep only stocks whose factor value is inside a band. Both ends are inclusive, and a stock
        with no value for the factor is excluded while the range is on.
      </p>

      <BeatBenchmark
        definition={definition}
        disabled={disabled}
        onCommit={commit}
      />

      {/* One keyed list for saved rows and the draft: the draft's key is the one its row gets once
          committed, so the inputs survive the commit and the next keystroke is not lost. */}
      {[
        ...visible.map(({ range, index }, position) => (
          <RangeRow
            key={`${index}-${range.factor}`}
            ordinal={position + 1}
            range={range}
            factors={factors}
            disabled={disabled}
            onChange={(next) => update(index, next)}
            onRemove={() => commit({ factor_ranges: ranges.filter((_, i) => i !== index) })}
            onProblem={setError}
          />
        )),
        draftFactor === null ? null : (
          <RangeRow
            key={`${ranges.length}-${draftFactor}`}
            ordinal={visible.length + 1}
            range={{ enabled: true, factor: draftFactor, min: null, max: null }}
            factors={factors}
            disabled={disabled}
            draft
            onChange={(next) => {
              const range: FactorRange = {
                enabled: true,
                factor: draftFactor,
                min: null,
                max: null,
                ...next,
              };
              if (next.factor !== undefined && range.min === null && range.max === null) {
                setDraftFactor(next.factor);
                return;
              }
              if (range.min === null && range.max === null) return;
              const partial = { factor_ranges: [...ranges, range] };
              if (checkPatch(definition, partial) === null) setDraftFactor(null);
              commit(partial);
            }}
            onRemove={() => setDraftFactor(null)}
            onProblem={setError}
          />
        ),
      ]}

      {draftFactor !== null ? null : canAddRange(definition) ? (
        <Button
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() => setDraftFactor(factors[0]?.key ?? "ret_12m")}
        >
          <Plus aria-hidden="true" />
          Add a factor range ({ranges.length} of {MAX_FACTOR_RANGES})
        </Button>
      ) : (
        <p className="text-xs text-muted-foreground">
          All {MAX_FACTOR_RANGES} factor ranges are in use.
        </p>
      )}

      {error ? (
        <p role="alert" className="text-xs text-negative">
          Not applied: {error}
        </p>
      ) : null}
    </div>
  );
}

/** "Beat NIFTY 500 by at least X pp over <window>" — an `excess_ret_*` range with only a minimum. */
function BeatBenchmark({
  definition,
  disabled,
  onCommit,
}: {
  definition: ScreenDefinition;
  disabled: boolean | undefined;
  onCommit: (partial: Partial<ScreenDefinition>) => void;
}) {
  const id = useId();
  const existing = definition.factor_ranges[beatBenchmarkIndex(definition.factor_ranges)];
  const [chosenWindow, setChosenWindow] = useState<ExcessReturnKey>("excess_ret_12m");
  const selected = (existing?.factor as ExcessReturnKey | undefined) ?? chosenWindow;
  const points = existing?.min ?? null;

  return (
    <div className="space-y-2 rounded-md border border-border p-3" data-testid="beat-benchmark">
      <p className="text-sm font-medium">Beat NIFTY 500</p>
      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <Label htmlFor={`${id}-points`} className="text-xs">
            By at least (pp)
          </Label>
          <DraftNumberInput
            id={`${id}-points`}
            aria-describedby={`${id}-hint`}
            placeholder="Off"
            disabled={disabled}
            value={points}
            onValue={(value) => onCommit(withBeatBenchmark(definition, selected, value))}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor={`${id}-window`} className="text-xs">
            Over
          </Label>
          <Select
            id={`${id}-window`}
            disabled={disabled}
            value={selected}
            onChange={(event) => {
              const next = event.currentTarget.value as ExcessReturnKey;
              setChosenWindow(next);
              if (points !== null) onCommit(withBeatBenchmark(definition, next, points));
            }}
          >
            {EXCESS_RETURN_WINDOWS.map((option) => (
              <option key={option.key} value={option.key}>
                {option.label}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        The stock&rsquo;s return minus NIFTY 500&rsquo;s over the same window, in percentage
        points. 0 keeps stocks that matched or beat the index; 10 keeps stocks at least 10 points
        ahead. Leave blank to turn it off.
      </p>
    </div>
  );
}

function RangeRow({
  ordinal,
  range,
  factors,
  disabled,
  draft = false,
  onChange,
  onRemove,
  onProblem,
}: {
  ordinal: number;
  range: FactorRange;
  factors: readonly RankingFactorMeta[];
  disabled: boolean | undefined;
  draft?: boolean;
  onChange: (next: Partial<FactorRange>) => void;
  onRemove: () => void;
  onProblem: (message: string | null) => void;
}) {
  const id = useId();
  const meta = factors.find((factor) => factor.key === range.factor);

  const setBound = (side: "min" | "max", value: number | null) => {
    const bounds = { min: range.min, max: range.max, [side]: value };
    if (bounds.min === null && bounds.max === null) {
      if (!draft) onProblem("a factor range needs a minimum, a maximum or both. Remove it to turn it off.");
      return;
    }
    if (bounds.min !== null && bounds.max !== null && bounds.min > bounds.max) {
      onProblem("the minimum is above the maximum.");
      return;
    }
    onChange(bounds);
  };

  return (
    <div className="space-y-2 rounded-md border border-border p-3" data-testid={`factor-range-${ordinal}`}>
      <div className="flex items-center justify-between gap-2">
        {draft ? (
          <span className="text-sm font-medium">New range</span>
        ) : (
          <SwitchRow
            label={`Range ${ordinal}`}
            render={({ id: switchId }) => (
              <Switch
                id={switchId}
                disabled={disabled}
                checked={range.enabled}
                onCheckedChange={(checked) => onChange({ enabled: checked })}
              />
            )}
          />
        )}
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label={draft ? "Discard the new range" : `Remove range ${ordinal}`}
          disabled={disabled}
          onClick={onRemove}
        >
          <Trash2 aria-hidden="true" className="size-4" />
        </Button>
      </div>
      <div className="space-y-1.5">
        <Label id={`${id}-factor`}>Factor</Label>
        <InlineFactorPicker
          factors={factors}
          value={range.factor}
          labelledBy={`${id}-factor`}
          disabled={disabled}
          onChange={(key) => onChange({ factor: key })}
        />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <DraftNumberInput
          aria-label={`Range ${ordinal} minimum`}
          placeholder="Minimum"
          disabled={disabled}
          value={range.min}
          onValue={(value) => setBound("min", value)}
        />
        <DraftNumberInput
          aria-label={`Range ${ordinal} maximum`}
          placeholder="Maximum"
          disabled={disabled}
          value={range.max}
          onValue={(value) => setBound("max", value)}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        {unitHint(meta?.unit)} A blank side is open.
        {draft ? " Not applied until you enter a minimum or a maximum." : ""}
      </p>
    </div>
  );
}

const REGIME_COPY: Record<RegimeLabel, string> = {
  BULL: "Bull",
  NEUTRAL: "Neutral",
  BEAR: "Bear",
};

export function RegimeFilter({ definition, patch, disabled }: SectionProps) {
  const { error, commit } = useCommit(definition, patch);
  const selected = definition.regime_in ?? [];
  return (
    <div className="space-y-2" data-testid="regime-filter">
      <p className="text-xs text-muted-foreground">
        Keep only stocks whose regime label on the screen date is one you switch on. The label is
        each stock&rsquo;s own Wasserstein regime, stored nightly. Stocks with no label are
        excluded while any regime is on. Leave all off to ignore the regime.
      </p>
      {REGIME_VALUES.map((label) => (
        <SwitchRow
          key={label}
          label={REGIME_COPY[label]}
          render={({ id }) => (
            <Switch
              id={id}
              disabled={disabled}
              checked={selected.includes(label)}
              onCheckedChange={(checked) => commit(withRegimeToggled(definition, label, checked))}
            />
          )}
        />
      ))}
      {error ? (
        <p role="alert" className="text-xs text-negative">
          Not applied: {error}
        </p>
      ) : null}
    </div>
  );
}
