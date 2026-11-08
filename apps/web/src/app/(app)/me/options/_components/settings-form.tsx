"use client";

import { useActionState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { OptionsConfig } from "@/lib/options/types";
import { GROUP_NAME } from "@/lib/options/view";
import type { OptionsFormResult } from "@/lib/options/write";

type SaveAction = (
  previous: OptionsFormResult | null,
  formData: FormData,
) => Promise<OptionsFormResult>;

/**
 * `op_book_config` and each `op_sleeve_config`'s money fields, with the server's ceiling under
 * every bounded box ("max 1.0% — set by the server"), read from the same response the form was
 * filled from. The pause, the execution switches and the ceilings are shown by the page, never
 * fields here.
 */
function Field({
  name,
  label,
  value,
  hint,
  type = "text",
}: {
  name: string;
  label: string;
  value: string;
  hint?: string;
  type?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span>{label}</span>
      <Input
        name={name}
        defaultValue={value}
        type={type}
        inputMode={type === "text" ? "decimal" : undefined}
      />
      {hint ? (
        <span className="text-xs text-muted-foreground">{hint}</span>
      ) : null}
    </label>
  );
}

export function OptionsSettingsForm({
  action,
  config,
}: {
  action: SaveAction;
  config: OptionsConfig;
}) {
  const [result, formAction, pending] = useActionState(action, null);
  const { book, ceilings } = config;
  return (
    <form
      action={formAction}
      className="space-y-6"
      data-testid="options-settings-form"
    >
      {book ? (
        <fieldset className="grid gap-3 sm:grid-cols-2">
          <legend className="mb-2 text-sm font-semibold">The options account</legend>
          <Field
            name="book.account_inr"
            label="Account size (₹)"
            value={book.account_inr}
          />
          <Field
            name="book.margin_pool_inr"
            label="Margin pool (₹)"
            value={book.margin_pool_inr}
          />
          <Field
            name="book.daily_loss_limit_inr"
            label="Daily loss limit (₹, 0 = derived)"
            value={book.daily_loss_limit_inr}
            hint={`max ₹${ceilings.book_daily_loss_inr_max} — set by the server`}
          />
          <Field
            name="book.monthly_pause_inr"
            label="Monthly pause (₹, 0 = derived)"
            value={book.monthly_pause_inr}
            hint={`max ₹${ceilings.book_monthly_loss_inr_max} — set by the server`}
          />
        </fieldset>
      ) : null}
      {config.sleeves.map((sleeve) => (
        <fieldset key={sleeve.sleeve} className="grid gap-3 sm:grid-cols-3">
          <legend className="mb-2 text-sm font-semibold">
            {GROUP_NAME[sleeve.sleeve]}
          </legend>
          <Field
            name={`${sleeve.sleeve}.sleeve_capital_inr`}
            label="Capital for this strategy (₹)"
            value={sleeve.sleeve_capital_inr}
            hint={`risk per trade at most ₹${ceilings.risk_per_trade_inr_max} — set by the server`}
          />
          <Field
            name={`${sleeve.sleeve}.risk_per_trade_pct`}
            label="Risk per trade (%)"
            value={sleeve.risk_per_trade_pct}
            hint={`max ${ceilings.risk_pct_max}% — set by the server`}
          />
          <Field
            name={`${sleeve.sleeve}.max_lots`}
            label="Most lots"
            value={String(sleeve.max_lots)}
            hint={`max ${ceilings.max_lots_max} — set by the server`}
          />
          <Field
            name={`${sleeve.sleeve}.hard_exit_time`}
            label="Hard exit"
            value={sleeve.hard_exit_time.slice(0, 5)}
            type="time"
            hint={`no later than ${ceilings.hard_exit_latest.slice(0, 5)} — set by the server`}
          />
          <label className="flex flex-col gap-1 text-sm">
            <span>Run on paper</span>
            <select
              name={`${sleeve.sleeve}.paper_enabled`}
              defaultValue={sleeve.paper_enabled ? "yes" : "no"}
              className="h-9 rounded-md border border-input bg-background px-2"
            >
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </label>
        </fieldset>
      ))}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={pending}>
          {pending ? "Saving…" : "Save"}
        </Button>
        {result ? (
          <p
            role={result.ok ? "status" : "alert"}
            data-testid="options-settings-result"
            className={
              result.ok ? "text-sm text-positive" : "text-sm text-negative"
            }
          >
            {result.ok ? result.message : result.error}
          </p>
        ) : null}
      </div>
    </form>
  );
}
