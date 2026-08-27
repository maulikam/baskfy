import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { SleevePlanner } from "@/components/portfolios/sleeve-planner";
import { serverApi } from "@/lib/api/server";

/**
 * `/portfolios/[id]/sleeves` — M34.
 *
 * The Rebalance Tracker answers "which symbols changed"; this answers "how much goes where". The
 * sleeves and the screen list are fetched on the server so the page opens with the division
 * already on it; editing, saving and the allocation are the client component's.
 *
 * **Amounts and weights only.** No share counts, and no control that could place anything.
 */
export const metadata: Metadata = {
  title: "Allocations",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function SleevesPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const portfolioId = Number.parseInt(id, 10);
  if (!Number.isInteger(portfolioId) || portfolioId < 1) notFound();

  const api = await serverApi();
  const [sleeves, screens] = await Promise.all([
    api.GET("/api/v1/portfolios/{portfolio_id}/sleeves", {
      params: { path: { portfolio_id: portfolioId } },
    }),
    api.GET("/api/v1/screens"),
  ]);

  return (
    <SleevePlanner
      portfolioId={portfolioId}
      screens={screens.data?.data ?? []}
      {...(sleeves.data ? { initial: sleeves.data } : {})}
    />
  );
}
