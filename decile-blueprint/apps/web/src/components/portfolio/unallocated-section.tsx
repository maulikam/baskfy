"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";

import { GroupingSuggestions } from "@/components/portfolio/grouping-suggestions";
import {
  NewPortfolioFlow,
  type CreateOutcome,
  type NewPortfolioSeed,
  type SourceOption,
} from "@/components/portfolio/new-portfolio-flow";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  describeHolding,
  isUnallocated,
  NO_FIGURE,
  ORGANIZE_CTA,
  suggestionKeyIds,
  type AggregatedHolding,
  type GroupingSuggestion,
  type PortfolioDraft,
  type Unallocated,
} from "@/lib/portfolio/organize";
import { formatRupees, isPositiveDecimal } from "@/lib/portfolios/decimal";

/**
 * PORTFOLIO_REDESIGN.md §6.6: "the centerpiece, not a footer".
 *
 * This component is the answer to §1 problem 6. The old surface's empty state pointed at the
 * basket catalog, which tells a user who has just connected a broker holding forty stocks that the
 * product has nothing to say about the forty stocks. §6.6 inverts it: everything lands here, and
 * the product's job is to help sort it. **Getting from 40 unallocated holdings to 4 named
 * portfolios is the activation event**, so this section is the loudest thing on its page and it
 * never links to the catalog — not from the populated state, and not from the empty one.
 *
 * Four states, and the difference between them is load-bearing:
 *
 * 1. **No broker, no holdings** — acceptance criterion 8. One way forward: connect a broker.
 * 2. **Nothing unallocated** — a real, tidy account. Good news, said as good news.
 * 3. **Something unallocated** — the centrepiece: cash, holdings, one total, {@link ORGANIZE_CTA},
 *    and the ranked suggestions from `baskfy_core.grouping_suggestions`.
 * 4. **We could not ask** — the API did not answer. Said plainly, because "nothing is unallocated"
 *    and "we do not know" must never render the same.
 */

export interface UnallocatedSectionProps {
  /** `null` when the overview API did not answer — which is not the same as nothing unallocated. */
  unallocated: Unallocated | null;
  /** Every consolidated holding row. The picker and the unallocated list both read this. */
  rows: readonly AggregatedHolding[];
  suggestions?: readonly GroupingSuggestion[] | undefined;
  suggestionsUnavailableReason?: string | null | undefined;
  sectors?: Readonly<Record<string, string>> | undefined;
  /** How many broker accounts are connected. Zero plus zero holdings is criterion 8. */
  connectedBrokerCount: number;
  subscribedBaskets?: readonly SourceOption[] | undefined;
  screens?: readonly SourceOption[] | undefined;
  strategies?: readonly SourceOption[] | undefined;
  benchmarks?: readonly string[] | undefined;
  onCreate?: ((draft: PortfolioDraft) => Promise<CreateOutcome>) | undefined;
}

/** How many unallocated holdings are listed before the list defers to the picker. */
const PREVIEW_LIMIT = 8;

