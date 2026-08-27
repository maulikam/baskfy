import type { Metadata } from "next";
import Link from "next/link";

import { dismissPendingAction } from "@/app/actions/pending-actions";
import { CollectionsGrid } from "@/components/home/collections-grid";
import { PendingActionsCarousel } from "@/components/home/pending-actions-carousel";
import { TrendingModule } from "@/components/home/trending-module";
import { UpdatesStrip } from "@/components/home/updates-strip";
import { NetWorthHeader } from "@/components/investments/net-worth-header";
import { PageHeader } from "@/components/shell/page-header";
import { fetchHome } from "@/lib/home/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/home` — the signed-in landing surface (docs/smallcase/05 §6.1, SC9).
 *
 * **Why this is not `/dashboard`.** `/dashboard` was the screener's market dashboard and now
 * permanently redirects to `/market/today`: it answers "what did the market do today", which is
 * a question about the world. This page answers "what is mine, and what needs me" — a question
 * about one person's position. Putting both on one route was the thing Tree 6 untangled, and
 * merging them back would undo it.
 *
 * Four modules, in the order somebody actually reads them: what I have, what needs a decision,
 * what changed, and what I might look at next. Every one of them degrades to an honest empty
 * state on its own (`lib/home/fetch`), so an unreachable API costs one module rather than the
 * page a person lands on.
 *
 * **Nothing here executes.** No Invest CTA on this surface reaches an order path; the catalog
 * and the investment pages own that flow and they end in a plan hand-off, not an order.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/home"].title,
  description: PAGES["/home"].blurb,
  robots: { index: false, follow: false },
};

export default async function HomePage() {
  const { investments, trending, collections, updates } = await fetchHome();
  const active = investments.items.filter((row) => row.status === "ACTIVE");

  return (
    <div className="flex flex-col gap-7">
      <PageHeader title={PAGES["/home"].title} blurb={PAGES["/home"].blurb} />

      <NetWorthHeader
        netWorth={investments.net_worth}
        investmentCount={active.length}
        href="/portfolio/overview"
      />

      <PendingActionsCarousel
        actions={investments.pending_actions}
        dismiss={dismissPendingAction}
      />

      <UpdatesStrip updates={updates} />

      <TrendingModule trending={trending} />

      <CollectionsGrid collections={collections} />

      {active.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nothing invested yet.{" "}
          <Link href="/discover" className="text-accent underline-offset-4 hover:underline">
            Browse the catalog
          </Link>{" "}
          — investing records a ledger entry here; it never places an order.
        </p>
      ) : null}
    </div>
  );
}
