import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { RebalanceWizard } from "@/components/portfolios/rebalance-wizard";
import { serverApi } from "@/lib/api/server";

/**
 * `/portfolios/[id]/rebalance` — docs/08 §Routes, and §"Rebalance tracker"'s wizard.
 *
 * The portfolio is fetched on the server so the page opens with the holdings already on it; the
 * screen list, the two rule inputs and the run itself are the client component's.
 */
export const metadata: Metadata = {
  title: "Rebalance",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function RebalancePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const portfolioId = Number.parseInt(id, 10);
  if (!Number.isInteger(portfolioId) || portfolioId < 1) notFound();

  const api = await serverApi();
  const { data } = await api.GET("/api/v1/portfolios/{portfolio_id}", {
    params: { path: { portfolio_id: portfolioId } },
  });

  return <RebalanceWizard portfolioId={portfolioId} initial={data ?? null} />;
}
