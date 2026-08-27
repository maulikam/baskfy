import type { Metadata } from "next";
import Link from "next/link";

import { BasketDetail } from "@/components/basket/basket-detail";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { BasketUnavailable, fetchBasket } from "@/lib/basket/fetch";
import { EMPTY_FACTS } from "@/lib/basket/holding-facts";
import type { MaterializedBasket } from "@/lib/basket/materialize";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/discover/featured` — the house strategy's own basket, read-only.
 *
 * **It was called "Featured" and it never was.** "Featured" implies an editorial pick from among
 * many, and a reader is entitled to ask why *this* one — a question the page could not answer,
 * because there is no selection. What it actually renders is the engine's momentum strategy as it
 * would be constructed today: one strategy's live output, not a curated choice.
 * `docs/DISCOVER-AUDIT.md` C13. The route keeps its path so links survive; the title says what
 * the page is.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/discover/featured"].title,
  description:
    "The momentum basket as the strategy would construct it today: names, weights, and amounts.",
};

export default async function FeaturedBasketPage() {
  let basket;
  try {
    basket = await fetchBasket();
  } catch (error) {
    if (!(error instanceof BasketUnavailable)) throw error;
    return (
      <>
        <SectionTabs section="discover" />
        <PageHeader
          title={PAGES["/discover/featured"].title}
          blurb={PAGES["/discover/featured"].blurb}
        />
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            There is no featured basket to show yet. Building one needs daily prices in the
            pipeline and one uploaded scan.
          </p>
          <Link
            href="/discover"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Browse the catalog
          </Link>
        </div>
      </>
    );
  }

  const holdings = basket.rows.map((row) => ({
    rank: row.rank,
    symbol: row.symbol,
    name: row.symbol,
    weight: row.weight / 100,
    price: row.ref_price,
    amount: row.value,
    facts: EMPTY_FACTS,
  }));
  const deployed = holdings.reduce((sum, row) => sum + row.amount, 0);

  const view: MaterializedBasket = {
    name: "House strategy basket",
    thesis: PAGES["/discover/featured"].blurb,
    asOf: basket.as_of,
    cashPct: basket.cash_target_pct,
    notional: basket.capital,
    minInvestment: Math.ceil(basket.capital * 0.05),
    source: "preview",
    holdings,
    deployed,
    cash: basket.capital - deployed,
    // The desk's own plan, not a profile's suggestion: its weights are score-proportional and
    // its cash comes from breadth bands, so naming a holding profile here would be a fiction.
    profile: null,
    underfunded: false,
    method: "SCORE",
  };

  return (
    <>
      <SectionTabs section="discover" />
      <PageHeader
        title={PAGES["/discover/featured"].title}
        blurb={PAGES["/discover/featured"].blurb}
        meta={<>As of {basket.as_of}</>}
      />

      {basket.suspect_symbols.length > 0 ? (
        <details className="mb-4 rounded-xl border border-warning/35 bg-warning-muted p-3.5 text-sm leading-relaxed">
          <summary className="cursor-pointer font-medium">
            Data quality note — {basket.suspect_symbols.length} stocks
          </summary>
          <p className="mt-2">
            Some names have a split or bonus history we do not fully trust, so past prices may look
            wrong.
          </p>
        </details>
      ) : null}

      <BasketDetail basket={view} />

      <p className="mt-4 text-xs text-muted-foreground">
        This page is read-only — nothing on it can buy or sell anything. It is one strategy's own
        output rather than a selection: nothing here has been picked out for you.
      </p>
      <DisclosureBlock variant="performance-not-verified" />
    </>
  );
}
