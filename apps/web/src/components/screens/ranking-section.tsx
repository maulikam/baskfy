"use client";

import { MAX_RANKING_TERMS, type ScreenDefinition } from "@baskfy/api-client";
import { Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";

import { InlineFactorPicker } from "@/components/data/factor-combobox";
import { DraftNumberInput } from "@/components/screens/draft-number-input";
import { Field } from "@/components/screens/field";
import type { SectionProps } from "@/components/screens/filter-sections";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  WEIGHT_FAMILIES,
  canAddTerm,
  checkPatch,
  rankableFactors,
  unitHint,
  weightFamilyLabel,
  withRankingMode,
  withTermAdded,
  withTermChanged,
  withTermFactor,
  withTermRemoved,
  withinSectorAllowed,
  type MissingDataPolicy,
  type RankingFactorMeta,
  type RankingMode,
  type RankingScope,
  type RankingTerm,
  type TermPreference,
  type WeightFamily,
} from "@/lib/screens/ranking";
import { cn } from "@/lib/utils";

/**
 * The editor's Ranking section — docs/ranking/PLAN.md C3/C4, gates/ranking-2.H-web.md G1.
 *
 * Mode, explicit terms, family weights, the missing-data rule and scope. Every change goes through
 * `checkPatch` first, so a change the schema would refuse shows its reason here instead of being
 * dropped by the URL state.
 */

export const MODE_OPTIONS: readonly { value: RankingMode; label: string; copy: string }[] = [
  {
    value: "single",
    label: "Single",
    copy: "Ranks by one factor, best value first. Nothing else changes the order.",
  },
  {
    value: "sequential",
    label: "Sequential",
    copy:
      "Orders by the first factor's value. Each later factor only breaks exact ties on the ones before it, and ties are rare on continuous numbers, so later factors often change nothing.",
  },
  {
    value: "composite",
    label: "Composite",
    copy:
      "Scores each factor against the scope, then adds the scores by weight into one 0–100 score. Every factor moves the order.",
  },
];

export const SCOPE_OPTIONS: readonly { value: RankingScope; label: string; copy: string }[] = [
  {
    value: "fixed_universe",
    label: "Fixed universe",
    copy:
      "Ranks against every stock in the selected index before your filters. A filter removes rows but does not move the scores of the stocks that remain.",
  },
  {
    value: "filtered_results",
    label: "Filtered results",
    copy:
      "Ranks only against the stocks that pass your filters. Changing a filter can move the scores of the stocks that remain.",
  },
  {
    value: "within_sector",
    label: "Within sector",
    copy:
      "Scores each stock against the other stocks in its sector in the selected index, before filters. Stocks in no sector index are scored together as unclassified. Composite with ranking terms only.",
  },
];

const PREFERENCE_OPTIONS: readonly { value: TermPreference; label: string }[] = [
  { value: "higher", label: "Higher is better" },
  { value: "lower", label: "Lower is better" },
  { value: "target_range", label: "Inside a target range" },
];

const MISSING_DATA_OPTIONS: readonly { value: MissingDataPolicy; label: string }[] = [
  { value: "penalize", label: "Score it as the worst (0)" },
  { value: "neutral", label: "Score it as the middle (0.5)" },
  { value: "exclude", label: "Leave the stock out of the results" },
];

export interface RankingSectionProps extends SectionProps {
  factors: readonly RankingFactorMeta[];
}

