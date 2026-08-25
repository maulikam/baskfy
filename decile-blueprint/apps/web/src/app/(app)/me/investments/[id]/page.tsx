import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { DriftRepair } from "@/components/investments/drift-repair";
import { InvestmentActions } from "@/components/investments/investment-actions";
import { ShowDetailsModal } from "@/components/investments/show-details-modal";
import { SipForm } from "@/components/investments/sip-form";
import { PageHeader } from "@/components/shell/page-header";
import { EMPTY_CELL, formatNumber, formatPercent } from "@/lib/format";
import { fetchInvestment } from "@/lib/investments/fetch";

/**
 * `/investments/[id]` — SC6 detail. Performance + Manage list; CTAs hand off.
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
    title: detail?.basket_name ?? "Investment",
    robots: { index: false, follow: false },
  };
}

function rupees(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 2 })}`;
}

export default async function InvestmentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const detail = await fetchInvestment(id);
  if (detail === null) notFound();

  const snap = detail.snapshot;

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader
        title={detail.basket_name}
        blurb="Holdings, returns, and manage actions — plans hand off to the desk; nothing executes here."
        meta={
          <span className="text-xs text-muted-foreground">
            Status {detail.status}
            {detail.basket_slug ? (
              <>
                {" · "}
                <Link
                  href={`/basket/${detail.basket_slug}`}
                  className="underline-offset-4 hover:underline"
                >
                  Basket page
                </Link>
              </>
            ) : null}
          </span>
        }
        actions={<ShowDetailsModal basketName={detail.basket_name} snapshot={snap} />}
      />

      {detail.rebalance_pending ? (
        <div className="rounded-xl border border-accent/40 bg-accent-muted/40 px-4 py-3 text-sm">
          <p className="font-medium text-foreground">Rebalance update available</p>
          <p className="mt-1 text-muted-foreground">
            Applying builds an order plan from the weight diff — it does not place orders from this
            page.
          </p>
        </div>
      ) : null}

      <section
        aria-label="Performance"
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
      >
        <Stat label="Current value" value={rupees(snap?.current_value)} />
        <Stat label="Money put in" value={rupees(snap?.money_put_in)} />
        <Stat
          label="Current returns"
          value={
            snap == null
              ? EMPTY_CELL
              : `${formatPercent(snap.current_returns_pct)}`
          }
        />
        <Stat
          label="XIRR"
          value={
            snap?.xirr_displayable && snap.xirr != null
              ? formatPercent(snap.xirr, 2)
              : "—"
          }
        />
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">Constituents</h2>
        {detail.holdings.length === 0 ? (
          <p className="text-sm text-muted-foreground">No holdings rows yet.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-muted-foreground">
                <th className="py-2 font-medium">Symbol</th>
                <th className="py-2 font-medium">Qty</th>
                <th className="py-2 font-medium">Weight</th>
                <th className="py-2 font-medium">Return</th>
              </tr>
            </thead>
            <tbody>
              {detail.holdings.map((row) => (
                <tr key={row.symbol} className="border-b border-border/60">
                  <td className="py-2 font-medium">{row.symbol}</td>
                  <td className="py-2 tabular-nums">{row.qty}</td>
                  <td className="py-2 tabular-nums">
                    {row.weight == null ? EMPTY_CELL : formatPercent(row.weight)}
                  </td>
                  <td className="py-2 tabular-nums">
                    {row.returns_pct == null ? EMPTY_CELL : formatPercent(row.returns_pct)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="space-y-3 rounded-xl border border-border/70 bg-card p-4">
        <h2 className="text-sm font-semibold">Manage</h2>
        <ul className="space-y-2 text-sm text-muted-foreground">
          <li>
            <Link
              href={`/basket/${detail.basket_slug}/constituents`}
              className="text-foreground underline-offset-4 hover:underline"
            >
              Constituents
            </Link>{" "}
            — read-only basket weights
          </li>
          <li>
            <Link
              href={`/investments/${detail.id}/customize`}
              className="text-foreground underline-offset-4 hover:underline"
            >
              Customize
            </Link>{" "}
            — weight-diff plan preview (CUSTOMIZE); no broker path from here
          </li>
          <li>
            <Link
              href={`/investments/${detail.id}/costs`}
              className="text-foreground underline-offset-4 hover:underline"
            >
              Costs and returns
            </Link>{" "}
            — accrued fees, not charged
          </li>
          <li>
            <Link
              href={`/investments/${detail.id}/orders`}
              className="text-foreground underline-offset-4 hover:underline"
            >
              Orders
            </Link>{" "}
            — read-only batch list
          </li>
        </ul>
        <SipForm investmentId={detail.id} />
        <DriftRepair investmentId={detail.id} />
        <InvestmentActions basketName={detail.basket_name} />
      </section>
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
