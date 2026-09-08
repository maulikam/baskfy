import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import {
  ExploreUnavailable,
  fetchExploreBasket,
  fetchExploreConstituents,
} from "@/lib/explore/fetch";

/**
 * `/basket/[slug]/constituents` — the basket's names, as of its newest published version.
 *
 * This was a stub whose own message said constituent rows "need an immutable version from the
 * catalog engine (SC3)". SC3 shipped: `docs/smallcase/STATUS.md` marks it green and the box
 * carries 6 versions and 103 `cb_constituent` rows. What was missing was never the data — it was
 * a route to serve it (`GET /explore/{slug}/constituents`, added 9 Sep 2026) and this render.
 *
 * Read-only. Nothing here can buy or sell.
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

  // The version read is allowed to fail without taking the page with it: the basket exists and
  // its header is worth rendering even if the constituents route is briefly unavailable.
  let version = null;
  try {
    version = await fetchExploreConstituents(slug);
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
  }
  const rows = version?.constituents ?? [];

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader
        title={`${basket.name} · Constituents`}
        blurb={
          version && version.version_no > 0
            ? `Version ${version.version_no} (${version.label}), effective ${version.effective_date} — ${rows.length} names`
            : "Weights appear here once this basket has a published version."
        }
        meta={
          <Link
            href={`/basket/${basket.slug}`}
            className="text-accent underline-offset-4 hover:underline"
          >
            ← Overview
          </Link>
        }
      />

      {rows.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            This basket has no published version yet, so it has no constituents to show. One
            appears here the first time it is rebalanced.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm">
            <caption className="sr-only">
              Constituents of {basket.name}, version {version?.version_no}
            </caption>
            <thead className="bg-card/60 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th scope="col" className="px-4 py-3 font-medium">Symbol</th>
                <th scope="col" className="px-4 py-3 font-medium">Name</th>
                <th scope="col" className="px-4 py-3 font-medium">Segment</th>
                <th scope="col" className="px-4 py-3 text-right font-medium">Weight</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.symbol} className="border-t border-border/60">
                  <td className="px-4 py-3 font-medium">{row.symbol}</td>
                  <td className="px-4 py-3 text-muted-foreground">{row.name ?? "—"}</td>
                  <td className="px-4 py-3 text-muted-foreground">{row.segment}</td>
                  {/* The string the API sent, never parsed to a float — house rule 9. */}
                  <td className="px-4 py-3 text-right tabular-nums">{row.weight}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <DisclosureBlock variant="performance-not-verified" />

      <p className="text-xs text-muted-foreground">
        This page is read-only — nothing on it can buy or sell anything.
      </p>
    </div>
  );
}
