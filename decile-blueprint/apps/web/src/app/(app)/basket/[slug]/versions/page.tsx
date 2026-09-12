import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { VersionDiffPanel } from "@/components/basket/version-diff-panel";
import { PageHeader } from "@/components/shell/page-header";
import { ExploreNotFound, fetchExploreBasket } from "@/lib/explore/fetch";
import { fetchBasketVersions } from "@/lib/explore/versions";

/**
 * `/basket/[slug]/versions` — rebalance history and a diff between any two cuts (AF I.1).
 *
 * The API already stored immutable versions; the consumer page only ever showed the newest.
 * This tab lists every published cut and lets the reader pick two to compare.
 */

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  try {
    const basket = await fetchExploreBasket(slug);
    return {
      title: `${basket.name} · Versions`,
      robots: { index: false, follow: false },
    };
  } catch {
    return { title: "Versions", robots: { index: false, follow: false } };
  }
}

export default async function BasketVersionsPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { slug } = await params;
  const query = await searchParams;

  let basket;
  try {
    basket = await fetchExploreBasket(slug);
  } catch (error) {
    if (error instanceof ExploreNotFound) notFound();
    throw error;
  }

  const versions = await fetchBasketVersions(slug);
  const rawFrom = typeof query.from === "string" ? query.from : undefined;
  const rawTo = typeof query.to === "string" ? query.to : undefined;

  return (
    <div className="flex w-full min-w-0 flex-col gap-6" data-testid="basket-versions">
      <PageHeader
        title={`${basket.name} · Versions`}
        blurb="Every published cut of this basket. Pick any two to see what changed."
      />

      <nav aria-label="Basket sections" className="flex flex-wrap gap-2 text-sm">
        <Link
          href={`/basket/${slug}`}
          className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          Overview
        </Link>
        <Link
          href={`/basket/${slug}/constituents`}
          className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          Constituents
        </Link>
        <span className="rounded-md border border-accent bg-accent-muted px-2.5 py-1 text-xs font-medium text-accent">
          Versions
        </span>
      </nav>

      {versions.count === 0 ? (
        <p className="rounded-xl border border-dashed border-border bg-card/50 px-4 py-10 text-center text-sm text-muted-foreground">
          No published versions yet. A first cut appears here once the catalogue engine writes one.
        </p>
      ) : (
        <>
          <ol className="space-y-2" aria-label="Published versions">
            {versions.versions.map((version) => (
              <li
                key={version.version_no}
                className="flex flex-wrap items-baseline justify-between gap-2 rounded-xl border border-border/70 bg-card px-4 py-3 text-sm"
              >
                <div>
                  <span className="font-semibold">
                    v{version.version_no} · {version.label}
                  </span>
                  <span className="ml-2 text-muted-foreground">
                    effective {version.effective_date} · {version.constituent_count} names
                  </span>
                </div>
                <span className="text-xs text-muted-foreground">
                  +{version.added_count} / −{version.removed_count}
                </span>
              </li>
            ))}
          </ol>

          <VersionDiffPanel
            slug={slug}
            versions={versions.versions}
            initialFrom={rawFrom}
            initialTo={rawTo}
          />
        </>
      )}
    </div>
  );
}
