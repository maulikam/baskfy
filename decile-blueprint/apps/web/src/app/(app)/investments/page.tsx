import type { Metadata } from "next";
import Link from "next/link";

import { InvestmentActions } from "@/components/investments/investment-actions";
import { NetWorthHeader } from "@/components/investments/net-worth-header";
import { PendingActionCard } from "@/components/pending-action-card";
import { PageHeader } from "@/components/shell/page-header";
import { fetchInvestments } from "@/lib/investments/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/investments` — SC6. Net-worth header, pending-actions slot, grouped rows.
 * Order-shaped CTAs hand off; nothing here executes.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/investments"].title,
  description: PAGES["/investments"].blurb,
  robots: { index: false, follow: false },
};

export default async function InvestmentsPage() {
  const list = await fetchInvestments();
  const active = list.items.filter((row) => row.status === "ACTIVE");
  const exited = list.items.filter((row) => row.status !== "ACTIVE");

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader title={PAGES["/investments"].title} blurb={PAGES["/investments"].blurb} />

      <NetWorthHeader netWorth={list.net_worth} investmentCount={active.length} />

      {list.pending_actions.length > 0 ? (
        <section aria-label="Pending actions" className="space-y-2">
          <h2 className="text-sm font-semibold">Needs a decision</h2>
          <ul className="space-y-2">
            {list.pending_actions.map((action) => (
              <li key={action.id}>
                <PendingActionCard
                  type={action.type}
                  title={action.title}
                  body={action.body}
                />
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {active.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            No basket investments yet. Browse the catalog, then Invest — that builds a desk plan;
            this page never places an order.
          </p>
          <Link
            href="/explore"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Explore baskets
          </Link>
        </div>
      ) : (
        <ul className="space-y-3" aria-label="Active investments">
          {active.map((row) => (
            <li
              key={row.id}
              className="space-y-3 rounded-xl border border-border/70 bg-card p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <Link
                    href={`/investments/${row.id}`}
                    className="text-sm font-semibold text-foreground underline-offset-4 hover:underline"
                  >
                    {row.basket_name}
                  </Link>
                  {row.rebalance_pending ? (
                    <p className="mt-1 text-xs text-accent">Rebalance update available</p>
                  ) : null}
                  {row.days_since_last_investment != null &&
                  row.days_since_last_investment >= 30 ? (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {row.days_since_last_investment} days since last investment
                    </p>
                  ) : null}
                </div>
                <Link
                  href={`/investments/${row.id}`}
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

      {exited.length > 0 ? (
        <p className="text-sm text-muted-foreground">
          <Link href="/investments?history=1" className="underline-offset-4 hover:underline">
            Exited history
          </Link>{" "}
          ({exited.length})
        </p>
      ) : null}

      <p className="text-xs text-muted-foreground">
        Read-only ledger view. Execution stays in the desk console.
      </p>
    </div>
  );
}
