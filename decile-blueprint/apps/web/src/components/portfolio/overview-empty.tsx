import Link from "next/link";

import { Button } from "@/components/ui/button";
import type { Overview } from "@/lib/portfolio/overview";

/**
 * §11 criterion 8: *"With zero connected brokers and zero holdings, the empty state leads to
 * 'Connect your broker', not the basket catalog."*
 *
 * §1 problem 6 is what the criterion is defending. The old page funnelled a new user into
 * Explore, which is catalog-first: it asks somebody who already owns forty stocks to start by
 * shopping. §6.6 makes the opposite claim — the user's existing demat holdings are the starting
 * material, and *"getting from 40 unallocated holdings to 4 named portfolios IS activation"*. So
 * the one primary action here connects a broker.
 *
 * The catalog is still reachable, as a quiet secondary line, because somebody genuinely starting
 * from zero does need it. It is a link, not a button, and it is second.
 */

/** True when there is nothing at all: no broker, no portfolio, nothing unallocated. */
export function isEmptyAccount(overview: Overview): boolean {
  const nothingGrouped =
    (overview.portfolios ?? []).length === 0 && (overview.monitoring_views ?? []).length === 0;
  const nothingLoose =
    overview.unallocated.holdings_count === 0 && Number(overview.unallocated.total_value) === 0;
  return (overview.sync_status ?? []).length === 0 && nothingGrouped && nothingLoose;
}

export function OverviewEmpty() {
  return (
    <section
      data-testid="overview-empty"
      className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center"
    >
      <h2 className="text-lg font-semibold">Start with the shares you already own</h2>
      <p className="mt-2 max-w-[56ch] text-sm leading-relaxed text-muted-foreground">
        Connect a broker and every holding appears here — yours to sort into portfolios you can
        measure separately. The connection is read-only: nothing is bought or sold, and your
        shares never leave your demat account.
      </p>
      <Button variant="primary" size="sm" asChild className="mt-5">
        <Link href="/brokers" data-testid="empty-connect-broker">
          Connect your broker
        </Link>
      </Button>
      <p className="mt-4 text-xs text-muted-foreground">
        Nothing to connect yet?{" "}
        <Link href="/discover" className="text-accent underline-offset-4 hover:underline">
          Look through the baskets
        </Link>
      </p>
    </section>
  );
}
