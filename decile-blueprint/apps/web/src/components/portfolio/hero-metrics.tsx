"use client";

import { ChevronDown } from "lucide-react";
import { useState } from "react";

import { Money, MoneyDelta, useAmounts } from "@/components/portfolio/amounts";
import { ReturnValue } from "@/components/portfolio/return-value";
import { EMPTY_CELL } from "@/lib/format";
import {
  describeReturn,
  formatRate,
  fromMove,
  fromRate,
  toneFor,
  type Hero,
} from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * §6.2 — five hero metrics, and not a sixth.
 *
 * *"Current value · Today's P&L (₹ and %, vs previous close) · Total P&L · XIRR · Invested
 * amount."* The cap is the design: a row of nine numbers is a row nobody reads, and the five
 * chosen are the five that answer "how much do I have, how did today go, how has it gone, what
 * rate is that, and what did I put in".
 *
 * **TWR is deliberately in the secondary row, not the hero.** §5.2 wants XIRR *and* TWR at the
 * consolidated level as two labelled numbers, and the payload carries both — but §6.2's list of
 * five names XIRR and stops. XIRR is the one that answers "what did *I* experience", which is
 * the question a person opening their own portfolio is asking; TWR answers "how good is the
 * strategy", which is a comparison question and belongs beside the benchmark. Promoting it would
 * mean six hero metrics, and the ceiling is the part of §6.2 that is easiest to erode and hardest
 * to win back.
 *
 * Every rate goes through {@link ReturnValue}, so none of them can appear unlabelled (§11
 * criterion 3). Every rupee figure goes through the show/hide-amounts context (§6.1).
 */

function Metric({
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
    <div
      data-testid={testId}
      className="min-w-0 flex-1 basis-40 rounded-xl border border-border/70 bg-card p-4"
    >
      <div className="eyebrow">{label}</div>
      <div className="mt-1.5">{children}</div>
      {hint ? (
        <p className="mt-1.5 max-w-[34ch] text-[11px] leading-snug text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}

/** A money move: rupees on top, its percentage under, both carrying the payload's own label. */
function MoveFigure({ move, testId }: { move: Hero["todays_pnl"]; testId: string }) {
  const entry = fromMove(move);
  const { visible } = useAmounts();
  return (
    <div title={describeReturn(entry)} data-testid={testId}>
      <p className={cn("text-2xl font-semibold tabular-nums", toneFor(move.amount))}>
        <MoneyDelta value={move.amount ?? null} />
      </p>
      <p className={cn("mt-0.5 text-sm tabular-nums", toneFor(move.pct))}>
        {visible ? formatRate(move.pct ?? null) : "••••"}
      </p>
      <p className="mt-0.5 text-[11px] leading-tight text-muted-foreground">{move.label}</p>
      {move.amount === null && move.unavailable_reason ? (
        <p className="mt-1 max-w-[34ch] text-[11px] leading-snug text-muted-foreground">
          {move.unavailable_reason}
        </p>
      ) : null}
    </div>
  );
}

export function HeroMetrics({ hero }: { hero: Hero }) {
  const [secondaryOpen, setSecondaryOpen] = useState(false);
  const secondary = hero.secondary;

  return (
    <section aria-label="Portfolio totals" className="space-y-3">
      <div className="flex flex-wrap gap-3" data-testid="hero-metrics">
        <Metric
          label="Current value"
          testId="hero-current-value"
          hint={
            hero.pending_reconciliation
              ? "Some holdings are waiting on reconciliation and are held out of this figure until you resolve them."
              : null
          }
        >
          <p className="text-2xl font-semibold tabular-nums tracking-tight">
            <Money value={hero.current_value} />
          </p>
        </Metric>

        <Metric label="Today" testId="hero-todays-pnl">
          <MoveFigure move={hero.todays_pnl} testId="todays-pnl-figure" />
        </Metric>

        <Metric label="Total P&L" testId="hero-total-pnl">
          <MoveFigure move={hero.total_pnl} testId="total-pnl-figure" />
        </Metric>

        <Metric label="Return" testId="hero-xirr">
          <ReturnValue
            entry={fromRate(hero.xirr)}
            size="lg"
            showReason
            data-testid="hero-xirr-value"
          />
        </Metric>

        <Metric
          label="Invested"
          testId="hero-invested"
          hint={
            hero.invested === null || hero.invested === undefined
              ? (hero.invested_unavailable_reason ?? null)
              : hero.holdings_without_cost_basis
                ? `${hero.holdings_without_cost_basis} holdings have no purchase price recorded and are not in this figure.`
                : null
          }
        >
          <p className="text-2xl font-semibold tabular-nums tracking-tight">
            {hero.invested === null || hero.invested === undefined ? (
              EMPTY_CELL
            ) : (
              <Money value={hero.invested} />
            )}
          </p>
        </Metric>
      </div>

      <div className="rounded-xl border border-border/70 bg-card">
        <button
          type="button"
          aria-expanded={secondaryOpen}
          aria-controls="hero-secondary"
          data-testid="hero-secondary-toggle"
          onClick={() => setSecondaryOpen((open) => !open)}
          className="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-sm font-medium"
        >
          More numbers
          <ChevronDown
            aria-hidden="true"
            className={cn(
              "size-4 text-muted-foreground motion-safe:transition-transform motion-safe:duration-150",
              secondaryOpen && "rotate-180",
            )}
          />
        </button>
        {secondaryOpen ? (
          <dl
            id="hero-secondary"
            data-testid="hero-secondary"
            className="grid grid-cols-2 gap-x-6 gap-y-3 border-t border-border/70 px-4 py-3 sm:grid-cols-3 lg:grid-cols-5"
          >
            <div>
              <dt className="text-xs text-muted-foreground">Cash</dt>
              <dd className="text-sm tabular-nums">
                <Money value={secondary.cash} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">{secondary.realised_pnl.label}</dt>
              <dd className={cn("text-sm tabular-nums", toneFor(secondary.realised_pnl.amount))}>
                <MoneyDelta value={secondary.realised_pnl.amount ?? null} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">{secondary.unrealised_pnl.label}</dt>
              <dd className={cn("text-sm tabular-nums", toneFor(secondary.unrealised_pnl.amount))}>
                <MoneyDelta value={secondary.unrealised_pnl.amount ?? null} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Dividends</dt>
              <dd className="text-sm tabular-nums">
                <Money value={secondary.dividends} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Brokers</dt>
              <dd className="text-sm tabular-nums">{secondary.broker_count}</dd>
            </div>
            <div className="col-span-2 sm:col-span-3 lg:col-span-5">
              <dt className="sr-only">Strategy quality</dt>
              <dd>
                <ReturnValue entry={fromRate(hero.twr)} showReason data-testid="hero-twr-value" />
              </dd>
            </div>
          </dl>
        ) : null}
      </div>
    </section>
  );
}
