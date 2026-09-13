"use client";

import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Desk Momentum Quality Score A–F breakdown (docs/ranking/PLAN.md Phase 1.2 UI).
 *
 * Values come from the screen result payload when ``sort_by=desk_score`` — the API attaches
 * ``desk_a_trend``…``desk_f_penalty`` from the book's ``score()``, not a second formula.
 */

export const DESK_EXPLAIN_KEYS = [
  "desk_a_trend",
  "desk_b_momentum",
  "desk_c_sharpe",
  "desk_d_consistency",
  "desk_e_liquidity",
  "desk_f_penalty",
  "desk_reject",
  "desk_eligible",
] as const;

export type DeskExplainKey = (typeof DESK_EXPLAIN_KEYS)[number];

const PARTS: ReadonlyArray<{ key: DeskExplainKey; label: string }> = [
  { key: "desk_a_trend", label: "A · Trend" },
  { key: "desk_b_momentum", label: "B · Momentum" },
  { key: "desk_c_sharpe", label: "C · Sharpe" },
  { key: "desk_d_consistency", label: "D · Consistency" },
  { key: "desk_e_liquidity", label: "E · Liquidity" },
  { key: "desk_f_penalty", label: "F · Penalty" },
];

export function hasDeskExplainColumns(columns: readonly string[]): boolean {
  return columns.includes("desk_a_trend") || columns.includes("desk_eligible");
}

export function isDeskExplainKey(key: string): boolean {
  return (DESK_EXPLAIN_KEYS as readonly string[]).includes(key);
}

function num(row: Record<string, unknown>, key: string): number | null {
  const raw = row[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

export interface DeskScoreBreakdownProps {
  row: Record<string, unknown>;
  className?: string;
}

export function DeskScoreBreakdown({ row, className }: DeskScoreBreakdownProps) {
  const reject = typeof row.desk_reject === "string" ? row.desk_reject : "";
  const eligible = row.desk_eligible === true || (row.desk_eligible !== false && reject === "");

  return (
    <div
      className={cn("vaaya-stat space-y-3 p-4", className)}
      data-testid="desk-score-breakdown"
    >
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-xs font-medium text-muted-foreground">Desk score · A–F</p>
        <p
          className={cn(
            "text-xs tabular-nums",
            eligible ? "text-muted-foreground" : "text-negative",
          )}
          data-testid="desk-eligible"
        >
          {eligible ? "Eligible" : reject || "Rejected"}
        </p>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
        {PARTS.map(({ key, label }) => {
          const value = num(row, key);
          return (
            <div key={key} className="flex items-baseline justify-between gap-2">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="text-sm tabular-nums" data-testid={key}>
                {value === null ? "—" : formatNumber(value, { decimals: 1 })}
              </dd>
            </div>
          );
        })}
      </dl>
    </div>
  );
}
