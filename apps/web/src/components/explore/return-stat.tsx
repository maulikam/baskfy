import { EMPTY_CELL, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Headline return with its window label always visible (05-ui-spec ReturnStat).
 */
export function ReturnStat({
  label,
  value,
  className,
}: {
  label: string | null | undefined;
  value: string | number | null | undefined;
  className?: string;
}) {
  const windowLabel = label?.trim() || "Return";
  const display =
    value === null || value === undefined || value === ""
      ? EMPTY_CELL
      : formatPercent(value);

  return (
    <div className={cn("text-right", className)}>
      <div className="eyebrow">{windowLabel}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums">{display}</div>
    </div>
  );
}
