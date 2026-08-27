import type { Metadata } from "next";
import type { SleeveListOut } from "@baskfy/api-client";

import { PortfoliosList } from "@/components/portfolios/portfolios-list";
import { serverApi } from "@/lib/api/server";
import { fetchInvestments } from "@/lib/investments/fetch";
import type { BookInvestment } from "@/lib/portfolios/book";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/portfolios` — PORTFOLIO_REDESIGN.md §2, the section's grouping layer: every
 * basket you hold, screen you run and set of holdings you have grouped, each measurable on its
 * own. It absorbed the tab that used to be called `Investments` (§1 problem 1); the body below
 * is unchanged, and its §8 rename pass belongs to the components it draws.
 */
export const metadata: Metadata = {
  title: PAGES["/portfolio/portfolios"].title,
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function PortfolioPortfoliosPage() {
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
