import type { Metadata } from "next";
import Link from "next/link";

import { BasketCard } from "@/components/explore/basket-card";
import { BasketCard as SharedBasketCard } from "@/components/basket/basket-card";
import { ReturnConventionNote } from "@/components/explore/return-convention-note";
import { ExploreFilters, type ExploreFilterState } from "@/components/explore/explore-filters";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import {
  ExploreUnavailable,
  definedParams,
  fetchExploreList,
} from "@/lib/explore/fetch";
import { serverApi } from "@/lib/api/server";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/baskets` — Tree 6 catalog (was `/explore`). Featured tab is `/baskets/featured`.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/baskets"].title,
  description: PAGES["/baskets"].blurb,
  robots: { index: false, follow: false },
};

function first(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export default async function BasketsCatalogPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const state: ExploreFilterState = definedParams({
    max_min_amount: first(params.max_min_amount),
    access: first(params.access),
    volatility: first(params.volatility),
    sort: first(params.sort),
    order: first(params.order),
    q: first(params.q),
  });

  const api = await serverApi();
  const screensRes = await api.GET("/api/v1/screens");
  const autoScreens = (screensRes.data?.data ?? []).filter((s) => !s.is_example).slice(0, 12);

  let catalog;
  try {
    catalog = await fetchExploreList(definedParams(state));
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
    return (
      <div className="flex max-w-5xl flex-col gap-6">
        <SectionTabs section="baskets" />
        <PageHeader title={PAGES["/baskets"].title} blurb={PAGES["/baskets"].blurb} />
        <p className="max-w-prose rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          The catalog could not be loaded. Reload, and if it keeps happening{" "}
          <Link href="/support" className="text-accent underline-offset-4 hover:underline">
            tell us
          </Link>
          .
        </p>
      </div>
    );
  }

  return (
    <div className="flex max-w-5xl flex-col gap-6">
      <SectionTabs section="baskets" />
      <PageHeader
        title={PAGES["/baskets"].title}
        blurb={PAGES["/baskets"].blurb}
        meta={
          <span className="text-xs text-muted-foreground">
            {catalog.total} basket{catalog.total === 1 ? "" : "s"}
            {state.max_min_amount || state.access || state.volatility
              ? " matching these filters"
              : ""}
          </span>
        }
      />

      <ExploreFilters state={state} />

      {catalog.items.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            Nothing matches these filters. Clear them to see the full catalog, or check back after
            the nightly metrics job has run.
          </p>
          <Link
            href="/baskets"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Clear filters
          </Link>
        </div>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2" aria-label="Basket catalog">
          {catalog.items.map((basket) => (
            <li key={basket.slug}>
              <BasketCard basket={basket} />
            </li>
          ))}
        </ul>
      )}

      {autoScreens.length > 0 ? (
        <section aria-labelledby="auto-baskets-heading" className="space-y-3">
          <h2 id="auto-baskets-heading" className="text-sm font-semibold tracking-tight">
            Auto — from your screens
          </h2>
          <ul className="grid gap-3 sm:grid-cols-2">
            {autoScreens.map((screen) => (
              <li key={screen.public_id}>
                <SharedBasketCard
                  basket={{
                    name: screen.name,
                    thesis: "Saved screen, shown as a basket. Open to run and see weights.",
                    href: `/build/${screen.public_id}`,
                    badge: "Auto — from your screens",
                    minAmount: null,
                  }}
                />
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <ReturnConventionNote metrics={catalog.items[0]?.metrics ?? null} />
      <DisclosureBlock variant="performance-not-verified" />
      <p className="text-xs text-muted-foreground">
        Catalog browsing is read-only — investing builds an order plan elsewhere; nothing here
        places an order.
      </p>
    </div>
  );
}
