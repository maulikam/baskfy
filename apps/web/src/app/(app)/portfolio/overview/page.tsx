import type { Metadata } from "next";
import Link from "next/link";

import { InvestmentActions } from "@/components/investments/investment-actions";
import { NetWorthHeader } from "@/components/investments/net-worth-header";
import { PendingActionCard } from "@/components/pending-action-card";
import { PortfolioOverviewScreen } from "@/components/portfolio/overview-screen";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchInvestments, type InvestmentList } from "@/lib/investments/fetch";
import { fetchPortfolioOverview } from "@/lib/portfolio/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/overview` — §6's single screen (PORTFOLIO_REDESIGN.md §2's landing tab).
 *
 * The v0 that stood here — a net-worth header, a pending-actions slot and a list of basket rows
 * carried over from the old Me → Investments tab — is **replaced**, not extended. Everything it
 * did, §6 does better: the net-worth figure is now the first of five hero metrics (§6.2) with
 * today's move and a labelled return beside it, the pending actions are the needs-attention
 * ribbon that deep-links to a resolution flow (§6.4), and the basket rows are the portfolio table
 * with source badges, per-row returns and an inspector drawer (§6.5).
 *
 * ## The fallback below is not dead code
 *
 * `GET /portfolio/overview` can be unreachable — a deploy, an outage, a user whose ledger has not
 * been built yet. `fetchPortfolioOverview` returns `null` for that case rather than an empty
 * account, because "we could not ask" and "you own nothing" are opposite messages and only one of
 * them is reassuring. When it is `null`, this page falls back to the older `/cb/investments`
 * read, which is a different service and frequently still answering, and says plainly at the top
 * that this is the reduced view. Rendering a blank screen with a spinner instead would delete a
 * working net-worth figure to make a point about architecture.
 *
 * §6.6's Unallocated centrepiece and §6.7's new-portfolio flow are a sibling's work; they compose
 * into `PortfolioOverviewScreen` as children, between the ribbon and the table.
 *
 * Order-shaped CTAs hand off; nothing here executes.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/portfolio/overview"].title,
  description: PAGES["/portfolio/overview"].blurb,
  robots: { index: false, follow: false },
};

export default async function PortfolioOverviewPage() {
  const overview = await fetchPortfolioOverview();

  if (overview === null) {
    return <ReducedView list={await fetchInvestments()} />;
  }

  return (
    <div className="flex flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PortfolioOverviewScreen overview={overview} />
    </div>
  );
}

/**
 * What the reader gets when the §6 route did not answer: the older ledger read, labelled as the
 * reduced view it is. No return percentages appear here — §5.2's headline metric is exactly what
 * the older payload cannot produce, and an unlabelled one is what §11 criterion 3 forbids.
 */
function ReducedView({ list }: { list: InvestmentList }) {
  const active = list.items.filter((row) => row.status === "ACTIVE");

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader
        title="My Portfolio"
        blurb="Your complete investment picture across baskets and brokers."
      />

      <p
        role="status"
        className="rounded-xl border border-warning/40 bg-warning-muted px-4 py-2.5 text-sm"
      >
        The full picture could not be loaded just now, so this is what we can still show you:
        what you hold, from the investment ledger. Today&rsquo;s move, the chart and the
        per-portfolio returns are missing rather than wrong.
      </p>

      <NetWorthHeader netWorth={list.net_worth} investmentCount={active.length} />

      {list.pending_actions.length > 0 ? (
        <section aria-label="Needs attention" className="space-y-2">
          <h2 className="text-sm font-semibold">Needs a decision</h2>
          <ul className="space-y-2">
            {list.pending_actions.map((action) => (
              <li key={action.id}>
                <PendingActionCard type={action.type} title={action.title} body={action.body} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {active.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            Nothing is recorded here yet. Connect a broker and the shares you already own appear
            here, ready to sort into portfolios.
          </p>
          <Link
            href="/brokers"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Connect your broker
          </Link>
        </div>
      ) : (
        <ul className="space-y-3" aria-label="What you hold">
          {active.map((row) => (
            <li key={row.id} className="space-y-3 rounded-xl border border-border/70 bg-card p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <Link
                    href={`/portfolio/${row.id}`}
                    className="text-sm font-semibold text-foreground underline-offset-4 hover:underline"
                  >
                    {row.basket_name}
                  </Link>
                  {row.rebalance_pending ? (
                    <p className="mt-1 text-xs text-accent">Rebalance update available</p>
                  ) : null}
                </div>
                <Link
                  href={`/portfolio/${row.id}`}
                  className="text-xs text-muted-foreground underline-offset-4 hover:underline"
                >
                  Open
                </Link>
              </div>
              <InvestmentActions basketName={row.basket_name} />
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-muted-foreground">
        Read-only. This page never places an order.
      </p>
    </div>
  );
}
