"use client";

import { useActionState, useId } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import type { SwingConfig } from "@/lib/swing/fetch";
import type { SwingFormResult } from "@/lib/swing/write";
import { cn } from "@/lib/utils";

/**
 * The `sw_config` form — `docs/swing/05` §2 "/swing/settings", SW14.
 *
 * Every editable number is one `PATCH /swing/config` field. The three the server bounds carry
 * their ceiling under the box as "max 1.0% — set by the server", read from the same response
 * the form was filled from, so the limit is known before a save is refused; when one is refused
 * anyway (a stale page, a raised ceiling) the 422's own sentence lands beside that field.
 *
 * The rung, the first-live countdown and the execution flag are shown by the page, not here:
 * they are not fields of this form and cannot be posted from it.
 */

type SettingsAction = (
  previous: SwingFormResult | null,
  formData: FormData,
) => Promise<SwingFormResult>;

const STOP_MODES: { value: string; label: string }[] = [
  { value: "LOW_OF_DAY", label: "Low of the day" },
  { value: "OPENING_RANGE_LOW", label: "Low of the opening range" },
];

function Field({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: string | undefined;
  error?: string | undefined;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
      {error ? (
        <p className="text-xs text-negative" role="alert" data-testid={`error-${id}`}>
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function SettingsForm({ action, config }: { action: SettingsAction; config: SwingConfig }) {
  const [result, formAction, pending] = useActionState(action, null);
  const ids = {
    sleeve_capital_inr: useId(),
    risk_per_trade_pct: useId(),
    max_position_pct: useId(),
    max_open_positions: useId(),
    or_window_minutes: useId(),
    stop_mode: useId(),
    adr_min_pct: useId(),
    turnover_min_inr: useId(),
    price_min: useId(),
  };
  const errorFor = (field: keyof typeof ids): string | undefined =>
    result && !result.ok && result.field === field ? result.error : undefined;
  const ceiling = (field: string, unit: string): string | undefined => {
    const value = config.ceilings[field];
    return value ? `max ${value}${unit} — set by the server` : undefined;
  };

  return (
    <form action={formAction} className="grid max-w-2xl gap-5 sm:grid-cols-2">
      <Field
        id={ids.sleeve_capital_inr}
        label="Allocation capital (₹)"
        hint="What the swing allocation may put to work, in rupees. Risk and position size are percentages of this."
        error={errorFor("sleeve_capital_inr")}
      >
        <Input
          id={ids.sleeve_capital_inr}
          name="sleeve_capital_inr"
          inputMode="decimal"
          defaultValue={config.sleeve_capital_inr.toFixed(2)}
          pattern="\d+(\.\d+)?"
        />
      </Field>
      <Field
        id={ids.risk_per_trade_pct}
        label="Risk per trade (%)"
        hint={ceiling("risk_per_trade_pct", "%")}
        error={errorFor("risk_per_trade_pct")}
      >
        <Input
          id={ids.risk_per_trade_pct}
          name="risk_per_trade_pct"
          inputMode="decimal"
          defaultValue={config.risk_per_trade_pct.toFixed(3)}
          pattern="\d+(\.\d+)?"
        />
      </Field>
      <Field
        id={ids.max_position_pct}
        label="Largest position (% of the allocation)"
        hint={ceiling("max_position_pct", "%")}
        error={errorFor("max_position_pct")}
      >
        <Input
          id={ids.max_position_pct}
          name="max_position_pct"
          inputMode="decimal"
          defaultValue={config.max_position_pct.toFixed(2)}
          pattern="\d+(\.\d+)?"
        />
      </Field>
      <Field
        id={ids.max_open_positions}
        label="Most positions open"
        hint={ceiling("max_open_positions", "")}
        error={errorFor("max_open_positions")}
      >
        <Input
          id={ids.max_open_positions}
          name="max_open_positions"
          inputMode="numeric"
          defaultValue={String(config.max_open_positions)}
          pattern="\d+"
        />
      </Field>
      <Field
        id={ids.or_window_minutes}
        label="Opening-range window"
        hint="How long after 09:15 the range is built before a break of it counts."
        error={errorFor("or_window_minutes")}
      >
        <Select
          id={ids.or_window_minutes}
          name="or_window_minutes"
          defaultValue={String(config.or_window_minutes)}
        >
          <option value="1">1 minute</option>
          <option value="5">5 minutes</option>
          <option value="60">60 minutes</option>
        </Select>
      </Field>
      <Field
        id={ids.stop_mode}
        label="Stop mode"
        hint="Where the first stop goes on the day of the entry."
        error={errorFor("stop_mode")}
      >
        <Select id={ids.stop_mode} name="stop_mode" defaultValue={config.stop_mode}>
          {STOP_MODES.map((mode) => (
            <option key={mode.value} value={mode.value}>
              {mode.label}
            </option>
          ))}
        </Select>
      </Field>
      <Field
        id={ids.adr_min_pct}
        label="Least daily range (ADR %)"
        hint="A name that moves less than this a day is not liquid enough to be a swing."
        error={errorFor("adr_min_pct")}
      >
        <Input
          id={ids.adr_min_pct}
          name="adr_min_pct"
          inputMode="decimal"
          defaultValue={config.adr_min_pct.toFixed(2)}
          pattern="\d+(\.\d+)?"
        />
      </Field>
      <Field
        id={ids.turnover_min_inr}
        label="Least turnover (₹ a day)"
        hint="Average daily value traded below which a name is not considered."
        error={errorFor("turnover_min_inr")}
      >
        <Input
          id={ids.turnover_min_inr}
          name="turnover_min_inr"
          inputMode="decimal"
          defaultValue={config.turnover_min_inr.toFixed(2)}
          pattern="\d+(\.\d+)?"
        />
      </Field>
      <Field
        id={ids.price_min}
        label="Lowest price (₹)"
        hint="Names below this are left out."
        error={errorFor("price_min")}
      >
        <Input
          id={ids.price_min}
          name="price_min"
          inputMode="decimal"
          defaultValue={config.price_min.toFixed(2)}
          pattern="\d+(\.\d+)?"
        />
      </Field>

      <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
        <Button type="submit" variant="primary" disabled={pending}>
          {pending ? "Saving…" : "Save"}
        </Button>
        <span
          role="status"
          aria-live="polite"
          className={cn(
            "text-sm",
            result?.ok === false ? "text-negative" : "text-muted-foreground",
          )}
          data-testid="settings-outcome"
        >
          {result === null
            ? ""
            : result.ok
              ? result.message
              : result.field && result.field in ids
                ? "Not saved — see the field marked above."
                : result.error}
        </span>
      </div>
    </form>
  );
}
