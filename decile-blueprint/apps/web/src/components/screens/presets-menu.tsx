"use client";

import type { RankingPresetOut, ScreenDefinition } from "@baskfy/api-client";
import { ChevronDown, Sparkles } from "lucide-react";
import { useState } from "react";

import { ErrorState } from "@/components/data/error-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { applyPreset, presetStatusBadge, validatePreset } from "@/lib/screens/presets";
import { useRankingPresets } from "@/lib/screens/queries";
import { cn } from "@/lib/utils";

/**
 * The ranking presets menu — `GET /meta/ranking-presets`, gates/ranking-2.H-web.md G5.
 *
 * Each preset shows its promotion status as a badge. Choosing one patches the current definition
 * (see `applyPreset`): filters it does not name stay, and a change the schema would refuse is shown
 * with its reason rather than applied.
 */
export interface PresetsMenuViewProps {
  presets: readonly RankingPresetOut[] | undefined;
  definition: ScreenDefinition;
  patch: (change: Partial<ScreenDefinition>) => void;
  disabled?: boolean | undefined;
  loading?: boolean | undefined;
  error?: unknown;
  onRetry?: (() => void) | undefined;
  className?: string | undefined;
}

export function PresetsMenuView({
  presets,
  definition,
  patch,
  disabled = false,
  loading = false,
  error,
  onRetry,
  className,
}: PresetsMenuViewProps) {
  const [open, setOpen] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const choose = (preset: RankingPresetOut) => {
    const result = applyPreset(definition, preset);
    if (!result.ok) {
      setProblem(`${preset.label} was not applied: ${result.reason}`);
      return;
    }
    setProblem(null);
    patch(result.change);
    setOpen(false);
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setProblem(null);
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          disabled={disabled}
          data-testid="presets-menu-trigger"
          className={cn("vaaya-pill rounded-full border-border bg-card shadow-none", className)}
        >
          <Sparkles aria-hidden="true" />
          Presets
          <ChevronDown aria-hidden="true" className="opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 space-y-3 p-3" data-testid="presets-menu">
        <div className="space-y-1">
          <p className="text-sm font-medium">Ranking presets</p>
          <p className="text-xs text-muted-foreground">
            A preset replaces how the screen ranks, including any ranking terms, and keeps the
            filters it does not name.
          </p>
        </div>

        {error && !presets ? <ErrorState error={error} onRetry={onRetry} /> : null}
        {loading && !presets ? (
          <p className="text-xs text-muted-foreground">Loading presets…</p>
        ) : null}
        {presets && presets.length === 0 ? (
          <p className="text-xs text-muted-foreground">No presets are available.</p>
        ) : null}

        {presets && presets.length > 0 ? (
          <ul className="space-y-1" aria-label="Ranking presets">
            {presets.map((preset) => {
              const status = presetStatusBadge(preset.status);
              const check = validatePreset(preset);
              return (
                <li key={preset.key}>
                  <button
                    type="button"
                    data-testid={`preset-${preset.key}`}
                    disabled={!check.ok}
                    onClick={() => choose(preset)}
                    className={cn(
                      "w-full space-y-1 rounded-md px-2 py-2 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none",
                      !check.ok && "cursor-not-allowed opacity-60 hover:bg-transparent",
                    )}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">{preset.label}</span>
                      <Badge
                        variant={status.tone}
                        title={status.note}
                        data-testid={`preset-status-${preset.key}`}
                      >
                        {status.label}
                      </Badge>
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {preset.description}
                    </span>
                    {check.ok ? null : (
                      <span
                        className="block text-xs text-negative"
                        data-testid={`preset-invalid-${preset.key}`}
                      >
                        Cannot be applied: {check.reason}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}

        {problem ? (
          <p role="alert" className="text-xs text-negative" data-testid="presets-problem">
            {problem}
          </p>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}

export interface PresetsMenuProps {
  definition: ScreenDefinition;
  patch: (change: Partial<ScreenDefinition>) => void;
  disabled?: boolean | undefined;
  className?: string | undefined;
}

/** The editor's presets menu, fed by `GET /meta/ranking-presets`. */
export function PresetsMenu({ definition, patch, disabled, className }: PresetsMenuProps) {
  const presets = useRankingPresets();
  return (
    <PresetsMenuView
      presets={presets.data}
      definition={definition}
      patch={patch}
      disabled={disabled}
      loading={presets.isPending}
      error={presets.error}
      onRetry={() => void presets.refetch()}
      className={className}
    />
  );
}
