import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { PageHeader } from "@/components/shell/page-header";
import { EMPTY_CELL, formatNumber, formatPercent } from "@/lib/format";
import { fetchInvestmentCosts } from "@/lib/investments/costs";
import { fetchInvestment } from "@/lib/investments/fetch";

/**
 * `/me/investments/[id]/costs` — T8.5. Accrued platform fees, not charged.
 */

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  const detail = await fetchInvestment(id);
  return {
    title: detail ? `Costs · ${detail.basket_name}` : "Costs",
    robots: { index: false, follow: false },
  };
}

function rupees(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 2 })}`;
}

export default async function InvestmentCostsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const detail = await fetchInvestment(id);
  const costs = await fetchInvestmentCosts(id);
  if (detail === null && costs === null) notFound();
  const name = detail?.basket_name ?? `Investment ${id}`;
  const snap = costs?.snapshot;

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <PageHeader
        title="Costs and returns"
        blurb="Platform fees are accrued on this book. They are not charged from this page."
        meta={
          <Link
            href={`/investments/${id}`}
            className="text-xs text-muted-foreground underline-offset-4 hover:underline"
          >
            Back to {name}
          </Link>
        }
      />

      {costs === null ? (
        <p className="text-sm text-muted-foreground">Costs are not available for this investment yet.</p>
      ) : (
        <section className="space-y-3 rounded-xl border border-border bg-card p-4">
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="eyebrow">Current value</dt>
              <dd className="mt-0.5 text-lg font-semibold tabular-nums">
                {rupees(snap?.current_value)}
              </dd>
            </div>
            <div>
              <dt className="eyebrow">Current returns</dt>
              <dd className="mt-0.5 text-lg font-semibold tabular-nums">
                {snap ? formatPercent(snap.current_returns_pct) : EMPTY_CELL}
              </dd>
            </div>
            <div>
              <dt className="eyebrow">Accrued fees (incl. GST)</dt>
              <dd className="mt-0.5 text-lg font-semibold tabular-nums">
                {rupees(costs.accrued_fees_total)}
              </dd>
            </div>
            <div data-testid="costs-after-fees">
              <dt className="eyebrow">Returns after fees</dt>
              <dd className="mt-0.5 text-lg font-semibold tabular-nums">
                {rupees(costs.returns_after_fees)}
              </dd>
            </div>
          </dl>
          <p className="text-xs leading-relaxed text-muted-foreground">
            Accrued, not collected. Fee collection stays off.
          </p>
        </section>
      )}
    </div>
  );
}