export function RankingSection({ definition, patch, disabled, factors }: RankingSectionProps) {
  const [error, setError] = useState<string | null>(null);
  const errorId = useId();

  const commit = (partial: Partial<ScreenDefinition>) => {
    const problem = checkPatch(definition, partial);
    setError(problem);
    if (problem === null) patch(partial);
  };

  const terms = definition.ranking_terms;
  const composite = definition.ranking_mode === "composite";
  const hasTerms = terms.length > 0;
  const mode = MODE_OPTIONS.find((option) => option.value === definition.ranking_mode);
  const scope = SCOPE_OPTIONS.find((option) => option.value === definition.ranking_scope);
  const sectorAllowed = withinSectorAllowed(definition);
  const choices = rankableFactors(factors);

  return (
    <div className="space-y-4" data-testid="ranking-section">
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Mode</legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {MODE_OPTIONS.map((option) => (
            <label
              key={option.value}
              className={cn(
                "flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-sm",
                definition.ranking_mode === option.value && "border-foreground",
                disabled && "cursor-not-allowed opacity-50",
              )}
            >
              <input
                type="radio"
                name={`${errorId}-mode`}
                value={option.value}
                disabled={disabled}
                checked={definition.ranking_mode === option.value}
                onChange={() => commit(withRankingMode(definition, option.value))}
              />
              {option.label}
            </label>
          ))}
        </div>
        {mode ? (
          <p className="text-xs text-muted-foreground" data-testid="ranking-mode-copy">
            {mode.copy}
          </p>
        ) : null}
      </fieldset>

      <div className="space-y-2 border-t border-border pt-3">
        <p className="text-sm font-medium">Ranking terms</p>
        {hasTerms ? null : (
          <p className="text-xs text-muted-foreground">
            No ranking terms: the screen ranks by Sort By
            {definition.ranking_mode === "single" ? "" : ", and Factor Two and Three if they are on"}
            . Add a term to set a preference, a weight and a missing-data rule per factor.
          </p>
        )}
        {terms.map((term, index) => (
          <TermEditor
            key={`${index}-${term.factor}`}
            index={index}
            term={term}
            factors={factors}
            choices={choices}
            composite={composite}
            disabled={disabled}
            onFactor={(key) => commit(withTermFactor(definition, factors, index, key))}
            onChange={(next) => commit(withTermChanged(definition, index, next))}
            onRemove={() => commit(withTermRemoved(definition, index))}
            onProblem={setError}
          />
        ))}
        {canAddTerm(definition) ? (
          <Button
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={() => commit(withTermAdded(definition, factors))}
          >
            <Plus aria-hidden="true" />
            Add a ranking term ({terms.length} of{" "}
            {definition.ranking_mode === "single" ? 1 : MAX_RANKING_TERMS})
          </Button>
        ) : (
          <p className="text-xs text-muted-foreground">
            {definition.ranking_mode === "single"
              ? "Single mode ranks by one term. Switch to Sequential or Composite to add more."
              : `All ${MAX_RANKING_TERMS} terms are in use.`}
          </p>
        )}
      </div>

      {composite && hasTerms ? (
        <fieldset className="space-y-2 border-t border-border pt-3" data-testid="family-weights">
          <legend className="text-sm font-medium">Family weights</legend>
          <p className="text-xs text-muted-foreground">
            Each family&rsquo;s share of the composite score. Numbers are relative: 2 and 1 give
            two-thirds and one-third. A blank family gets the average of the numbers you set, and
            all blank splits the score equally. A family with no terms gets no share; terms in one
            family split its share by their weights.
          </p>
          <div className="grid grid-cols-2 gap-2">
            {WEIGHT_FAMILIES.map((family) => (
              <FamilyWeightInput
                key={family.key}
                family={family.key}
                label={family.label}
                definition={definition}
                disabled={disabled}
                onCommit={commit}
                onProblem={setError}
              />
            ))}
          </div>
        </fieldset>
      ) : null}

      <div className="space-y-4 border-t border-border pt-3">
        <Field
          label="Missing data"
          hint={
            hasTerms
              ? "When a stock has no value for a term, for example too little history. In Single and Sequential, missing values sort last under the first two choices."
              : "Applies once the screen has ranking terms."
          }
          render={({ id, describedBy }) => (
            <Select
              id={id}
              aria-describedby={describedBy}
              disabled={disabled || !hasTerms}
              value={definition.missing_data}
              onChange={(event) =>
                commit({ missing_data: event.currentTarget.value as MissingDataPolicy })
              }
            >
              {MISSING_DATA_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          )}
        />

        <Field
          label="Scope"
          hint={scope?.copy}
          render={({ id, describedBy }) => (
            <Select
              id={id}
              aria-describedby={describedBy}
              disabled={disabled}
              value={definition.ranking_scope}
              onChange={(event) =>
                commit({ ranking_scope: event.currentTarget.value as RankingScope })
              }
            >
              {SCOPE_OPTIONS.map((option) => (
                <option
                  key={option.value}
                  value={option.value}
                  disabled={option.value === "within_sector" && !sectorAllowed}
                >
                  {option.value === "within_sector" && !sectorAllowed
                    ? `${option.label} (composite with terms)`
                    : option.label}
                </option>
              ))}
            </Select>
          )}
        />
      </div>

      {error ? (
        <p id={errorId} role="alert" className="text-xs text-negative">
          Not applied: {error}
        </p>
      ) : null}
    </div>
  );
}

interface TermEditorProps {
  index: number;
  term: RankingTerm;
  factors: readonly RankingFactorMeta[];
  choices: readonly RankingFactorMeta[];
  composite: boolean;
  disabled: boolean | undefined;
  onFactor: (key: string) => void;
  onChange: (next: Partial<RankingTerm>) => void;
  onRemove: () => void;
  onProblem: (message: string | null) => void;
}

function TermEditor({
  index,
  term,
  factors,
  choices,
  composite,
  disabled,
  onFactor,
  onChange,
  onRemove,
  onProblem,
}: TermEditorProps) {
  const id = useId();
  /* A target range is not a valid term until it has a bound, so choosing it opens the band here
     and the term keeps its old preference until a number is typed. */
  const [pendingRange, setPendingRange] = useState(false);
  const meta = factors.find((factor) => factor.key === term.factor);
  const family = weightFamilyLabel(meta?.weight_family);
  const ordinal = index + 1;
  const showRange = term.preference === "target_range" || pendingRange;

  const setBound = (side: "target_min" | "target_max", value: number | null) => {
    const bounds = { target_min: term.target_min, target_max: term.target_max, [side]: value };
    if (bounds.target_min === null && bounds.target_max === null) {
      if (term.preference === "target_range") {
        onProblem("a target range needs a minimum, a maximum or both.");
      }
      return;
    }
    if (
      bounds.target_min !== null &&
      bounds.target_max !== null &&
      bounds.target_min > bounds.target_max
    ) {
      onProblem("the target minimum is above the maximum.");
      return;
    }
    setPendingRange(false);
    onChange({ preference: "target_range", ...bounds });
  };

  return (
    <div
      className="space-y-2 rounded-md border border-border p-3"
      data-testid={`ranking-term-${ordinal}`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">Term {ordinal}</span>
          {family ? <Badge variant="neutral">{family}</Badge> : null}
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label={`Remove term ${ordinal}`}
          disabled={disabled}
          onClick={onRemove}
        >
          <Trash2 aria-hidden="true" className="size-4" />
        </Button>
      </div>

      <div className="space-y-1.5">
        <Label id={`${id}-factor`}>Factor</Label>
        <InlineFactorPicker
          factors={choices}
          value={term.factor}
          labelledBy={`${id}-factor`}
          placeholder="Search rankable factors…"
          disabled={disabled}
          onChange={onFactor}
        />
        {index === 0 ? (
          <p className="text-xs text-muted-foreground">
            The first term is also the screen&rsquo;s Sort By.
          </p>
        ) : null}
      </div>

      <Field
        label="Preference"
        render={({ id: selectId }) => (
          <Select
            id={selectId}
            disabled={disabled}
            value={showRange ? "target_range" : term.preference}
            onChange={(event) => {
              const next = event.currentTarget.value as TermPreference;
              if (next === "target_range") {
                setPendingRange(true);
                return;
              }
              setPendingRange(false);
              onChange({ preference: next, target_min: null, target_max: null });
            }}
          >
            {PREFERENCE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        )}
      />

      {showRange ? (
        <div className="space-y-1.5">
          <div className="grid grid-cols-2 gap-2">
            <DraftNumberInput
              aria-label={`Term ${ordinal} target minimum`}
              placeholder="Minimum"
              disabled={disabled}
              value={term.target_min}
              onValue={(value) => setBound("target_min", value)}
            />
            <DraftNumberInput
              aria-label={`Term ${ordinal} target maximum`}
              placeholder="Maximum"
              disabled={disabled}
              value={term.target_max}
              onValue={(value) => setBound("target_max", value)}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Inside the band scores best; the further outside, the lower the score. A blank side is
            open. {unitHint(meta?.unit)}
            {term.preference === "target_range"
              ? ""
              : " Not applied until you enter a minimum or a maximum."}
          </p>
        </div>
      ) : null}

      {composite ? (
        <div className="space-y-1.5">
          <Label htmlFor={`${id}-weight`}>Weight</Label>
          <DraftNumberInput
            id={`${id}-weight`}
            aria-describedby={`${id}-weight-hint`}
            min={0}
            max={100}
            disabled={disabled}
            value={term.weight}
            onValue={(value) => {
              if (value === null || value <= 0 || value > 100) {
                onProblem("a term weight must be above 0 and at most 100.");
                return;
              }
              onChange({ weight: value });
            }}
          />
          <p id={`${id}-weight-hint`} className="text-xs text-muted-foreground">
            Relative to the other terms in the same family.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function FamilyWeightInput({
  family,
  label,
  definition,
  disabled,
  onCommit,
  onProblem,
}: {
  family: WeightFamily;
  label: string;
  definition: ScreenDefinition;
  disabled: boolean | undefined;
  onCommit: (partial: Partial<ScreenDefinition>) => void;
  onProblem: (message: string | null) => void;
}) {
  const id = useId();
  const current = definition.family_weights;
  return (
    <div className="space-y-1">
      <Label htmlFor={id} className="text-xs">
        {label}
      </Label>
      <DraftNumberInput
        id={id}
        min={0}
        placeholder="Equal"
        disabled={disabled}
        value={current?.[family] ?? null}
        onValue={(value) => {
          if (value !== null && value < 0) {
            onProblem("a family weight cannot be negative.");
            return;
          }
          const next = {
            momentum: current?.momentum ?? null,
            path_quality: current?.path_quality ?? null,
            trend_structure: current?.trend_structure ?? null,
            participation: current?.participation ?? null,
            risk_execution: current?.risk_execution ?? null,
            [family]: value,
          };
          const empty = Object.values(next).every((weight) => weight === null);
          onCommit({ family_weights: empty ? null : next });
        }}
      />
    </div>
  );
}
