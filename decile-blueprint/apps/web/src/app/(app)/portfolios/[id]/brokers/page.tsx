import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { BrokerReadPanel } from "@/components/portfolios/broker-read-panel";
import { BrokerRollup } from "@/components/portfolios/broker-rollup";
import { serverApi } from "@/lib/api/server";
import { consolidate, type BrokerListOut } from "@/lib/portfolios/rollup";

/**
 * `/portfolios/[id]/brokers` — whose money is where.
 *
 * The consolidated view migration 0019 made possible: every holding in this portfolio's subtree,
 * summed per broker account, with the total's coverage stated rather than implied. Fetched on the
 * server so the page opens with the split already on it; only the broker read is client-side,
 * because it is an action.
 *
 * Read-only in the strongest sense available: the endpoint behind it reaches no broker session,
 * marks nothing to market, and has no order path. Nothing on this route can place anything.
 *
 * The SEBI disclaimer is not written on this page and must not be. The app shell mounts the
 * component once per app page (CLAUDE.md house rule 9, "disclaimers are components, not
 * footers"), so this surface already carries it; a second copy here would be the double-render
 * tree 6 removed from the backtest detail page.
 */
export const metadata: Metadata = {
  title: "Whose money is where",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function PortfolioBrokersPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const portfolioId = Number.parseInt(id, 10);
  if (!Number.isInteger(portfolioId) || portfolioId < 1) notFound();

  const api = await serverApi();
  const [rollupResponse, portfolioResponse, brokersResponse] = await Promise.all([
    api.GET("/api/v1/portfolios/{portfolio_id}/holdings", {
      params: { path: { portfolio_id: portfolioId } },
    }),
    api.GET("/api/v1/portfolios/{portfolio_id}", {
      params: { path: { portfolio_id: portfolioId } },
    }),
    api.GET("/api/v1/brokers"),
  ]);

  const rollup = rollupResponse.data;
  if (!rollup) notFound();

  // A catalog that could not be read is reported as unknown coverage, never as full coverage:
  // `consolidate` says so in as many words rather than letting the total imply completeness.
  const catalog: BrokerListOut | null = brokersResponse.data ?? null;
  const view = consolidate({ rollup, catalog });
  const name = portfolioResponse.data?.name;

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">{name ?? "Whose money is where"}</h1>
        <p className="text-sm text-muted-foreground">
          Holdings filed under this portfolio and everything nested beneath it, split by the broker
          account each row names. Nothing on this page places an order.
        </p>
      </header>

      <BrokerRollup view={view} {...(name === undefined ? {} : { portfolioName: name })} />
      <BrokerReadPanel brokers={[...view.coverage.canSync, ...view.coverage.cannotSync]} />
    </div>
  );
}
