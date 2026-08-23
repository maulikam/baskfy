import type { Metadata } from "next";
import Link from "next/link";

import { BasketDetail } from "@/components/basket/basket-detail";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { BasketUnavailable, fetchBasket } from "@/lib/basket/fetch";
import type { MaterializedBasket } from "@/lib/basket/materialize";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/baskets/featured` — Tree 6. The live strategy basket, read-only.
 * Same BasketDetail renderer as screen results and catalog detail.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/baskets/featured"].title,
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
        <SectionTabs section="baskets" />
        <PageHeader
          title={PAGES["/baskets/featured"].title}
          blurb={PAGES["/baskets/featured"].blurb}
        />
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            There is no featured basket to show yet. Building one needs daily prices in the
            pipeline and one uploaded scan.
          </p>
          <Link
            href="/baskets"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Browse the catalog
          </Link>
        </div>
      </>
    );
  }

  const view: MaterializedBasket = {
    name: "Featured basket",
    thesis: PAGES["/baskets/featured"].blurb,
    asOf: basket.as_of,
    cashPct: basket.cash_target_pct,
    notional: basket.capital,
    minInvestment: Math.ceil(basket.capital * 0.05),
    source: "preview",
    holdings: basket.rows.map((row) => ({
      rank: row.rank,
      symbol: row.symbol,
      name: row.symbol,
      weight: row.weight / 100,
      price: row.ref_price,
      amount: row.value,
    })),
  };

  return (
    <>
      <SectionTabs section="baskets" />
      <PageHeader
        title={PAGES["/baskets/featured"].title}
        blurb={PAGES["/baskets/featured"].blurb}
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
        This page is read-only — nothing on it can buy or sell anything.
      </p>
    </>
  );
}
