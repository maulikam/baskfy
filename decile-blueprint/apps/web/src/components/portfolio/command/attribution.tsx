"use client";

import type { Route } from "next";
import Link from "next/link";
import { CircleAlert, Info, Lock } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type {
  Attribution as AttributionModel,
  AttributionBand,
  ContributionRow,
} from "@/lib/portfolio/performance";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * Performance attribution — which portfolio moved the number, and what cannot be decomposed.
 *
 * Brief: *"performance attribution: contribution by portfolio, by holding, by sector, allocation
 * effect, security selection effect, cash drag, fees and taxes."* Exactly one of those seven is
 * real at this level, and this panel is built around saying so rather than around filling seven
 * cards. `lib/portfolio/performance.ts` computes the one that is real and carries
 * `NOT_DECOMPOSABLE` — the other six with, for each, what is actually missing and what would
 * unblock it.
 *
 * ## WHY THE SHARE BAR IS A SHARE OF THE **GROSS** MOVE
 *
 * On a day one portfolio gained ₹10,000 and another lost ₹9,000, the book moved ₹1,000 net. A bar
 * showing "share of the move" against that denominator reads 1,000% and −900%, which is
 * arithmetically correct and useless. The denominator here is every contribution's absolute size
 * added together, the label says gross, and when the signs are mixed the panel says that too —
 * because "the portfolios moved ₹19,000 between them to produce ₹1,000" is the fact a reader of
 * this panel actually wants.
 *
 * ## THE RECONCILIATION IS ALWAYS SHOWN, INCLUDING WHEN IT FAILS
 *
 * The rows add up, or they do not and the gap is named. A residual folded into the largest row,
 * or quietly dropped, turns a checkable panel into a decorative one. The usual cause is real and
 * worth surfacing: holdings filed into no portfolio count towards net worth and belong to no
 * strategy, so nothing on this screen can say how they did.
 *
 * Nothing here is phrased as advice. Baskfy is not registered to give it (D3), so the copy
 * describes what the data says and never what to do about it.
 */

/** Tabular numerals, matching the comparison table — a column of figures aligns on its decimals. */
const FIGURE = "tabular-nums tracking-tight";

/** Shades of the one accent rather than a rainbow: this is an ordering, not a set of categories. */
const SHADES = ["bg-accent", "bg-accent/75", "bg-accent/55", "bg-accent/40", "bg-accent/25"] as const;

function shade(index: number): string {
  return SHADES[index % SHADES.length] ?? SHADES[0];
}

/** The non-colour half of the identifier, exactly as `comparison-table` draws it. */
function initialOf(name: string): string {
  return name.trim().charAt(0).toUpperCase() || "•";
}

function toneOf(value: string | null): "up" | "down" | "flat" {
  if (value === null) return "flat";
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

/** A signed rupee figure with its direction as a glyph as well as a hue. */
function Move({ value }: { value: string }) {
  const tone = toneOf(value);
  return (
    <span
      className={cn(
        FIGURE,
        "font-medium",
        tone === "up" && "text-positive",
        tone === "down" && "text-negative",
      )}
    >
      {tone !== "flat" ? (
        <span aria-hidden="true" className="mr-0.5 text-[0.8em]">
          {tone === "up" ? "▲" : "▼"}
        </span>
      ) : null}
      <span className="sr-only">{tone === "up" ? "gain of " : tone === "down" ? "loss of " : ""}</span>
      {formatRupees(value)}
    </span>
  );
}

function Row({ row, rank }: { row: ContributionRow; rank: number }) {
  const share = row.sharePct;
  return (
    <li
      data-testid={`attribution-row-${row.portfolioId}`}
      className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5"
    >
      <span
        aria-hidden="true"
        className={cn(
          "flex size-5 shrink-0 items-center justify-center rounded text-[10px] font-semibold text-accent-foreground",
          shade(rank),
        )}
      >
        {initialOf(row.name)}
      </span>
      {/* Drill-down. The brief: *"Drill down from every chart into the relevant holdings."* An
          attribution row says a portfolio moved the number; the question it raises is WHICH
          holding did, and that is the portfolio's own workspace. A row that names a cause and
          cannot be followed is a picture you cannot ask a question of. */}
      <Link
        href={`/portfolio/${row.portfolioId}` as Route}
        data-testid={`attribution-link-${row.portfolioId}`}
        className="min-w-0 flex-1 truncate text-sm font-medium underline-offset-4 hover:underline"
      >
        {row.name}
      </Link>

      <span className="flex w-28 shrink-0 justify-end text-sm">
        {row.amount.value === null ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="flex cursor-help items-center gap-1 text-xs text-muted-foreground">
                <CircleAlert aria-hidden="true" className="size-3 shrink-0 text-warning" />
                Not available
              </span>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs text-xs leading-relaxed">
              {row.amount.unavailable}
            </TooltipContent>
          </Tooltip>
        ) : (
          <Move value={row.amount.value} />
        )}
      </span>

      <span className="flex w-40 shrink-0 items-center gap-2">
        <span
          aria-hidden="true"
          className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted"
        >
          <span
            className={cn("block h-full rounded-full", toneOf(row.amount.value) === "down" ? "bg-negative" : "bg-positive")}
            style={{ width: share === null ? "0%" : `${Math.min(Number(share), 100)}%` }}
          />
        </span>
        <span className={cn("w-14 shrink-0 text-right text-xs text-muted-foreground", FIGURE)}>
          {share === null ? "no share" : `${share}%`}
        </span>
      </span>

      <span className={cn("w-20 shrink-0 text-right text-xs text-muted-foreground", FIGURE)}>
        {row.weightPct === null ? "unweighted" : `${row.weightPct}% of total`}
      </span>

      {row.partialFrom !== null ? (
        <span className="w-full text-xs leading-snug text-warning">
          Measured from {row.partialFrom}, which is where this portfolio&apos;s own series starts —
          not from the left edge of the window.
        </span>
      ) : null}
    </li>
  );
}

