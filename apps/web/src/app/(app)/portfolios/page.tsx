import type { Metadata } from "next";

import { PortfoliosList } from "@/components/portfolios/portfolios-list";
import { serverApi } from "@/lib/api/server";

/**
 * `/portfolios` — docs/08 §Routes marks it client, and docs/01 §8 is the feature: the rebalance
 * tracker. The list itself is fetched on the server so the first paint is not a spinner; every
 * action after that is the client component's.
 */
export const metadata: Metadata = {
  title: "Rebalance Tracker",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function PortfoliosPage() {
  const api = await serverApi();
  const { data, error } = await api.GET("/api/v1/portfolios");

  return (
    <PortfoliosList initial={data?.data ?? null} error={data ? null : (error ?? "unreachable")} />
  );
}
