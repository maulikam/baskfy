"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EMPTY_CELL, formatNumber } from "@/lib/format";

/**
 * Net-worth header with eye-toggle — docs/smallcase/05 §6.8.
 */

export function NetWorthHeader({
  netWorth,
  investmentCount,
}: {
  netWorth: string | null;
  investmentCount: number;
}) {
  const [visible, setVisible] = useState(true);
  const amount =
    netWorth === null || netWorth === ""
      ? EMPTY_CELL
      : `₹${formatNumber(netWorth, { decimals: 0 })}`;

  return (
    <section
      aria-label="Net worth"
      className="flex flex-wrap items-end justify-between gap-3 rounded-xl border border-border/70 bg-card p-4"
    >
      <div>
        <div className="eyebrow">Net worth in baskets</div>
        <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">
          {visible ? amount : "••••••"}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          {investmentCount === 0
            ? "No active investments yet"
            : `${investmentCount} active investment${investmentCount === 1 ? "" : "s"}`}
        </p>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-pressed={!visible}
        onClick={() => setVisible((v) => !v)}
      >
        {visible ? "Hide amounts" : "Show amounts"}
      </Button>
    </section>
  );
}
