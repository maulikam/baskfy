import type { Metadata } from "next";

import { PortfoliosList } from "@/components/portfolios/portfolios-list";
import { serverApi } from "@/lib/api/server";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/me/portfolios` — Tree 6 canonical path (was `/portfolios` / "Rebalance Tracker").
 */
export const metadata: Metadata = {
  title: PAGES["/me/portfolios"].title,
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function MePortfoliosPage() {
  const api = await serverApi();
  const { data, error } = await api.GET("/api/v1/portfolios");

  return (
    <PortfoliosList initial={data?.data ?? null} error={data ? null : (error ?? "unreachable")} />
  );
}