function Band({ band, testId }: { band: AttributionBand; testId: string }) {
  return (
    <section
      aria-label={band.title}
      data-testid={testId}
      className="rounded-xl border border-border bg-card"
    >
      <div className="border-b border-border px-4 py-2.5">
        <h3 className="text-sm font-semibold">{band.title}</h3>
        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{band.definition}</p>
      </div>

      {band.unavailable !== null ? (
        <p className="flex items-start gap-2 px-4 py-4 text-sm">
          <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          <span>
            <strong className="font-medium">Not available.</strong>{" "}
            <span className="text-muted-foreground">{band.unavailable}</span>
          </span>
        </p>
      ) : (
        <>
          <ul className="divide-y divide-border/60">
            {band.rows.map((row, index) => (
              <Row key={row.portfolioId} row={row} rank={index} />
            ))}
          </ul>

          {band.mixedSigns && band.gross !== null ? (
            <p
              data-testid={`${testId}-offset`}
              className="border-t border-border px-4 py-2 text-xs leading-snug text-muted-foreground"
            >
              Gains and losses offset here: the portfolios moved{" "}
              <strong className="font-medium text-foreground">{formatRupees(band.gross)}</strong>{" "}
              between them, which is what the bars are shares of.
            </p>
          ) : null}

          <p
            data-testid={`${testId}-reconciliation`}
            className={cn(
              "flex items-start gap-2 border-t border-border px-4 py-2.5 text-xs leading-snug",
              band.reconciliation.reconciles ? "text-muted-foreground" : "text-foreground",
            )}
          >
            <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
            <span>
              {band.reconciliation.explained === null || band.reconciliation.reported === null ? (
                band.reconciliation.note
              ) : (
                <>
                  These add to{" "}
                  <strong className={cn("font-medium", FIGURE)}>
                    {formatRupees(band.reconciliation.explained)}
                  </strong>
                  ; the consolidated figure is{" "}
                  <strong className={cn("font-medium", FIGURE)}>
                    {formatRupees(band.reconciliation.reported)}
                  </strong>
                  .{" "}
                  {band.reconciliation.reconciles ? null : (
                    <>
                      The difference of{" "}
                      <strong className={cn("font-medium", FIGURE)}>
                        {formatRupees(band.reconciliation.residual)}
                      </strong>{" "}
                    </>
                  )}
                  {band.reconciliation.note}
                </>
              )}
            </span>
          </p>
        </>
      )}
    </section>
  );
}

export function Attribution({ attribution }: { attribution: AttributionModel }) {
  return (
    <section aria-label="Performance attribution" data-testid="attribution" className="space-y-4">
      <Band band={attribution.today} testId="attribution-today" />
      <Band band={attribution.period} testId="attribution-period" />

      {/* The six the brief names and Baskfy cannot compute. Named on the surface that would have
          shown them, each with what is missing and what would unblock it — never as a dash, and
          never as a plausible figure. Baskfy places live orders.

          CLOSED BY DEFAULT, and that is a correction rather than a preference. Open, these six
          entries are the tallest thing on the screen: a screenshot of an EMPTY account measured
          4,661px on a phone, most of it this list explaining what an account with no holdings
          cannot be broken down by. The brief asks to "avoid a very long page" and to "use tabs,
          drawers, expandable rows and sticky controls to progressively reveal detail", and the
          rule it collided with — never a bare dash, always the reason — is satisfied by the
          reason being one click away and labelled, not by it being unavoidable. The summary line
          carries the count, so nothing is hidden; it is folded. */}
      <details
        data-testid="attribution-blocked"
        className="group rounded-xl border border-border bg-card [&_summary::-webkit-details-marker]:hidden"
      >
        <summary className="flex cursor-pointer list-none items-start gap-1.5 px-4 py-2.5">
          <Lock aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-semibold">
              What this cannot be broken down by, and why
              <span className="ml-1.5 font-normal text-muted-foreground">
                ({attribution.blocked.length})
              </span>
            </span>
            <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">
              Sector, allocation and selection effects, cash drag, fees, and contribution per
              holding. None is shown as a dash or as an estimate.
            </span>
          </span>
          <span
            aria-hidden="true"
            className="mt-0.5 shrink-0 text-xs text-muted-foreground transition-transform duration-150 group-open:rotate-90"
          >
            ▸
          </span>
        </summary>
        <ul className="divide-y divide-border/60 border-t border-border">
          {attribution.blocked.map((effect) => (
            <li key={effect.name} className="px-4 py-2.5">
              <p className="flex items-center gap-1.5 text-sm font-medium">
                {effect.name}
                <span className="rounded-full bg-muted px-1.5 py-0.5 text-[0.6875rem] font-normal text-muted-foreground">
                  Not available
                </span>
              </p>
              <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{effect.reason}</p>
              <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                <span className="font-medium text-foreground">Needs:</span> {effect.unblockedBy}
              </p>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
