import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { ExploreUnavailable, fetchExploreManager } from "@/lib/explore/fetch";

/**
 * `/manager/[slug]` — Tree 7 / smallcase §6.7.
 *
 * SEBI furniture: registration number and disclosures belong on a manager surface, not buried
 * in a basket blurb. Data already lives on `cb_manager` and `GET /explore/managers/{slug}`.
 * No order path.
 */

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  try {
    const manager = await fetchExploreManager(slug);
    return {
      title: manager.name,
      description: manager.bio?.slice(0, 160) ?? `Manager ${manager.name}`,
      robots: { index: false, follow: false },
    };
  } catch {
    return { title: "Manager", robots: { index: false, follow: false } };
  }
}

export default async function ManagerPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;

  let manager;
  try {
    manager = await fetchExploreManager(slug);
  } catch (error) {
    if (error instanceof ExploreUnavailable) notFound();
    throw error;
  }

  const kindLabel = manager.kind.toLowerCase().replace(/_/g, " ");

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader
        title={manager.name}
        blurb={manager.bio ?? `${kindLabel} manager on Baskfy.`}
        meta={
          <span className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span className="rounded-md border border-border/70 bg-card px-2 py-0.5 text-xs capitalize">
              {kindLabel}
            </span>
            {manager.sebi_reg_no ? (
              <span>SEBI reg. {manager.sebi_reg_no}</span>
            ) : (
              <span>SEBI registration not on file</span>
            )}
          </span>
        }
      />

      {!manager.sebi_reg_no ? <DisclosureBlock variant="registration-pending" /> : null}

      <DisclosureBlock variant="performance-not-verified" />

      {manager.strategies.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold">Strategies</h2>
          <ul className="flex flex-wrap gap-2">
            {manager.strategies.map((strategy) => (
              <li
                key={strategy}
                className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs"
              >
                {strategy}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {manager.disclosures_md ? (
        <section className="space-y-2 rounded-xl border border-border/70 bg-card p-4">
          <h2 className="text-sm font-semibold">Disclosures</h2>
          <div className="prose prose-sm max-w-none whitespace-pre-wrap text-muted-foreground">
            {manager.disclosures_md}
          </div>
        </section>
      ) : (
        <p className="text-sm text-muted-foreground">
          No written disclosures are on file for this manager yet.
        </p>
      )}

      <p className="text-xs text-muted-foreground">
        Registration does not guarantee performance. This page is read-only — nothing here buys
        or sells anything.{" "}
        <Link href="/baskets" className="text-accent underline-offset-4 hover:underline">
          Back to baskets
        </Link>
      </p>
    </div>
  );
}
