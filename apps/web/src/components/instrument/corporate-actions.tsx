import { AlertTriangle } from "lucide-react";

import type { CorporateActionOut } from "@baskfy/api-client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatTradeDate } from "@/lib/format";
import { actionValue, isMaterial } from "@/lib/instrument/present";

/**
 * docs/01 §5 block 11 — "type / value / ex-date table (bonus 4:1, split 10:1, bonus 1:1)".
 *
 * docs/08 adds: "flag rows that materially affect adjusted history". A split or a bonus rebases
 * every close before its ex-date (docs/05 §1), which is why a chart of the last five years can
 * disagree with a screenshot someone took at the time — the flag is there so that surprise has an
 * explanation on the page rather than in a support ticket.
 */
export interface CorporateActionsTableProps {
  actions: readonly CorporateActionOut[];
}

const MATERIAL_EXPLANATION =
  "This action rebases every adjusted close before its ex-date, so prices shown for earlier dates " +
  "are not the prices the exchange printed that day.";

export function CorporateActionsTable({ actions }: CorporateActionsTableProps) {
  if (actions.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No corporate actions are on record for this instrument.
      </p>
    );
  }

  return (
    <table className="w-full border-collapse text-sm">
      <caption className="sr-only">
        Corporate actions, newest first, with the ones that rebase adjusted history flagged
      </caption>
      <thead>
        <tr className="text-xs text-muted-foreground">
          <th scope="col" className="py-1.5 pr-3 text-left font-medium">
            Type
          </th>
          <th scope="col" className="px-3 py-1.5 text-right font-medium">
            Value
          </th>
          <th scope="col" className="py-1.5 pl-3 text-right font-medium">
            Ex-date
          </th>
        </tr>
      </thead>
      <tbody>
        {actions.map((action) => (
          <tr
            key={`${action.action_type}-${action.ex_date}`}
            className="border-t border-border"
          >
            <th scope="row" className="py-1.5 pr-3 text-left font-medium capitalize">
              <span className="flex items-center gap-1.5">
                {action.action_type}
                {isMaterial(action.action_type) ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="cursor-help text-warning">
                        <AlertTriangle aria-hidden="true" className="size-3.5" />
                        <span className="sr-only">Adjusts historical prices</span>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent className="max-w-xs">{MATERIAL_EXPLANATION}</TooltipContent>
                  </Tooltip>
                ) : null}
              </span>
            </th>
            <td className="px-3 py-1.5 text-right tnum">{actionValue(action)}</td>
            <td className="py-1.5 pl-3 text-right tnum">{formatTradeDate(action.ex_date)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
