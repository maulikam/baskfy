import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { ExploreUnavailable, fetchExploreBasket } from "@/lib/explore/fetch";

/**
 * `/basket/[slug]/constituents` — SC5 stub.
 * Rebalance timeline + holdings distribution land when SC3 versions publish history.
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
      title: `${basket.name} · Constituents`,
      robots: { index: false, follow: false },
    };
  } catch {
    return { title: "Constituents", robots: { index: false, follow: false } };
  }
}

export default async function BasketConstituentsPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;

  let basket;
  try {
    basket = await fetchExploreBasket(slug);
  } catch (error) {
    if (error instanceof ExploreUnavailable) notFound();
    throw error;
  }

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader
        title={`${basket.name} · Constituents`}
        blurb="Weights and the rebalance timeline will appear here once version history is published."
        meta={
          <Link
            href={`/basket/${basket.slug}`}
            className="text-accent underline-offset-4 hover:underline"
          >
            ← Overview
          </Link>
        }
      />

      <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
        <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
          Constituent rows need an immutable version from the catalog engine (SC3). Until then
          this route stays a stub so the detail tabs and nav links already resolve.
        </p>
      </div>

      <DisclosureBlock variant="performance-not-verified" />

      <p className="text-xs text-muted-foreground">
        This page is read-only — nothing on it can buy or sell anything.
      </p>
    </div>
  );
}
