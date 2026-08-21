"use client";

import Link from "next/link";
import { ArrowUpRight, X } from "lucide-react";

import type { ColumnMeta, ResultRow } from "@/components/screens/result-columns";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { EMPTY_CELL, formatCrore, formatFraction, formatNumber, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * docs/08 §"Results panel": "click a row to open a **peek drawer** with the factsheet's top blocks
 * without leaving the screen."
 *
 * It shows what the *row* already carries rather than fetching the factsheet: the point of a peek
 * is that it is instant. The link out to the full factsheet is right there for anyone who wants
 * the whole thing.
 *
 * A side sheet rather than a centred dialog, because the table behind it stays legible — which is
 * the difference between a peek and an interruption.
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

export function PeekDrawer({
  row,
  columns,
  meta,
  sortingFactorLabel,
  onClose,
}: PeekDrawerProps) {
  const symbol = typeof row?.symbol === "string" ? row.symbol : "";
  const name = typeof row?.name === "string" ? row.name : "";

  return (
    <Dialog open={row !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/20 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full max-w-sm flex-col gap-4 overflow-y-auto border-l border-border bg-popover p-5",
            "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
          )}
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <DialogTitle className="text-lg font-semibold tracking-tight tnum">
                {symbol}
              </DialogTitle>
              <DialogDescription className="text-sm text-muted-foreground">
                {name || "Result detail"}
              </DialogDescription>
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" className="size-8" aria-label="Close preview">
                <X aria-hidden="true" className="size-4" />
              </Button>
            </DialogPrimitive.Close>
          </div>

          <dl className="divide-y divide-border">
            {columns
              .filter((key) => key !== "symbol" && key !== "name")
              .map((key) => {
                const entry = meta.get(key);
                const label = key === "sorting_factor" ? sortingFactorLabel : (entry?.label ?? key);
                const unit = key === "sorting_factor" ? "ratio" : (entry?.unit ?? "ratio");
                return (
                  <div key={key} className="flex items-baseline justify-between gap-4 py-2">
                    <dt className="text-xs text-muted-foreground">{label}</dt>
                    <dd className="text-sm tnum">{display(row?.[key], unit)}</dd>
                  </div>
                );
              })}
          </dl>

          <Button variant="outline" size="sm" asChild className="mt-auto">
            <Link href={`/instruments/${symbol}`}>
              Open the full factsheet
              <ArrowUpRight aria-hidden="true" />
            </Link>
          </Button>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </Dialog>
  );
}
