import type { Metadata } from "next";
import Link from "next/link";

import { CompareTable } from "@/components/discover/compare-table";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { MAX_COMPARE, MIN_COMPARE, selectionFromParams } from "@/lib/discover/selection";
import { ExploreUnavailable, fetchExploreBasket } from "@/lib/explore/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/discover/compare?b=one&b=two` — two or three baskets, measured the same way.
 *
 * The selection lives in the URL rather than only in `localStorage`, so a comparison is a link:
 * it can be shared, bookmarked, opened in a second tab against a different pair, and pasted into
 * a bug report. The sticky bar builds the link; this page only reads it.
 *
 * A basket that cannot be fetched is named and skipped rather than failing the page. Comparing
 * the two that did load, and saying which one did not, beats an error where a table should be.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/discover/compare"].title,
  description: PAGES["/discover/compare"].blurb,
  robots: { index: false, follow: false },
};

export default async function ComparePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const slugs = selectionFromParams(params);

  const settled = await Promise.all(
    slugs.map(async (slug) => {
      try {
        return { slug, basket: await fetchExploreBasket(slug) };
      } catch (error) {
        if (error instanceof ExploreUnavailable) return { slug, basket: null };
        throw error;
      }
    }),
  );
  const baskets = settled.flatMap((entry) => (entry.basket ? [entry.basket] : []));
  const missing = settled.filter((entry) => entry.basket === null).map((entry) => entry.slug);

  return (
    <div className="flex w-full max-w-[104rem] flex-col gap-6">
      <SectionTabs section="discover" />
      <PageHeader
        title={PAGES["/discover/compare"].title}
        blurb={PAGES["/discover/compare"].blurb}
      />

      {missing.length > 0 ? (
        <p
          className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
          data-testid="compare-missing"
        >
          Could not load {missing.join(", ")}. The rest are compared below.
        </p>
      ) : null}

      {baskets.length < MIN_COMPARE ? (
        <div
          className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center"
          data-testid="compare-empty"
        >
          <p className="max-w-[56ch] text-sm leading-relaxed text-muted-foreground">
            Pick {MIN_COMPARE} or {MAX_COMPARE} baskets to compare. Use the Compare button on any
            basket card — the bar at the foot of the page keeps track, and brings you here.
          </p>
          <Link
            href="/discover/all"
            className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
          >
            Browse all baskets
          </Link>
        </div>
      ) : (
        <CompareTable baskets={baskets} />
      )}

      <DisclosureBlock variant="performance-not-verified" />
      <p className="text-xs text-muted-foreground">
        Comparing is read-only — nothing here places an order.
      </p>
    </div>
  );
}
