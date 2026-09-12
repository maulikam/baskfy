"use client";

import type { IndexRowOut } from "@baskfy/api-client";

import { EMPTY_CELL, formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * AFH 5.6 — pinned headline strip so NIFTY 50 is not buried at row ~60 of a move-sorted table.
 */

const HEADLINE_SLUGS = [
  "nifty-50",
  "nifty-bank",
  "nifty-midcap-150",
  "nifty-smallcap-250",
  "india-vix",
] as const;

const HEADLINE_LABEL: Record<(typeof HEADLINE_SLUGS)[number], string> = {
  "nifty-50": "NIFTY 50",
  "nifty-bank": "Bank Nifty",
  "nifty-midcap-150": "Midcap 150",
  "nifty-smallcap-250": "Smallcap 250",
  "india-vix": "India VIX",
};

function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const numeric = typeof value === "string" ? Number(value) : value;
  return Number.isNaN(numeric) ? null : numeric;
}

function pick(rows: readonly IndexRowOut[], slug: string): IndexRowOut | undefined {
  return rows.find((row) => row.slug === slug);
}

export function HeadlineStrip({ rows }: { rows: readonly IndexRowOut[] }) {
  return (
    <ul
      data-testid="market-headline-strip"
      className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5"
    >
      {HEADLINE_SLUGS.map((slug) => {
        const row = pick(rows, slug);
        const change = toNumber(row?.change_pct);
        return (
          <li
            key={slug}
            data-testid={`headline-${slug}`}
            className="rounded-lg border border-border/70 bg-card px-3 py-2"
          >
            <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              {HEADLINE_LABEL[slug]}
            </p>
            <p className="mt-0.5 text-sm font-semibold tabular-nums">
              {row?.level === null || row?.level === undefined
                ? EMPTY_CELL
                : formatNumber(row.level, { decimals: 2 })}
            </p>
            <p
              className={cn(
                "text-xs tabular-nums",
                change !== null && change > 0 && "text-positive",
                change !== null && change < 0 && "text-negative",
                change === null && "text-muted-foreground",
              )}
            >
              {change === null ? EMPTY_CELL : formatPercent(change)}
            </p>
          </li>
        );
      })}
    </ul>
  );
}
