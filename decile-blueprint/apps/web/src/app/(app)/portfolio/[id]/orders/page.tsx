import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { PageHeader } from "@/components/shell/page-header";
import { EMPTY_CELL, formatDateTimeIST } from "@/lib/format";
import { fetchInvestment } from "@/lib/investments/fetch";

/**
 * `/portfolio/[id]/orders` — SC6. Read-only batch list; never an execute surface.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Orders",
  robots: { index: false, follow: false },
};

export default async function InvestmentOrdersPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const detail = await fetchInvestment(id);
  if (detail === null) notFound();

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader
        title="Orders"
        blurb={`Read-only batches for ${detail.basket_name}. Desk journal status when joined; nothing executes from here.`}
        meta={
          <Link
            href={`/portfolio/${detail.id}`}
            className="text-xs underline-offset-4 hover:underline"
          >
            ← Back to investment
          </Link>
        }
      />

      {detail.orders.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-12 text-center">
          <p className="max-w-[48ch] text-sm text-muted-foreground">
            No order batches yet. When a plan is built and the desk journal records it, rows will
            appear here as read-only history.
          </p>
        </div>
      ) : (
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-border text-xs text-muted-foreground">
              <th className="py-2 font-medium">Kind</th>
              <th className="py-2 font-medium">Status</th>
              <th className="py-2 font-medium">Plan</th>
              <th className="py-2 font-medium">When</th>
            </tr>
          </thead>
          <tbody>
            {detail.orders.map((row) => (
              <tr key={row.id} className="border-b border-border/60">
                <td className="py-2">{row.kind}</td>
                <td className="py-2">{row.status}</td>
                <td className="py-2 font-mono text-xs">
                  {row.desk_plan_id ?? EMPTY_CELL}
                </td>
                <td className="py-2 tabular-nums">
                  {row.created_at ? formatDateTimeIST(row.created_at) : EMPTY_CELL}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
