"use client";

import Link from "next/link";
import { ArrowUpRight, ChevronDown, X } from "lucide-react";
import { useState } from "react";

import {
  BumpinessDots,
  ReturnChip,
  ScoreBar,
} from "@/components/screens/cell-encodings";
import type { ColumnMeta, ResultRow } from "@/components/screens/result-columns";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  EMPTY_CELL,
  formatCrore,
  formatFraction,
  formatNumber,
  formatPercent,
} from "@/lib/format";
import { columnDisplayLabel } from "@/lib/screens/column-display";
import { cn } from "@/lib/utils";

/**
 * Mini factsheet peek (§2.4): hero + encodings + collapsed "All numbers".
 * Desktop: right sheet. Mobile (<700px): bottom sheet. Motion: 200ms, motion-safe.
 *
 * No 1-year chart: preview rows have no history series. Fetching
 * GET /instruments/{symbol}/history per peek would N+1 the history endpoint.
 * Reversible later if preview grows a sparkline field.
 */
export interface PeekDrawerProps {
  row: ResultRow | null;
  columns: readonly string[];
  meta: ReadonlyMap<string, ColumnMeta>;
  sortingFactorLabel: string;
  onClose: () => void;
}

function display(value: unknown, unit: string): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  if (unit === "percent") return formatPercent(value as number);
  if (unit === "fraction") return formatFraction(value as number);
  if (unit === "crore") return formatCrore(value as number);
  if (unit === "rupees" || unit === "count") return formatNumber(value as number, { decimals: 0 });
  if (unit === "index") return formatNumber(value as number, { decimals: 4 });
  if (unit === "price" || unit === "ratio") return formatNumber(value as number, { decimals: 2 });
  return typeof value === "string" || typeof value === "number" ? String(value) : EMPTY_CELL;
}

