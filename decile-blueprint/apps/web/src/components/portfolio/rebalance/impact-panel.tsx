"use client";

import { ArrowRight, CircleAlert, Clock, Info, ShieldQuestion } from "lucide-react";

import type { Metric } from "@/lib/portfolio/command-center";
import {
  NOT_PRODUCED_HERE,
  type ConcentrationMove,
  type PreviewWarning,
  type RebalancePreview,
} from "@/lib/portfolio/rebalance-preview";
import { cn } from "@/lib/utils";

/**
 * What changes about the shape of the book — and the list of everything the brief asked for that
 * Baskfy cannot honestly answer.
 *
 * THE SECOND HALF IS THE POINT
 * ----------------------------
 * The brief's impact list is: quantity changes, buy and sell values, cash required or released,
 * turnover, estimated brokerage and taxes, tax-lot treatment, and exposure, concentration, risk
 * and regime before and after. Exactly one of those — concentration — is arithmetic over numbers
 * that exist. The rest need a price, a cost model, a purchase date or a covariance matrix, and
 * none of the four is in this product.
 *
 * A panel that quietly omitted them would leave a reader assuming the drawer had considered
 * costs. A panel that showed them as dashes would say nothing. So they are listed, by name, each
 * with why it is not here and where the figure genuinely comes from. `docs/PORTFOLIO-COMMAND-
 * CENTER.md` §2.2 is the survey this list is drawn from.
 *
 * WHY THE "AFTER" FIGURES CAN GO UNAVAILABLE
 * ------------------------------------------
 * Keeping a name the screen exits puts the book outside the target the server computed, and there
 * is no non-invented answer to "what is the largest weight then". It says so, naming the names.
 */

const FIGURE = "tabular-nums tracking-tight";

const MOVE: Record<ConcentrationMove, { word: string; detail: string; className: string }> = {
  "less-concentrated": {
    word: "Less concentrated",
    detail: "the largest single weight is smaller after than it is now",
    className: "text-positive",
  },
  "more-concentrated": {
    word: "More concentrated",
    detail: "the largest single weight is bigger after than it is now",
    className: "text-warning",
  },
  unchanged: {
    word: "Unchanged",
    detail: "the largest single weight is the same on both sides",
    className: "text-muted-foreground",
  },
  unknown: {
    word: "Not comparable",
    detail: "one of the two sides has no figure — the reason is beside it",
    className: "text-muted-foreground",
  },
};

function Figure({ metric, testId }: { metric: Metric; testId?: string }) {
  return (
    <div data-testid={testId}>
      <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
        {metric.label}
      </p>
      {metric.value === null ? (
        <p className="mt-1 flex items-start gap-1 text-xs leading-snug text-muted-foreground">
          <CircleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warning" />
          {metric.unavailable}
        </p>
      ) : (
        <p className={cn("mt-1 text-lg font-semibold", FIGURE)} title={metric.definition}>
          {metric.value}%
        </p>
      )}
    </div>
  );
}

function WarningRow({ warning }: { warning: PreviewWarning }) {
  const severe = warning.level === "critical";
  return (
    <li className="flex gap-2 px-3 py-2.5" data-testid={`warning-${warning.id}`}>
      {severe ? (
        <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-negative" />
      ) : (
        <Clock aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
      )}
      <div className="min-w-0">
        <p className="text-sm font-medium">
          {warning.headline}
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {severe ? "Critical" : "Review"}
          </span>
        </p>
        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{warning.detail}</p>
      </div>
    </li>
  );
}

