"use client";

import type { Route } from "next";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EMPTY_CELL, formatNumber } from "@/lib/format";

/**
 * Net-worth header with eye-toggle — docs/smallcase/05 §6.8, and §6.1's home headline.
 *
 * One component for both surfaces rather than two. `/home` shows the same figure as
 * `/me/investments` and reads it from the same payload; a second net-worth block with its own
 * copy of the formatting is how two pages start disagreeing about the same person's money.
 * `href` is what §6.1 calls the chevron — on home it opens the full ledger, on the ledger page
 * itself there is nowhere to go and it is omitted.
 */

export function NetWorthHeader({
  netWorth,
  investmentCount,
  href,
}: {
  netWorth: string | null;
  investmentCount: number;
  /** Where the headline leads. Omitted on the page it would lead to. */
  href?: Route;
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
        {href ? (
          <Link
            href={href}
            data-testid="net-worth-link"
            className="mt-2 inline-block text-xs text-accent underline-offset-4 hover:underline"
          >
            See every investment →
          </Link>
        ) : null}
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
