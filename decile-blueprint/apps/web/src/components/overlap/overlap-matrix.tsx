"use client";

import { Check } from "lucide-react";
import { useMemo, useState } from "react";

import { InstrumentLink } from "@/components/instrument/instrument-link";
import {
  filterMembershipRows,
  type Membership,
  type MembershipColumn,
  type MembershipView,
} from "@/lib/overlap/overlap";
import { cn } from "@/lib/utils";

/**
 * Every name on Volume, Tight, Swing, and the selected screens, in one table.
 *
 * The named intersection panels still answer "who is on both of these two". This table answers
 * the other question: which names appear across the strategies, and whether the screens named
 * them too.
 */
export function OverlapMatrix({
  membership,
  screenKeys,
}: {
  membership: Membership;
  screenKeys: readonly string[];
}) {
  const [view, setView] = useState<MembershipView>("shared");
  const readable = membership.columns.filter((column) => column.available);
  const rows = useMemo(
    () => filterMembershipRows(membership.rows, view, screenKeys),
    [membership.rows, view, screenKeys],
  );
  const screenAvailable = screenKeys.some((key) =>
    membership.columns.some((column) => column.key === key && column.available),
  );
  const canThree = readable.length >= 3;

  return (
    <section
      className="space-y-3 rounded-xl border border-border/70 bg-card/40 p-5"
      data-testid="overlap-matrix"
      aria-labelledby="overlap-matrix-heading"
    >
      <div className="space-y-1">
        <h2
          id="overlap-matrix-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Across strategies and screens
        </h2>
        <p className="max-w-[72ch] text-sm leading-relaxed text-muted-foreground">
          One row per name. A mark means that scan named it today — Volume, Tight, Swing, and
          the screens you pick together, not as three separate lists.
        </p>
      </div>

      <p className="text-sm leading-relaxed" data-testid="overlap-matrix-summary">
        {membership.summary}
      </p>

      {!membership.available ? null : (
        <>
          <nav aria-label="Which names to show" className="flex flex-wrap gap-2">
            <ViewChip current={view} value="shared" onSelect={setView}>
              On 2+
            </ViewChip>
            {canThree ? (
              <ViewChip current={view} value="three" onSelect={setView}>
                On 3+
              </ViewChip>
            ) : null}
            {screenAvailable ? (
              <ViewChip current={view} value="screen" onSelect={setView}>
                On a screen
              </ViewChip>
            ) : null}
            <ViewChip current={view} value="all" onSelect={setView}>
              Every name
            </ViewChip>
          </nav>

          {rows.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="overlap-matrix-empty">
              {emptyCopy(view, screenAvailable)}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[36rem] text-sm" data-testid="overlap-matrix-table">
                <thead>
                  <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                    <th className="py-2 pr-3 font-medium">Symbol</th>
                    {readable.map((column) => (
                      <th
                        key={column.key}
                        className={cn(
                          "px-2 py-2 text-center font-medium",
                          column.key === "vbt" || column.key === "twt" || column.key === "swing"
                            ? null
                            : "max-w-[9rem] truncate",
                        )}
                        title={`${column.label} · ${column.size} names`}
                      >
                        {columnHeading(column)}
                        <span className="mt-0.5 block font-normal normal-case tracking-normal text-muted-foreground/80">
                          {column.size}
                        </span>
                      </th>
                    ))}
                    <th className="py-2 pl-3 text-right font-medium">Sources</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr
                      key={row.symbol}
                      className="border-b border-border/40"
                      data-testid="overlap-matrix-row"
                      data-symbol={row.symbol}
                      data-count={row.count}
                    >
                      <td className="py-1.5 pr-3">
                        <InstrumentLink symbol={row.symbol} className="tabular-nums" />
                      </td>
                      {readable.map((column) => {
                        const on = row.sourceKeys.includes(column.key);
                        return (
                          <td key={column.key} className="px-2 py-1.5 text-center">
                            {on ? (
                              <span
                                className="inline-flex items-center justify-center text-foreground"
                                aria-label={`On ${column.label}`}
                              >
                                <Check className="size-3.5" aria-hidden="true" />
                              </span>
                            ) : (
                              <span
                                className="text-muted-foreground/40"
                                aria-label={`Not on ${column.label}`}
                              >
                                —
                              </span>
                            )}
                          </td>
                        );
                      })}
                      <td className="py-1.5 pl-3 text-right tabular-nums text-muted-foreground">
                        {row.count}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function columnHeading(column: MembershipColumn): string {
  if (column.key === "vbt") return "Volume";
  if (column.key === "twt") return "Tight";
  if (column.key === "swing") return "Swing";
  return column.label;
}

function emptyCopy(view: MembershipView, screenAvailable: boolean): string {
  if (view === "screen" && screenAvailable) {
    return "The selected screens named no stocks on this session.";
  }
  if (view === "three") {
    return "No names sit on three or more of these scans on this session.";
  }
  if (view === "shared") {
    return "No names sit on more than one of these scans on this session.";
  }
  return "No names on this session.";
}

function ViewChip({
  current,
  value,
  onSelect,
  children,
}: {
  current: MembershipView;
  value: MembershipView;
  onSelect: (view: MembershipView) => void;
  children: string;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      data-testid={`overlap-matrix-view-${value}`}
      aria-pressed={active}
      onClick={() => onSelect(value)}
      className={cn(
        "rounded-full border px-3 py-1 text-xs transition-colors",
        active
          ? "border-foreground bg-foreground text-background"
          : "border-border text-muted-foreground hover:border-foreground/60",
      )}
    >
      {children}
    </button>
  );
}
