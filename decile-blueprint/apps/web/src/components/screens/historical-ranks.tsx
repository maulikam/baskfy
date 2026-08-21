"use client";

import type { ScreenDefinition } from "@decile/api-client";

import { Field, SwitchRow } from "@/components/screens/field";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { formatTradeDate } from "@/lib/format";
import type { Patch } from "@/components/screens/filter-sections";

/**
 * docs/01 §2.13 and Prompt 9 deliverable 9:
 *
 *     "Historical Ranks: date picker **restricted to trading days from /meta/trading-days**, with
 *      a clear banner when viewing a historical date."
 *
 * A `<select>` of real trading days rather than `<input type="date">`, because a free date input
 * lets a user pick a Sunday or a holiday and get a 422 back. The API snaps a non-trading day
 * backwards (docs/06 §step 1) and refuses one outside the servable range altogether (docs/07a §1);
 * offering only the dates it will accept means neither ever happens.
 *
 * The banner itself is in the editor's header, where it is visible with the results rather than
 * buried in a collapsed accordion group.
 */
export interface HistoricalRanksProps {
  definition: ScreenDefinition;
  patch: Patch;
  disabled?: boolean;
  tradingDays: readonly string[];
  dataStartDate: string | null;
  latestDate: string | null;
}

/** Newest first: a user reaching for a historical date usually wants a recent one. */
const MAX_OPTIONS = 400;

export function HistoricalRanks({
  definition,
  patch,
  disabled,
  tradingDays,
  dataStartDate,
  latestDate,
}: HistoricalRanksProps) {
  const enabled = definition.historical_date !== null;
  const options = [...tradingDays].sort((a, b) => b.localeCompare(a)).slice(0, MAX_OPTIONS);
  const mostRecent = options[0] ?? latestDate;

  return (
    <>
      <SwitchRow
        label="Apply Historical Date"
        hint={
          dataStartDate
            ? `Re-runs the whole screen as of a past date. History begins ${formatTradeDate(dataStartDate)}.`
            : "Re-runs the whole screen as of a past date."
        }
        render={({ id, describedBy }) => (
          <Switch
            id={id}
            aria-describedby={describedBy}
            disabled={disabled || mostRecent === null}
            checked={enabled}
            data-testid="historical-toggle"
            onCheckedChange={(checked) =>
              patch({ historical_date: checked ? (mostRecent ?? null) : null })
            }
          />
        )}
      />

      {enabled ? (
        <Field
          label="Historical date"
          hint="Only NSE trading days inside the published range are offered."
          render={({ id, describedBy }) => (
            <Select
              id={id}
              aria-describedby={describedBy}
              disabled={disabled}
              value={definition.historical_date ?? ""}
              data-testid="historical-date"
              onChange={(event) => patch({ historical_date: event.currentTarget.value || null })}
            >
              {options.map((day) => (
                <option key={day} value={day}>
                  {formatTradeDate(day)}
                </option>
              ))}
            </Select>
          )}
        />
      ) : null}
    </>
  );
}
