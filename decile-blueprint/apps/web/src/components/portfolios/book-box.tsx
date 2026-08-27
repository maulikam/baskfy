import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { EMPTY_CELL, direction, formatNumber, formatPercent } from "@/lib/format";
import { BOX_KIND_LABEL, type BookBox } from "@/lib/portfolios/book";
import { cn } from "@/lib/utils";

function rupees(value: string | null): string {
  if (value === null || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 0 })}`;
}

function signedClass(value: string | null): string {
  const dir = direction(value === null ? null : Number(value));
  if (dir === "up") return "text-positive";
  if (dir === "down") return "text-negative";
  return "";
}

function Stat({
  label,
  value,
  className,
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <div>
      <p className="eyebrow">{label}</p>
      <p className={cn("mt-0.5 text-sm tabular-nums", className)}>{value}</p>
    </div>
  );
}

/**
 * One slice of the portfolio group. Stats that exist on this ledger are on the card; missing
 * marks stay as
 * an em dash rather than a made-up NAV.
 */
export function BookBoxCard({ box }: { box: BookBox }) {
  const hero = box.valueIsLiveMark
    ? rupees(box.currentValue)
    : box.capital !== null
      ? rupees(box.capital)
      : box.holdingsCount !== null
        ? `${box.holdingsCount} holding${box.holdingsCount === 1 ? "" : "s"}`
        : EMPTY_CELL;
  const heroLabel = box.valueIsLiveMark
    ? "Current value"
    : box.capital !== null
      ? "Assigned capital"
      : box.holdingsCount !== null
        ? "Names in this portfolio"
        : "No figure yet";

  return (
    <li>
      <Link
        href={box.href as never}
        data-testid="book-box"
        data-kind={box.kind}
        className="flex h-full flex-col gap-3 rounded-xl border border-border/70 bg-card p-4 transition-colors hover:border-foreground/40"
      >
        <div className="flex items-start justify-between gap-2">
          <p className="min-w-0 truncate text-sm font-semibold">{box.name}</p>
          <Badge variant="outline">{BOX_KIND_LABEL[box.kind]}</Badge>
        </div>
        <div>
          <p className="eyebrow">{heroLabel}</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">{hero}</p>
        </div>
        {box.valueIsLiveMark ? (
          <div className="mt-auto grid grid-cols-3 gap-2">
            <Stat
              label="Return"
              value={box.returnsPct === null ? EMPTY_CELL : formatPercent(box.returnsPct)}
              className={signedClass(box.returnsPct)}
            />
            <Stat
              label="XIRR"
              value={box.xirr === null ? EMPTY_CELL : formatPercent(box.xirr)}
              className={signedClass(box.xirr)}
            />
            <Stat label="Put in" value={rupees(box.capital)} />
          </div>
        ) : (
          <p className="mt-auto text-xs leading-relaxed text-muted-foreground">
            {box.capital !== null
              ? "Assigned capital, not a live mark."
              : "Names you uploaded. No live mark on this portfolio yet."}
          </p>
        )}
      </Link>
    </li>
  );
}