export function ImpactPanel({ preview }: { preview: RebalancePreview }) {
  const { impact, warnings } = preview;
  const move = MOVE[impact.concentrationMove];

  return (
    <section aria-label="Impact" data-testid="impact-panel" className="space-y-4">
      <div className="rounded-xl border border-border bg-card">
        <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border px-3 py-2.5">
          <h3 className="text-sm font-semibold">Shape of this portfolio</h3>
          <p className="text-xs text-muted-foreground">
            <span className={cn("font-medium", move.className)}>{move.word}</span> — {move.detail}.
          </p>
        </header>

        <div className="grid gap-4 px-3 py-3 sm:grid-cols-2 xl:grid-cols-4">
          <Figure metric={impact.largestNow} testId="largest-now" />
          <Figure metric={impact.largestAfter} testId="largest-after" />
          <Figure metric={impact.topFiveNow} testId="top-five-now" />
          <Figure metric={impact.topFiveAfter} testId="top-five-after" />
        </div>

        <div className="flex flex-wrap gap-x-6 gap-y-1 border-t border-border px-3 py-2.5 text-xs text-muted-foreground">
          <span data-testid="name-count">
            {impact.namesNow === null ? (
              /* Never 0. A book nobody fetched holds an unknown number of names. */
              <span className="inline-flex items-center gap-1">
                <CircleAlert aria-hidden="true" className="size-3.5 shrink-0 text-warning" />
                Holdings not loaded, so how many names you hold now is unknown —
              </span>
            ) : (
              <>
                <strong className={cn("font-semibold text-foreground", FIGURE)}>
                  {impact.namesNow}
                </strong>{" "}
                names now
                <ArrowRight aria-hidden="true" className="mx-1 inline size-3" />
              </>
            )}
            <strong className={cn("font-semibold text-foreground", FIGURE)}>
              {impact.namesAfter}
            </strong>{" "}
            after
          </span>
          <span>
            <strong className={cn("font-semibold text-foreground", FIGURE)}>{impact.entering}</strong>{" "}
            entering ·{" "}
            <strong className={cn("font-semibold text-foreground", FIGURE)}>{impact.exiting}</strong>{" "}
            exiting ·{" "}
            <strong className={cn("font-semibold text-foreground", FIGURE)}>{impact.staying}</strong>{" "}
            staying
          </span>
          {impact.pricedOnly ? (
            <span className="flex items-center gap-1">
              <Info aria-hidden="true" className="size-3.5 shrink-0" />
              The current side covers priced holdings only.
            </span>
          ) : null}
        </div>
      </div>

      <div className="rounded-xl border border-border bg-card" data-testid="coverage">
        <header className="border-b border-border px-3 py-2.5">
          <h3 className="text-sm font-semibold">What your exclusions do to the target</h3>
        </header>
        <div className="grid gap-4 px-3 py-3 sm:grid-cols-2">
          <Figure metric={impact.coverage} testId="coverage-figure" />
          <Figure metric={impact.leftUnassigned} testId="unassigned-figure" />
        </div>
        <p className="border-t border-border px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">
          An excluded name&rsquo;s weight is left where it falls. Baskfy does not spread it over
          the names you kept: the target weights are the screen&rsquo;s own equal split, and
          re-splitting them here would be this drawer inventing an allocation nothing computed. To
          get a genuine target over fewer names, run the diff again with a smaller top N.
        </p>
      </div>

      <div className="rounded-xl border border-border bg-card" data-testid="preview-warnings">
        <header className="flex items-baseline gap-2 border-b border-border px-3 py-2.5">
          <h3 className="text-sm font-semibold">Warnings</h3>
          <span className={cn("text-xs text-muted-foreground", FIGURE)}>{warnings.length}</span>
        </header>
        {warnings.length === 0 ? (
          <p className="px-3 py-3 text-xs text-muted-foreground">
            Nothing is delisted, every holding has a price, the holdings reconcile, and the screen ran
            on the same session your holdings are marked at.
          </p>
        ) : (
          <ul className="divide-y divide-border/60">
            {warnings.map((warning) => (
              <WarningRow key={warning.id} warning={warning} />
            ))}
          </ul>
        )}
      </div>

      <NotProducedHereList />
    </section>
  );
}

/**
 * The honest half of the impact list. Every item names where the figure comes from instead —
 * "coming soon" is not something a person can act on.
 */
export function NotProducedHereList() {
  return (
    <div className="rounded-xl border border-border bg-card" data-testid="not-produced-here">
      <header className="flex items-start gap-2 border-b border-border px-3 py-2.5">
        <ShieldQuestion aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div>
          <h3 className="text-sm font-semibold">What this preview does not produce</h3>
          <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
            A rebalance answers with names and target weights. Everything below needs a price, a
            cost model, a purchase date or a statistics job that Baskfy does not have, and none of
            it is estimated here — a figure with nothing behind it is one a person may size a
            position against.
          </p>
        </div>
      </header>
      <ul className="divide-y divide-border/60">
        {NOT_PRODUCED_HERE.map((item) => (
          <li key={item.id} className="px-3 py-2.5" data-testid={`not-produced-${item.id}`}>
            <p className="text-sm font-medium">{item.name}</p>
            <p className="mt-0.5 text-xs leading-snug text-muted-foreground">{item.reason}</p>
            <p className="mt-1 text-xs leading-snug">
              <span className="font-medium text-brand-strong">Comes from instead: </span>
              <span className="text-muted-foreground">{item.insteadFrom}</span>
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}
