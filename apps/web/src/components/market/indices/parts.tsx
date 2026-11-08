"use client";

import type { ReactNode } from "react";

import { EMPTY_CELL, formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

export const num = (v: number | null | undefined, decimals = 2) => formatNumber(v, { decimals });
export const pct = (v: number | null | undefined) => formatPercent(v);

export function toneOf(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return "";
  return v > 0 ? "text-positive" : "text-negative";
}

export function Pct({ value, className }: { value: number | null | undefined; className?: string }) {
  return <span className={cn("tabular-nums", toneOf(value), className)}>{pct(value)}</span>;
}

export const GROUP_SHORT: Record<string, string> = {
  "Broad market": "Broad",
  Sectoral: "Sector",
  Thematic: "Theme",
  Strategy: "Strategy",
};

export function GroupPill({ group }: { group: string }) {
  return (
    <span className="inline-block rounded-full border border-border px-2 py-px text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground">
      {GROUP_SHORT[group] ?? group}
    </span>
  );
}

export function Check({ on }: { on: boolean | null | undefined }) {
  if (on === null || on === undefined) return <span className="text-muted-foreground">{EMPTY_CELL}</span>;
  return on ? (
    <span className="text-positive" aria-label="10-day above 20-day">
      ✓
    </span>
  ) : (
    <span className="text-muted-foreground" aria-label="10-day below 20-day">
      ·
    </span>
  );
}

export function HitsBadge({ count }: { count: number | null | undefined }) {
  if (!count) return <span className="text-muted-foreground">{EMPTY_CELL}</span>;
  return (
    <span className="inline-flex min-w-6 justify-center rounded-full bg-accent-muted px-2 py-px text-xs font-semibold tabular-nums text-accent">
      {count}
    </span>
  );
}

export function HitTags({ hits }: { hits: readonly string[] }) {
  if (hits.length === 0) return <span className="text-muted-foreground">{EMPTY_CELL}</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {hits.map((h) => (
        <span
          key={h}
          className="rounded-full bg-accent-muted px-1.5 py-px text-[10.5px] font-semibold tracking-wide text-accent"
        >
          {h}
        </span>
      ))}
    </span>
  );
}

export function SortHeader({
  label,
  active,
  dir,
  onClick,
  align = "right",
}: {
  label: ReactNode;
  active: boolean;
  dir: 1 | -1;
  onClick: () => void;
  align?: "left" | "right";
}) {
  return (
    <th
      scope="col"
      aria-sort={active ? (dir === 1 ? "ascending" : "descending") : "none"}
      className={cn(
        "whitespace-nowrap px-3 py-2 text-xs font-medium text-muted-foreground",
        align === "right" ? "text-right" : "text-left",
      )}
    >
      <button
        type="button"
        onClick={onClick}
        className={cn("inline-flex items-center gap-1 hover:text-foreground", active && "text-foreground")}
      >
        {label}
        <span aria-hidden="true" className={cn(!active && "invisible")}>
          {dir === 1 ? "↑" : "↓"}
        </span>
      </button>
    </th>
  );
}

/** Nulls sort last in both directions. */
export function compareNullable(a: number | string | null | undefined, b: number | string | null | undefined, dir: 1 | -1): number {
  const an = a === null || a === undefined;
  const bn = b === null || b === undefined;
  if (an && bn) return 0;
  if (an) return 1;
  if (bn) return -1;
  if (typeof a === "string" || typeof b === "string") return String(a).localeCompare(String(b)) * dir;
  return (a - b) * dir;
}

export function RangeBar({
  label,
  lo,
  hi,
  at,
}: {
  label: string;
  lo: number | null | undefined;
  hi: number | null | undefined;
  at: number | null | undefined;
}) {
  const pos =
    lo === null || lo === undefined || hi === null || hi === undefined || at === null || at === undefined || hi <= lo
      ? null
      : Math.min(100, Math.max(0, ((at - lo) / (hi - lo)) * 100));
  return (
    <div>
      <div className="mb-1.5 flex justify-between gap-2">
        <span className="eyebrow">{label}</span>
        <span className="text-xs tabular-nums text-muted-foreground">
          {pos === null ? "not published" : `${Math.round(pos)}% of range`}
        </span>
      </div>
      <div className="relative h-1.5 rounded-full bg-muted">
        {pos === null ? null : (
          <>
            <div className="absolute inset-y-0 left-0 rounded-full bg-accent/40" style={{ width: `${pos}%` }} />
            <div
              className="absolute -top-[3px] h-3 w-[3px] -translate-x-1/2 rounded-sm bg-foreground"
              style={{ left: `${pos}%` }}
            />
          </>
        )}
      </div>
      <div className="mt-1 flex justify-between text-xs tabular-nums text-muted-foreground">
        <span>{num(lo)}</span>
        <span>{num(hi)}</span>
      </div>
    </div>
  );
}

export function Stat({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border/70 bg-background/60 px-3 py-2">
      <dt className="eyebrow">{k}</dt>
      <dd className="mt-0.5 text-sm font-semibold tabular-nums">{children}</dd>
    </div>
  );
}
