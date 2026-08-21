"use client";

import type { ImportReportOut, ImportRowOut } from "@baskfy/api-client";
import { AlertTriangle, CheckCircle2, HelpCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";

/**
 * The parse report — PROMPTS.md Prompt 14 §1 and docs/08 §"Rebalance tracker":
 *
 *     "returns a parse report listing matched, ambiguous and unmatched symbols rather than
 *      silently dropping rows"
 *
 *     "Show unmatched symbols prominently rather than silently dropping."
 *
 * Prominently, so: unmatched and ambiguous rows are listed in full, at the top, with the line
 * number from the file and the reason in words. Matched rows are a count — a user who uploaded
 * forty symbols does not need forty green ticks, they need the three that did not work.
 *
 * Every string here explains a *server* verdict; the reasons are the API's enum values, mapped to
 * sentences once, in this file.
 */

const UNMATCHED_REASONS: Record<string, string> = {
  unknown_symbol: "Not an NSE symbol we hold data for.",
  bse_code: "That looks like a BSE scrip code. We hold NSE instruments — use the NSE symbol.",
  invalid: "Not a trading symbol.",
};

const SKIP_REASONS: Record<string, string> = {
  blank: "Blank line",
  no_symbol: "No symbol in that row",
  duplicate: "Already listed above",
  truncated: "Beyond the row limit",
};

const ROW_ISSUES: Record<string, string> = {
  suffix_stripped: "“.NS” removed",
  unreadable_quantity: "Quantity not a number — left empty",
  unreadable_avg_price: "Average price not a number — left empty",
};

export interface ImportReportProps {
  report: ImportReportOut;
}

export function ImportReport({ report }: ImportReportProps) {
  const problems = report.rows.filter((row) => row.status !== "matched");

  return (
    <section className="space-y-4" data-testid="import-report" aria-label="Import report">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Count icon={<CheckCircle2 className="size-4" />} label="imported" value={report.imported} />
        {report.ambiguous > 0 ? (
          <Count
            icon={<HelpCircle className="size-4" />}
            label="ambiguous"
            value={report.ambiguous}
            tone="warn"
          />
        ) : null}
        {report.unmatched > 0 ? (
          <Count
            icon={<AlertTriangle className="size-4" />}
            label="unmatched"
            value={report.unmatched}
            tone="warn"
          />
        ) : null}
        {report.skipped > 0 ? (
          <Count label="skipped rows" value={report.skipped} tone="muted" />
        ) : null}
      </div>

      {problems.length > 0 ? (
        <ul className="divide-y divide-border rounded-md border border-border" data-testid="import-problems">
          {problems.map((row) => (
            <li key={`${row.line}-${row.symbol}`} className="flex flex-col gap-1 p-3 text-sm">
              <ProblemRow row={row} />
            </li>
          ))}
        </ul>
      ) : null}

      {report.skipped_rows.length > 0 ? (
        <details className="rounded-md border border-border p-3 text-sm">
          <summary className="cursor-pointer text-muted-foreground">
            {report.skipped_rows.length} row{report.skipped_rows.length === 1 ? "" : "s"} produced
            no holding
          </summary>
          <ul className="mt-2 space-y-1 text-muted-foreground">
            {report.skipped_rows.map((row) => (
              <li key={`${row.line}-${row.reason}`}>
                Line {row.line}: {SKIP_REASONS[row.reason] ?? row.reason}
                {row.raw ? ` — ${row.raw}` : ""}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {report.ignored_columns.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          Columns ignored: {report.ignored_columns.join(", ")}.
        </p>
      ) : null}
    </section>
  );
}

function ProblemRow({ row }: { row: ImportRowOut }) {
  if (row.status === "ambiguous") {
    return (
      <>
        <span className="font-medium">
          {row.symbol} <span className="text-muted-foreground">(line {row.line})</span>
        </span>
        <span className="text-muted-foreground">
          More than one instrument uses that symbol. Not imported — pick one and enter it as{" "}
          {row.candidates.map((candidate) => `${candidate.symbol}·${candidate.series ?? "—"}`).join(" or ")}.
        </span>
      </>
    );
  }
  return (
    <>
      <span className="font-medium">
        {row.raw_symbol || row.symbol}{" "}
        <span className="text-muted-foreground">(line {row.line})</span>
      </span>
      <span className="text-muted-foreground">
        {UNMATCHED_REASONS[row.reason ?? ""] ?? "Could not be resolved."}
        {row.issues.length > 0
          ? ` ${row.issues.map((issue) => ROW_ISSUES[issue] ?? issue).join("; ")}.`
          : ""}
      </span>
    </>
  );
}

interface CountProps {
  label: string;
  value: number;
  icon?: React.ReactNode;
  tone?: "warn" | "muted";
}

function Count({ label, value, icon, tone }: CountProps) {
  return (
    <Badge variant={tone === "warn" ? "warning" : "neutral"}>
      {icon ? <span aria-hidden="true">{icon}</span> : null}
      {value} {label}
    </Badge>
  );
}