function num(row: ResultRow | null, key: string): number | null {
  if (!row) return null;
  const raw = row[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/** Already in the title; everything else belongs under All numbers. */
const TITLE_KEYS = new Set(["symbol", "name"]);

export function PeekDrawer({
  row,
  columns,
  meta,
  sortingFactorLabel,
  onClose,
}: PeekDrawerProps) {
  const symbol = typeof row?.symbol === "string" ? row.symbol : "";
  const name = typeof row?.name === "string" ? row.name : "";
  const [allOpen, setAllOpen] = useState(false);
  const [openForSymbol, setOpenForSymbol] = useState(symbol);
  if (symbol !== openForSymbol) {
    setOpenForSymbol(symbol);
    setAllOpen(false);
  }

  const price = num(row, "close_raw");
  const ret = num(row, "ret_12m");
  const score = num(row, "sorting_factor");
  const vol = num(row, "vol_12m");
  const volLabel = vol !== null ? formatFraction(vol) : null;
  const scoreLabel = sortingFactorLabel || columnDisplayLabel("sorting_factor", "Consistency score");

  const numberKeys = columns.filter(
    (key, index) => !TITLE_KEYS.has(key) && columns.indexOf(key) === index,
  );

  return (
    <Dialog
      open={row !== null}
      onOpenChange={(open) => {
        if (!open) {
          setAllOpen(false);
          onClose();
        }
      }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-foreground/25",
            "data-[state=open]:animate-in data-[state=open]:fade-in-0",
            "data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
            "motion-safe:duration-200 motion-reduce:animate-none",
          )}
        />
        <DialogPrimitive.Content
          data-testid="peek-drawer"
          className={cn(
            "fixed z-50 flex flex-col gap-4 overflow-y-auto overscroll-contain border-border bg-card p-5 shadow-lg outline-none",
            // Mobile: bottom sheet
            "inset-x-0 bottom-0 max-h-[85vh] rounded-t-[26px] border-t",
            // Desktop: right drawer
            "min-[700px]:inset-y-0 min-[700px]:right-0 min-[700px]:left-auto min-[700px]:max-h-none min-[700px]:w-full min-[700px]:max-w-sm min-[700px]:rounded-l-[26px] min-[700px]:border-l min-[700px]:border-t-0",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0",
            "data-[state=open]:slide-in-from-bottom-4 data-[state=closed]:slide-out-to-bottom-4",
            "min-[700px]:data-[state=open]:slide-in-from-right-4 min-[700px]:data-[state=closed]:slide-out-to-right-4",
            "motion-safe:duration-200 motion-reduce:animate-none",
          )}
        >
          <div
            aria-hidden="true"
            className="mx-auto h-1 w-10 shrink-0 rounded-full bg-muted-foreground/30 min-[700px]:hidden"
          />
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <DialogTitle className="vaaya-display text-3xl tabular-nums">
                {symbol}
              </DialogTitle>
              <DialogDescription className="truncate text-sm text-muted-foreground">
                {name || "Result detail"}
              </DialogDescription>
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" className="size-8 shrink-0" aria-label="Close preview">
                <X aria-hidden="true" className="size-4" />
              </Button>
            </DialogPrimitive.Close>
          </div>

          {price !== null || ret !== null ? (
            <div className="flex flex-wrap items-end gap-3">
              {price !== null ? (
                <p className="text-3xl font-semibold tabular-nums">
                  ₹{formatNumber(price, { decimals: 2 })}
                </p>
              ) : null}
              {ret !== null ? <ReturnChip text={formatPercent(ret)} value={ret} /> : null}
            </div>
          ) : null}

          {score !== null || vol !== null ? (
            <div className="vaaya-stat space-y-3 p-4">
              {score !== null ? (
                <div>
                  <p className="mb-1 text-xs text-muted-foreground">{scoreLabel}</p>
                  <ScoreBar value={score} />
                </div>
              ) : null}
              {vol !== null ? (
                <div className="flex items-center justify-between gap-2" title={volLabel ?? undefined}>
                  <p className="text-xs text-muted-foreground">
                    {columnDisplayLabel("vol_12m", "Bumpiness")}
                  </p>
                  <span className="flex items-center gap-2 text-xs tabular-nums">
                    <BumpinessDots value={vol} />
                    {volLabel}
                  </span>
                </div>
              ) : null}
            </div>
          ) : null}

          {numberKeys.length > 0 ? (
            <div>
              <button
                type="button"
                className="flex w-full items-center justify-between py-1 text-sm font-medium"
                aria-expanded={allOpen}
                aria-controls="peek-all-numbers-list"
                data-testid="peek-all-numbers"
                onClick={() => setAllOpen((open) => !open)}
              >
                All numbers
                <ChevronDown
                  aria-hidden="true"
                  className={cn(
                    "size-4 text-muted-foreground motion-safe:transition-transform motion-safe:duration-150",
                    allOpen && "rotate-180",
                  )}
                />
              </button>
              {allOpen ? (
                <dl id="peek-all-numbers-list" className="mt-1 divide-y divide-border">
                  {numberKeys.map((key) => {
                    const entry = meta.get(key);
                    const label =
                      key === "sorting_factor"
                        ? scoreLabel
                        : columnDisplayLabel(key, entry?.label ?? key);
                    const unit = key === "sorting_factor" ? "ratio" : (entry?.unit ?? "ratio");
                    return (
                      <div key={key} className="flex items-baseline justify-between gap-4 py-2">
                        <dt className="text-xs text-muted-foreground">{label}</dt>
                        <dd className="text-sm tabular-nums">{display(row?.[key], unit)}</dd>
                      </div>
                    );
                  })}
                </dl>
              ) : null}
            </div>
          ) : null}

          {symbol ? (
            <Button variant="secondary" size="sm" asChild className="mt-auto rounded-full px-5">
              <Link href={`/instruments/${encodeURIComponent(symbol)}`}>
                Open the full factsheet
                <ArrowUpRight aria-hidden="true" />
              </Link>
            </Button>
          ) : null}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </Dialog>
  );
}
