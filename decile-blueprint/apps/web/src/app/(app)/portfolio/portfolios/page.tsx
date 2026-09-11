import type { Metadata } from "next";
import type { SleeveListOut } from "@baskfy/api-client";

import { CommandCenterScreen } from "@/components/portfolio/command/command-center-screen";
import { PortfoliosList } from "@/components/portfolios/portfolios-list";
import { serverApi } from "@/lib/api/server";
import { fetchInvestments } from "@/lib/investments/fetch";
import { fetchPortfolioOverview } from "@/lib/portfolio/fetch";
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
  // The overview is the ALLOCATION LEDGER's answer — what each portfolio is worth, what it did
  // today, how many holdings it has. The two calls below it read the older forest API and the
  // basket book, which is why this page could show "15 holdings filed here" beside an "Overall"
  // panel reading "—": the grouping came from one source and the money from another that knew
  // nothing about grouped holdings. Maulik reported exactly that on 11 Sep 2026.
  const [{ data, error }, investments, overview] = await Promise.all([
    api.GET("/api/v1/portfolios"),
    fetchInvestments(),
    fetchPortfolioOverview(),
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
    <div className="space-y-8">
      {/* PC1 — the command centre replaces the repeating per-portfolio blocks this page used to
          draw. `docs/PORTFOLIO-COMMAND-CENTER.md` carries the plan and, more usefully, the survey
          of which metrics Baskfy can and cannot compute; nothing here invents one. */}
      <CommandCenterScreen
        overview={overview}
        unallocated={overview?.unallocated ?? null}
        error={overview ? null : "The portfolio service did not answer."}
      />

      {/* The grouping forest, sub-portfolios and the basket book are a different job from the
          analytical one above — kept, below, rather than folded in. PC6 moves configuration into
          its own drawer. */}
      <details className="rounded-xl border border-border bg-card">
        <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
          Grouping, sub-portfolios and baskets
        </summary>
        <div className="border-t border-border p-4">
          <PortfoliosList
            initial={list}
            error={data ? null : (error ?? "unreachable")}
            investments={bookInvestments}
            initialSleeves={initialSleeves}
          />
        </div>
      </details>
    </div>
  );
}
