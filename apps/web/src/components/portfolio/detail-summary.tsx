"use client";

import { Money, MoneyDelta, useAmounts } from "@/components/portfolio/amounts";
import { ReturnValue } from "@/components/portfolio/return-value";
import { Badge } from "@/components/ui/badge";
import {
  benchmarkGap,
  isMonitoringView,
  syncedAtLine,
  valuedAtLine,
  type PortfolioSummary,
} from "@/lib/portfolio/detail-view";
import {
  MONITORING_NOTE,
  describeReturn,
  formatRate,
  fromFigure,
  fromMove,
  fromRate,
  toneFor,
  type MoneyMove,
} from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * §7's first block: *"value, invested, today's P&L, total P&L, headline return metric (per §5.2),
 * benchmark diff, cash, last sync."*
 *
 * ## The two rules this block exists to keep
 *
 * **Criterion 3 — no bare percentage.** Every rate on this screen goes through
 * {@link ReturnValue}, which cannot be called without a `DisplayReturn`, which cannot be built
 * without the label and the start date the API sent. That includes the benchmark difference,
 * which arrives on the wire as an unlabelled fraction and is lifted into a labelled figure by
 * `benchmarkGap` before it can reach a component.
 *
 * **Criterion 5 — the model is never the user's return.** `headline_return` and `model_return`
 * are rendered in two separate bordered panels with two separate headings, not as two numbers in
 * one row. §5.2 says the model TWR is shown "separately" and never blended; the strongest reading
 * of "separately" that a layout can express is a border between them and a sentence on the model
 * panel saying whose record it is. `ReturnValue` also marks the model figure itself, so the two
 * are distinguishable even if this layout is ever collapsed onto one line.
 *
 * ## Last sync is two clocks
 *
 * §6.1 keeps the price date and the holdings-sync time apart, and §7's "last sync" inherits that.
 * One merged "last updated" would let a fresh broker sync vouch for a week-old close.
 */

export interface DetailSummaryProps {
  summary: PortfolioSummary;
}

export function DetailSummary({ summary }: DetailSummaryProps) {
  const monitoring = isMonitoringView(summary);
  const benchmark = summary.benchmark ?? null;

  return (
    <section aria-label="Summary" data-testid="detail-summary" className="flex flex-col gap-4">
      {monitoring ? (
        <p
          data-testid="detail-monitoring-note"
          className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
        >
          {summary.excluded_note ?? MONITORING_NOTE}
        </p>
      ) : null}

      {summary.pending_reconciliation ? (
        <p
          role="status"
          data-testid="detail-reconciliation-note"
          className="rounded-lg border border-warning/40 bg-warning-muted px-3 py-2 text-xs"
        >
          Something changed at your broker that we could not attribute on our own. Until you
          answer it, the affected holdings are held out of this portfolio&rsquo;s return series
          rather than guessed at.
        </p>
      ) : null}

      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Figure label="Value" testId="detail-value">
          <span className="text-2xl font-semibold tabular-nums">
            <Money value={summary.value} />
          </span>
        </Figure>

        <Figure
          label="Invested"
          testId="detail-invested"
          hint={summary.invested ? null : (summary.invested_unavailable_reason ?? null)}
        >
          <span className="text-2xl font-semibold tabular-nums">
            <Money value={summary.invested ?? null} />
          </span>
        </Figure>

        <MoveFigure move={summary.todays_pnl} testId="detail-todays-pnl" />
        <MoveFigure move={summary.total_pnl} testId="detail-total-pnl" />

        <Figure label="Cash in this portfolio" testId="detail-cash">
          <span className="text-lg font-semibold tabular-nums">
            <Money value={summary.cash} />
          </span>
        </Figure>

        <Figure label="Status" testId="detail-status">
          <Badge variant={summary.status === "On target" ? "positive" : "neutral"}>
            {summary.status}
          </Badge>
        </Figure>
      </dl>

      <div className="grid gap-3 md:grid-cols-2">
        <div
          data-testid="detail-your-return"
          className="flex flex-col gap-3 rounded-xl border border-border/70 bg-card p-4"
        >
          <h3 className="text-sm font-semibold">Your return, on your money</h3>
          <ReturnValue
            entry={fromFigure(summary.headline_return)}
            showReason
            size="lg"
            data-testid="detail-headline-return"
          />
          <ReturnValue entry={fromRate(summary.xirr)} showReason data-testid="detail-xirr" />
          {benchmark ? (
            <ReturnValue
              entry={benchmarkGap(benchmark)}
              showReason
              data-testid="detail-benchmark-gap"
            />
          ) : (
            <p className="text-[11px] leading-snug text-muted-foreground">
              No benchmark has been chosen for this portfolio, so there is nothing to compare it
              against.
            </p>
          )}
        </div>

        {summary.model_return ? (
          <div
            data-testid="detail-model-return-panel"
            className="flex flex-col gap-3 rounded-xl border border-dashed border-border bg-muted/20 p-4"
          >
            <h3 className="text-sm font-semibold">
              The published model&rsquo;s own record — not your money
            </h3>
            <ReturnValue
              entry={fromFigure(summary.model_return)}
              showReason
              size="lg"
              data-testid="detail-model-return"
            />
            <p className="max-w-[46ch] text-[11px] leading-snug text-muted-foreground">
              This is what {summary.publisher ?? "the publisher"} reports for the model itself. It
              is measured on the model, not on your account, and the two are never added together
              or averaged into one figure.
            </p>
          </div>
        ) : null}
      </div>

      <p data-testid="detail-timestamps" className="text-xs text-muted-foreground">
        {valuedAtLine(summary)} · {syncedAtLine(summary)}
      </p>
    </section>
  );
}

function Figure({
  label,
  children,
  hint,
  testId,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string | null;
  testId: string;
}) {
  return (
    <div data-testid={testId} className="min-w-0 rounded-xl border border-border/70 bg-card p-4">
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-1.5">
        {children}
        {hint ? (
          <p className="mt-1.5 max-w-[34ch] text-[11px] leading-snug text-muted-foreground">
            {hint}
          </p>
        ) : null}
      </dd>
    </div>
  );
}

/** Rupees on top, the percentage under, both carrying the payload's own label (criterion 3). */
function MoveFigure({ move, testId }: { move: MoneyMove; testId: string }) {
  const entry = fromMove(move);
  const { visible } = useAmounts();
  return (
    <div
      data-testid={testId}
      title={describeReturn(entry)}
      className="min-w-0 rounded-xl border border-border/70 bg-card p-4"
    >
      <dt className="eyebrow">{move.label}</dt>
      <dd className="mt-1.5">
        <p className={cn("text-2xl font-semibold tabular-nums", toneFor(move.amount))}>
          <MoneyDelta value={move.amount ?? null} />
        </p>
        <p className={cn("mt-0.5 text-sm tabular-nums", toneFor(move.pct))}>
          {visible ? formatRate(move.pct ?? null) : "••••"}
        </p>
        {move.amount === null && move.unavailable_reason ? (
          <p className="mt-1.5 max-w-[34ch] text-[11px] leading-snug text-muted-foreground">
            {move.unavailable_reason}
          </p>
        ) : null}
      </dd>
    </div>
  );
}
