"use client";

import { parseAsString, parseAsStringLiteral, useQueryState } from "nuqs";
import { useId, useState } from "react";

import { AskPanel } from "@/components/opportunities/ask-panel";
import { CheckboxGroup, NumberField, SELECT_CLASS } from "@/components/opportunities/fields";
import { OptionsResultView } from "@/components/opportunities/options-results";
import { ErrorPanel, ProviderUnavailable } from "@/components/opportunities/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { toProblem, useOpportunitiesMeta, useOptionsAnalysis } from "@/lib/opportunities/hooks";
import type { OpportunitiesMeta, OptionDirection, QuoteRules } from "@/lib/opportunities/types";
import { STRATEGY_LABEL, label } from "@/lib/opportunities/view";

const DIRECTIONS = ["BULLISH", "BEARISH"] as const;
const BULLISH_STRATEGIES = ["LONG_CALL", "BULL_CALL_SPREAD"];
const BEARISH_STRATEGIES = ["LONG_PUT", "BEAR_PUT_SPREAD"];

function defaultStrategies(meta: OpportunitiesMeta, direction: OptionDirection): string[] {
  const fit = direction === "BULLISH" ? BULLISH_STRATEGIES : BEARISH_STRATEGIES;
  return meta.strategies.filter((s) => fit.includes(s));
}

function OptionsAnalyzer({ meta }: { meta: OpportunitiesMeta }) {
  const symbolId = useId();
  const directionId = useId();
  const [symbol, setSymbol] = useQueryState("symbol", parseAsString.withDefault(""));
  const [direction, setDirection] = useQueryState(
    "direction",
    parseAsStringLiteral(DIRECTIONS).withDefault("BULLISH"),
  );
  const [strategies, setStrategies] = useState<string[]>(() => defaultStrategies(meta, direction));
  const [maxLoss, setMaxLoss] = useState<number | null>(null);
  const [minDte, setMinDte] = useState<number | null>(7);
  const [maxDte, setMaxDte] = useState<number | null>(45);
  const [rules, setRules] = useState<QuoteRules>(meta.quote_rules_defaults);
  const { state, run, cancel } = useOptionsAnalysis();
  const running = state.phase === "running";
  const trimmed = symbol.trim().toUpperCase();

  const submit = () => {
    if (!trimmed || strategies.length === 0) return;
    void run({
      symbol: trimmed,
      direction,
      strategies,
      max_loss_inr: maxLoss,
      min_dte: minDte ?? 0,
      max_dte: maxDte ?? 90,
      quote_rules: rules,
    });
  };
  const rule = (key: keyof QuoteRules) => (value: number | null) =>
    setRules((prev) => ({ ...prev, [key]: value ?? meta.quote_rules_defaults[key] }));

  return (
    <div className="space-y-5">
      {!meta.options_provider.available ? <ProviderUnavailable provider={meta.options_provider} /> : null}
      <form
        className="grid gap-4 rounded-lg border border-border/70 bg-card p-4"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
        data-testid="options-form"
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="flex flex-col gap-1">
            <label htmlFor={symbolId} className="text-xs font-medium text-muted-foreground">
              Symbol
            </label>
            <Input
              id={symbolId}
              value={symbol}
              onChange={(e) => void setSymbol(e.target.value.toUpperCase() || null)}
              placeholder="e.g. RELIANCE"
              autoCapitalize="characters"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={directionId} className="text-xs font-medium text-muted-foreground">
              Direction
            </label>
            <select
              id={directionId}
              className={SELECT_CLASS}
              value={direction}
              onChange={(e) => {
                const next: OptionDirection = e.target.value === "BEARISH" ? "BEARISH" : "BULLISH";
                void setDirection(next);
                setStrategies(defaultStrategies(meta, next));
              }}
            >
              <option value="BULLISH">Bullish</option>
              <option value="BEARISH">Bearish</option>
            </select>
          </div>
          <NumberField label="Max loss" suffix="₹" min={0} step={500} value={maxLoss} onChange={setMaxLoss} />
          <div className="grid grid-cols-2 gap-2">
            <NumberField label="Min DTE" min={0} value={minDte} onChange={setMinDte} />
            <NumberField label="Max DTE" min={0} value={maxDte} onChange={setMaxDte} />
          </div>
        </div>
        <CheckboxGroup
          legend="Strategies"
          options={meta.strategies.map((s) => ({ value: s, label: label(STRATEGY_LABEL, s) }))}
          selected={strategies}
          onChange={setStrategies}
        />
        <fieldset className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <legend className="mb-1 text-xs font-medium text-muted-foreground">Quote rules</legend>
          <NumberField label="Max spread" suffix="%" step={0.1} min={0} value={rules.max_spread_pct} onChange={rule("max_spread_pct")} />
          <NumberField label="Max quote age" suffix="s" min={0} value={rules.max_quote_age_s} onChange={rule("max_quote_age_s")} />
          <NumberField label="Min OI" min={0} value={rules.min_oi} onChange={rule("min_oi")} />
          <NumberField label="Min volume" min={0} value={rules.min_volume} onChange={rule("min_volume")} />
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="primary" disabled={running || !trimmed || strategies.length === 0}>
            {running ? "Analyzing…" : "Analyze"}
          </Button>
          {running ? (
            <Button variant="outline" onClick={cancel}>
              Cancel
            </Button>
          ) : null}
          {state.phase === "cancelled" ? <span className="text-xs text-muted-foreground">Cancelled.</span> : null}
        </div>
      </form>
      {state.error ? <ErrorPanel problem={state.error} onRetry={submit} /> : null}
      {state.result ? <OptionsResultView analysis={state.result} /> : null}
    </div>
  );
}

export function OptionsWorkspace() {
  const [symbolParam] = useQueryState("symbol", parseAsString.withDefault(""));
  const meta = useOpportunitiesMeta();
  if (meta.isPending) {
    return (
      <div className="space-y-3" aria-busy="true">
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }
  if (meta.isError) {
    return <ErrorPanel problem={toProblem(meta.error)} onRetry={() => void meta.refetch()} />;
  }
  return (
    <div className="space-y-8">
      <OptionsAnalyzer meta={meta.data} />
      <AskPanel symbol={symbolParam.trim() || undefined} />
    </div>
  );
}
