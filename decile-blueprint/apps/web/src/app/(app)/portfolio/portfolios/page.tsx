import type { Metadata } from "next";
import type { SleeveListOut } from "@baskfy/api-client";

import {
  createPortfolioAction,
  deletePortfolioAction,
  loadSleevesAction,
  reattributePortfolioAction,
  renamePortfolioAction,
  saveSleevesAction,
  transferHoldingsAction,
} from "@/app/actions/portfolio";
import { CommandCenterScreen } from "@/components/portfolio/command/command-center-screen";
import { PortfoliosList } from "@/components/portfolios/portfolios-list";
import { SectionTabs } from "@/components/shell/section-tabs";
import { serverApi } from "@/lib/api/server";
import { fetchRegime, type Regime } from "@/lib/desk/fetch";
import { readerSafeDeskError } from "@/lib/portfolio/desk-error";
import { fetchInvestments } from "@/lib/investments/fetch";
import { fetchPortfolioHoldings, readPortfolioOverview } from "@/lib/portfolio/fetch";
import { isMarketOpen } from "@/lib/market/session";
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
  const [{ data, error }, investments, overviewRead, holdings, regimeRead] = await Promise.all([
    api.GET("/api/v1/portfolios"),
    fetchInvestments(),
    readPortfolioOverview(),
    fetchPortfolioHoldings(),
    readRegime(),
  ]);
  const overview = overviewRead.overview;
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
      {/* AFH 5.1: same tab bar as Overview — command centre is the Details drill-down. */}
      <SectionTabs section="portfolio" />
      {/* PC1 — the command centre replaces the repeating per-portfolio blocks this page used to
          draw. `docs/PORTFOLIO-COMMAND-CENTER.md` carries the plan and, more usefully, the survey
          of which metrics Baskfy can and cannot compute; nothing here invents one. */}
      <CommandCenterScreen
        overview={overview}
        unallocated={overview?.unallocated ?? null}
        error={overviewRead.failure === "unreachable" ? "The portfolio service did not answer." : null}
        failure={overviewRead.failure}
        marketOpen={isMarketOpen()}
        regime={regimeRead.regime}
        regimeError={regimeRead.error}
        /* EXCHANGE time, not the server's. The panel is pure and takes no clock, so the caller
           supplies the day — and a box in another zone must not make yesterday's evaluation look
           like today's. */
        regimeToday={exchangeToday()}
        holdings={holdings?.rows ?? []}
        manageHandlers={{
          create: createPortfolioAction,
          rename: renamePortfolioAction,
          transfer: transferHoldingsAction,
          loadSleeves: loadSleevesAction,
          saveSleeves: saveSleevesAction,
          reattribute: reattributePortfolioAction,
          remove: deletePortfolioAction,
        }}
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

/**
 * The desk's stance, or the sentence saying why there is none.
 *
 * `null` is never "risk-on by default" — an unreachable desk shows no tier at all, because a tier
 * from a market the screen cannot currently see is worse than no tier. The panel holds that rule;
 * this only has to avoid throwing the page away when the desk is down.
 */
async function readRegime(): Promise<{ regime: Regime | null; error: string | null }> {
  try {
    return { regime: await fetchRegime(), error: null };
  } catch (error) {
    /* THE TRANSPORT'S OWN MESSAGE NEVER REACHES THE SCREEN, and it used to.
       `DeskUnavailable.message` is the fetch's text — "http://127.0.0.1:8100/api/v1/desk/regime
       responded 404" — so a reader met an internal host and port on a portfolio page. It was
       found by looking at a screenshot, not by a test, which is why there is now a test
       (`no-internals.test.tsx`). The detail is still worth having, so it is logged rather than
       rendered: the person who can act on a 404 is reading the logs, not the page. */
    console.error("[portfolios] the desk's regime could not be read", error);
    return { regime: null, error: readerSafeDeskError(error) };
  }
}

/** Today in IST as `YYYY-MM-DD`. The exchange's day, which is the only one a stance is about. */
function exchangeToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
