"use client";

import { useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * A number field whose "off" state is encoded in its own value — docs/08 §"Screen editor":
 *
 *     "Sentinel values are explained inline exactly as the reference does ('Keep value as 100 if
 *      you want to ignore…'), and the field renders visually 'off' when at its sentinel."
 *
 * docs/01 §2.4-§2.10 and `decile_core.screen_definition` fix the four sentinels:
 *
 *     away_from_high.ath / .one_year   100   = ignore
 *     positive_days.m*                   0   = ignore
 *     circuits.m*                    > 250   = ignore
 *     ignore_above_beta                100   = ignore
 *
 * Two of those are equality (`=== 100`, `=== 0`) and one is a *threshold* (`> 250`), which is why
 * `isOff` is a predicate rather than a compared constant. Getting that wrong is how a screen ends
 * up silently applying a filter the user meant to disable.
 *
 * Accessibility: Prompt 8 requires the explanation to reach the field "via aria-describedby", so
 * the hint has a stable id and the input points at it. The "Off" pill is `aria-hidden` because the
 * same fact is already in the description — announcing it twice is noise.
 */
export type SentinelMode = "equals" | "above";

export interface SentinelNumberInputProps {
  label: string;
  value: number;
  onChange: (value: number) => void;
  /** The value that means "ignore this filter". */
  sentinel: number;
  /** `equals`: off when `value === sentinel`. `above`: off when `value > sentinel`. */
  mode?: SentinelMode;
  /** The reference product's own wording, e.g. "Keep value as 100 if you want to ignore this." */
  sentinelHint: string;
  min?: number | undefined;
  max?: number | undefined;
  step?: number | undefined;
  suffix?: string | undefined;
  disabled?: boolean | undefined;
  className?: string;
}

export function isSentinel(value: number, sentinel: number, mode: SentinelMode): boolean {
  return mode === "above" ? value > sentinel : value === sentinel;
}

export function SentinelNumberInput({
  label,
  value,
  onChange,
  sentinel,
  mode = "equals",
  sentinelHint,
  min,
  max,
  step = 1,
  suffix,
  disabled = false,
  className,
}: SentinelNumberInputProps) {
  const inputId = useId();
  const hintId = `${inputId}-sentinel-hint`;
  const off = isSentinel(value, sentinel, mode);

  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={inputId}>{label}</Label>
        {off ? (
          <span
            aria-hidden="true"
            className="rounded-full border border-border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground"
          >
            Off
          </span>
        ) : null}
      </div>

      <div className="relative">
        <Input
          id={inputId}
          type="number"
          inputMode="numeric"
          value={String(value)}
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          aria-describedby={hintId}
          onChange={(event) => {
            const next = Number(event.currentTarget.value);
            if (!Number.isNaN(next)) onChange(next);
          }}
          className={cn("tnum", off && "text-muted-foreground", suffix && "pr-9")}
        />
        {suffix ? (
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-xs text-muted-foreground"
          >
            {suffix}
          </span>
        ) : null}
      </div>

      <p id={hintId} className="text-xs text-muted-foreground">
        {sentinelHint}
        {off ? " This filter is currently off." : ""}
      </p>
    </div>
  );
}
