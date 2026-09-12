import type { Metadata, Route } from "next";
import { ReturnConventionNote } from "@/components/explore/return-convention-note";
import Link from "next/link";
import { notFound } from "next/navigation";

import { AccessBadge } from "@/components/explore/access-badge";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { InvestCta } from "@/components/explore/invest-cta";
import { PerformanceChart } from "@/components/explore/performance-chart";
import { ReturnStat } from "@/components/explore/return-stat";
import { VolatilityChip } from "@/components/explore/volatility-chip";
import { PageHeader } from "@/components/shell/page-header";
import { ExploreNotFound, fetchExploreBasket } from "@/lib/explore/fetch";
import { resolveBasketPerformanceSeries } from "@/lib/explore/performance";
import { EMPTY_CELL, formatNumber, formatTradeDate } from "@/lib/format";
import { BasketDescription } from "@/components/basket/basket-description";

/**
 * `/basket/[slug]` — SC5 detail. Overview + disclosures + Invest hand-off.
 */

export const dynamic = "force-dynamic";

function rupees(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 0 })}`;
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  try {
    const basket = await fetchExploreBasket(slug);
    return {
      title: basket.name,
      description: basket.description_md?.slice(0, 160) ?? `Basket ${basket.name}`,
      robots: { index: false, follow: false },
    };
  } catch {
    return { title: "Basket", robots: { index: false, follow: false } };
  }
}

export default async function BasketDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;

  let basket;
  try {
    basket = await fetchExploreBasket(slug);
  } catch (error) {
    if (error instanceof ExploreNotFound) notFound();
    throw error;
  }

  const metrics = basket.metrics;
  const young =
    metrics !== null &&
    metrics.headline_label !== null &&
    (metrics.cagr_3y === null || metrics.cagr_5y === null);

  const performance = await resolveBasketPerformanceSeries(basket.slug, metrics);

  return (
    <div className="flex w-full min-w-0 flex-col gap-6">
      <PageHeader
        title={basket.name}
        blurb={`Managed by ${basket.manager.name}.`}
        meta={
          <span className="flex flex-wrap items-center gap-2">
            <AccessBadge access={basket.access} />
            <span>
              <Link
                href={`/manager/${basket.manager.slug}` as Route}
                className="underline-offset-4 hover:underline"
              >
                {basket.manager.name}
              </Link>{" "}
              ·{" "}
              {basket.rebalance_frequency.toLowerCase().replace(/_/g, " ")} rebalance
              {basket.launched_at ? ` · since ${formatTradeDate(basket.launched_at)}` : ""}
            </span>
          </span>
        }
        actions={
          <InvestCta
            basketName={basket.name}
            basketSlug={slug}
            minAmount={metrics?.min_amount ?? null}
          />
        }
      />

      {basket.description_md ? (
        <BasketDescription markdown={basket.description_md} />
      ) : null}

      {/* One strip only — headline return, min amount, volatility. No second 1Y / MED pair. */}
      <section
        aria-label="Returns and costs"
        className="grid grid-cols-2 gap-3 sm:grid-cols-3"
      >
        <ReturnStat label={metrics?.headline_label} value={metrics?.headline_pct} />
        <Stat label="Min. amount" value={rupees(metrics?.min_amount)} />
        <div>
          <div className="eyebrow">Volatility</div>
          <div className="mt-1.5">
            <VolatilityChip bucket={metrics?.volatility_bucket} />
          </div>
        </div>
      </section>

      <nav aria-label="Basket sections" className="flex flex-wrap gap-2 text-sm">
        <span className="rounded-md border border-accent bg-accent-muted px-2.5 py-1 text-xs font-medium text-accent">
          Overview
        </span>
        <Link
          href={`/basket/${basket.slug}/constituents`}
          className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          Constituents
        </Link>
      </nav>

      <div className="space-y-3">
        <PerformanceChart
          series={performance.points}
          basketLabel={basket.name}
          benchmarkLabel="NIFTY 50"
          defaultRange="1Y"
          incompleteHistory={
            young ||
            performance.source === "empty" ||
            (performance.coverage !== null && performance.coverage < 0.95)
          }
        />
        <ReturnConventionNote metrics={basket.metrics ?? null} />
        <DisclosureBlock variant="performance-not-verified" />
        {young ? <DisclosureBlock variant="history-caveat" /> : null}
      </div>

      <section className="space-y-2 rounded-xl border border-border/70 bg-card p-4">
        <h2 className="text-sm font-semibold">About the manager</h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          <Link
            href={`/manager/${basket.manager.slug}` as Route}
            className="font-medium text-foreground underline-offset-4 hover:underline"
          >
            {basket.manager.name}
          </Link>
          .
        </p>
        {basket.manager.kind === "HUMAN" ? (
          <DisclosureBlock variant="registration-pending" />
        ) : null}
      </section>

      <p className="text-xs text-muted-foreground">
        This page is read-only — nothing on it can buy or sell anything. Invest opens a plan
        hand-off; orders are placed only from the desk console.
      </p>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="eyebrow">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}