export function UnallocatedSection({
  unallocated,
  rows,
  suggestions = [],
  suggestionsUnavailableReason = null,
  sectors = {},
  connectedBrokerCount,
  subscribedBaskets = [],
  screens = [],
  strategies = [],
  benchmarks,
  onCreate,
}: UnallocatedSectionProps) {
  const [seed, setSeed] = useState<NewPortfolioSeed | null>(null);
  const [open, setOpen] = useState(false);

  const unallocatedRows = rows.filter(isUnallocated);

  function openFlow(next: NewPortfolioSeed | null): void {
    setSeed(next);
    setOpen(true);
  }

  function acceptSuggestion(suggestion: GroupingSuggestion): void {
    openFlow({
      start: "HOLDINGS",
      step: "holdings",
      name: suggestion.proposed_name,
      kind: suggestion.suggested_kind,
      selected: suggestionKeyIds(suggestion),
    });
  }

  const flow = open ? (
    <NewPortfolioFlow
      key={JSON.stringify(seed)}
      rows={rows}
      sectors={sectors}
      seed={seed}
      subscribedBaskets={subscribedBaskets}
      screens={screens}
      strategies={strategies}
      benchmarks={benchmarks}
      onCancel={() => {
        setOpen(false);
        setSeed(null);
      }}
      onCreate={onCreate}
    />
  ) : null;

  /* ------------------------------------------------------- 4. we could not ask */
  if (unallocated === null && rows.length === 0 && connectedBrokerCount === 0) {
    return <ConnectFirst onEmptyPortfolio={() => openFlow({ start: "EMPTY", step: "kind" })} flow={flow} open={open} />;
  }

  if (unallocated === null) {
    return (
      <section
        aria-label="Unallocated"
        data-testid="unallocated-section"
        data-state="unavailable"
        className="rounded-xl border border-dashed border-border bg-card/50 p-5"
      >
        <h2 className="text-base font-semibold">Unallocated</h2>
        <p className="mt-1 max-w-[60ch] text-sm text-muted-foreground">
          <span aria-hidden="true">{NO_FIGURE}</span> We could not reach the portfolio service, so
          we do not know what is unallocated. This is not the same as nothing being unallocated, and
          we would rather say so than show you a zero.
        </p>
      </section>
    );
  }

  /* ------------------------------------------------ 1. criterion 8: nothing at all */
  const nothingHeld = rows.length === 0 && unallocated.holdings_count === 0;
  if (connectedBrokerCount === 0 && nothingHeld) {
    return <ConnectFirst onEmptyPortfolio={() => openFlow({ start: "EMPTY", step: "kind" })} flow={flow} open={open} />;
  }

  /* ------------------------------------------------------ 2. nothing unallocated */
  const hasCash = isPositiveDecimal(unallocated.cash);
  const hasHoldings = unallocated.holdings_count > 0;
  if (!hasCash && !hasHoldings) {
    return (
      <section
        aria-label="Unallocated"
        data-testid="unallocated-section"
        data-state="clear"
        className="space-y-3 rounded-xl border border-border bg-card p-5"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold">Nothing unallocated</h2>
            <p className="text-sm text-muted-foreground">
              Every share and every rupee is in a portfolio. That is the tidy end of §6.6.
            </p>
          </div>
          {open ? null : (
            <Button type="button" variant="outline" size="sm" onClick={() => openFlow(null)}>
              + New portfolio
            </Button>
          )}
        </div>
        {flow}
      </section>
    );
  }

  /* ----------------------------------------------------------- 3. the centrepiece */
  const preview = unallocatedRows.slice(0, PREVIEW_LIMIT);
  const remaining = unallocatedRows.length - preview.length;

  return (
    <section
      aria-label="Unallocated"
      data-testid="unallocated-section"
      data-state="unallocated"
      className="space-y-5 rounded-xl border-2 border-accent/40 bg-card p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">Unallocated</h2>
          <p className="max-w-[58ch] text-sm text-muted-foreground">
            Everything your brokers hold that is not in a portfolio yet. Sorting it is what turns a
            pile of shares into something you can measure.
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Total</p>
          <p className="text-2xl font-semibold tabular-nums" data-testid="unallocated-total">
            {formatRupees(unallocated.total_value)}
          </p>
        </div>
      </div>

      <dl className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-lg border border-border bg-background p-3">
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">
            Unallocated cash
          </dt>
          <dd className="text-lg font-semibold tabular-nums" data-testid="unallocated-cash">
            {formatRupees(unallocated.cash)}
          </dd>
          {(unallocated.cash_by_broker ?? []).length > 0 ? (
            <dd className="mt-1 text-xs text-muted-foreground">
              {(unallocated.cash_by_broker ?? [])
                .map((row) => `${row.broker.label} ${formatRupees(row.balance)}`)
                .join(" · ")}
            </dd>
          ) : null}
        </div>

        <div className="rounded-lg border border-border bg-background p-3">
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">
            Unassigned holdings
          </dt>
          <dd className="text-lg font-semibold tabular-nums" data-testid="unallocated-holdings">
            {formatRupees(unallocated.holdings_value)}
          </dd>
          <dd className="mt-1 text-xs text-muted-foreground">
            {unallocated.holdings_count} holding{unallocated.holdings_count === 1 ? "" : "s"} in no
            portfolio
          </dd>
        </div>
      </dl>

      {unallocated.pending_reconciliation ? (
        <Badge variant="warning">
          Some of this is pending reconciliation — its contribution is frozen rather than guessed.
        </Badge>
      ) : null}

      {preview.length > 0 ? (
        <ul className="space-y-1 text-sm" data-testid="unallocated-list">
          {preview.map((row) => (
            <li key={row.instrument.instrument_id} className="flex items-baseline gap-2">
              <span className="min-w-0 flex-1">{describeHolding(row)}</span>
              <span className="shrink-0 tabular-nums text-muted-foreground">
                {row.value === null || row.value === undefined
                  ? NO_FIGURE
                  : formatRupees(row.value)}
              </span>
            </li>
          ))}
          {remaining > 0 ? (
            <li className="text-xs text-muted-foreground">and {remaining} more</li>
          ) : null}
        </ul>
      ) : null}

      {open ? null : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="primary"
            onClick={() => openFlow({ start: "HOLDINGS", step: "holdings" })}
          >
            {unallocated.cta || ORGANIZE_CTA}
          </Button>
          <Button type="button" variant="outline" onClick={() => openFlow(null)}>
            + New portfolio
          </Button>
        </div>
      )}

      {open ? (
        flow
      ) : (
        <GroupingSuggestions
          suggestions={suggestions}
          unavailableReason={suggestionsUnavailableReason}
          onAccept={acceptSuggestion}
        />
      )}
    </section>
  );
}

/**
 * Acceptance criterion 8, and the only correct answer to it: "With zero connected brokers and zero
 * holdings, the empty state leads to *Connect your broker*, not the basket catalog."
 *
 * There is deliberately no link to `/explore`, `/baskets` or anything else that lists models. A
 * user with nothing connected has nothing this product can measure; showing them a catalog first is
 * how the old surface managed to be busy and useless at the same time (§1 problem 6).
 */
function ConnectFirst({
  onEmptyPortfolio,
  flow,
  open,
}: {
  onEmptyPortfolio: () => void;
  flow: ReactNode;
  open: boolean;
}) {
  return (
    <section
      aria-label="Unallocated"
      data-testid="unallocated-section"
      data-state="no-broker"
      className="space-y-4 rounded-xl border-2 border-accent/40 bg-card p-6"
    >
      <div className="max-w-[58ch] space-y-2">
        <h2 className="text-lg font-semibold">Start with what you already own</h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Connect a broker and every share you hold lands here, in Unallocated. From there the
          product helps you sort it into named portfolios — by sector, by when you bought, or by the
          models you follow. Nothing to sort until the shares are in view.
        </p>
        <p className="text-xs text-muted-foreground">
          Read-only. Shares stay in your demat account; nothing on this page places an order.
        </p>
      </div>

      {open ? null : (
        <div className="flex flex-wrap items-center gap-3">
          <Button asChild variant="primary">
            <Link href="/brokers">Connect your broker</Link>
          </Button>
          <button
            type="button"
            onClick={onEmptyPortfolio}
            className="text-xs text-muted-foreground underline-offset-4 hover:underline"
          >
            Or set up an empty portfolio to fill in later
          </button>
        </div>
      )}

      {flow}
    </section>
  );
}
