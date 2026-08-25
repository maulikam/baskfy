import type { Metadata } from "next";
import type { SleeveListOut } from "@baskfy/api-client";

import { PortfoliosList } from "@/components/portfolios/portfolios-list";
import { serverApi } from "@/lib/api/server";
import { fetchInvestments } from "@/lib/investments/fetch";
import type { BookInvestment } from "@/lib/portfolios/book";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/me/portfolios` — one book of boxes: baskets you hold, rules you wrote, names you run by hand.
 */
export const metadata: Metadata = {
  title: PAGES["/me/portfolios"].title,
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function MePortfoliosPage() {
  const api = await serverApi();
  const [{ data, error }, investments] = await Promise.all([
    api.GET("/api/v1/portfolios"),
    fetchInvestments(),
  ]);
  const list = data?.data ?? [];
  const initialSleeves: Record<number, SleeveListOut> = {};
  await Promise.all(
    list.map(async (portfolio) => {
      const { data: sleeves } = await api.GET("/api/v1/portfolios/{portfolio_id}/sleeves", {
        params: { path: { portfolio_id: portfolio.id } },
      });
      if (sleeves) initialSleeves[portfolio.id] = sleeves;
    }),
  );

  const bookInvestments: BookInvestment[] = investments.items.map((row) => ({
    id: row.id,
    basket_name: row.basket_name,
    status: row.status,
    basket_source: row.basket_source ?? null,
    visibility: row.visibility ?? null,
    snapshot: row.snapshot
      ? {
          money_put_in: row.snapshot.money_put_in,
          current_value: row.snapshot.current_value,
          current_returns_pct: row.snapshot.current_returns_pct,
          xirr: row.snapshot.xirr,
          xirr_displayable: row.snapshot.xirr_displayable,
        }
      : null,
  }));

  return (
    <PortfoliosList
      initial={list}
      error={data ? null : (error ?? "unreachable")}
      investments={bookInvestments}
      initialSleeves={initialSleeves}
    />
  );
}
