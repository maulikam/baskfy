"use client";

import type { RebalanceNameOut } from "@decile/api-client";
import { Check, Copy, Download } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  EXIT_REASON_LABELS,
  clipboardText,
  csvText,
  downloadCsv,
  filenameFor,
} from "@/lib/portfolios/export";

/**
 * One of the three columns docs/01 §8 describes — Exits, Inside WRH, Entries — with the two
 * affordances docs/08 §"Rebalance tracker" requires: "each with copy-to-clipboard and CSV".
 *
 * The reference product's layout puts Exits first and Entries last, and this keeps that order: it
 * reads as the order you would act in, closing before opening.
 *
 * A column with nothing in it says what that *means* rather than "0 results" — for this tool an
 * empty Exits column is the good outcome, and saying so is more useful than a dash.
 */

export interface ResultColumnProps {
  slug: "exits" | "inside-wrh" | "entries";
  title: string;
  description: string;
  emptyMessage: string;
  rows: readonly RebalanceNameOut[];
  asOf: string;
  tone: "negative" | "neutral" | "positive";
}

export function ResultColumn({
  slug,
  title,
  description,
  emptyMessage,
  rows,
  asOf,
  tone,
}: ResultColumnProps) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(clipboardText(rows));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  return (
    <section
      aria-labelledby={`${slug}-heading`}
      data-testid={`column-${slug}`}
      className="flex min-w-0 flex-col rounded-md border border-border"
    >
      <header className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <h3 id={`${slug}-heading`} className="text-sm font-semibold tracking-tight">
          {title}
        </h3>
        <Badge variant={tone === "neutral" ? "neutral" : tone} data-testid={`count-${slug}`}>
          {rows.length}
        </Badge>
        <div className="ml-auto flex gap-1">
          <Button
            variant="ghost"
            size="sm"
            disabled={rows.length === 0}
            onClick={() => void copy()}
            aria-label={`Copy ${title} to the clipboard`}
            data-testid={`copy-${slug}`}
          >
            {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
            {copied ? "Copied" : "Copy"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={rows.length === 0}
            onClick={() => downloadCsv(filenameFor(slug, asOf), csvText(rows))}
            aria-label={`Download ${title} as CSV`}
            data-testid={`csv-${slug}`}
          >
            <Download aria-hidden="true" />
            CSV
          </Button>
        </div>
      </header>

      <p className="border-b border-border px-3 py-2 text-xs text-muted-foreground">
        {description}
      </p>

      {rows.length === 0 ? (
        <p className="px-3 py-6 text-center text-sm text-muted-foreground">{emptyMessage}</p>
      ) : (
        <ul className="divide-y divide-border">
          {rows.map((row) => (
            <li key={row.instrument_id} className="flex items-baseline gap-2 px-3 py-2 text-sm">
              <span className="font-medium tabular-nums text-muted-foreground">
                {row.rank ?? "—"}
              </span>
              <span className="min-w-0 flex-1 truncate">
                <span className="font-medium">{row.symbol}</span>{" "}
                <span className="text-muted-foreground">{row.name}</span>
              </span>
              {row.reason ? (
                <span className="shrink-0 text-xs text-muted-foreground">
                  {EXIT_REASON_LABELS[row.reason] ?? row.reason}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
