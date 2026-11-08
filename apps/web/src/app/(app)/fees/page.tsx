import type { Metadata } from "next";

import { FeeFaq } from "@/components/investments/fee-faq";
import { PageHeader } from "@/components/shell/page-header";
import { EMPTY_CELL, formatDateTimeIST, formatNumber } from "@/lib/format";
import { fetchFeeLedger } from "@/lib/investments/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/fees` — SC6. Accrued (not collected) framing + fee FAQ from docs/smallcase/04 §1.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/fees"].title,
  description: PAGES["/fees"].blurb,
  robots: { index: false, follow: false },
};

function rupees(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 2 })}`;
}

export default async function FeesPage() {
  const ledger = await fetchFeeLedger();

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <PageHeader title={PAGES["/fees"].title} blurb={PAGES["/fees"].blurb} />

      <section
        aria-label="Accrued fees"
        className="rounded-xl border border-border/70 bg-card p-4"
      >
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Accrued · not collected
        </p>
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
          Platform fees are journaled when a batch executes, with{" "}
          <span className="text-foreground">collected=false</span> for this run. Nothing on this
          page charges a card or debit — Track B collection stays off until D3/D7.
        </p>
        <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
          <div>
            <dt className="text-xs text-muted-foreground">Accrued total</dt>
            <dd className="mt-0.5 text-lg font-semibold tabular-nums">
              {rupees(ledger.accrued_total)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Collected</dt>
            <dd className="mt-0.5 text-lg font-semibold tabular-nums">
              {rupees(ledger.collected_total ?? "0")}
            </dd>
          </div>
        </dl>
      </section>

      {ledger.items.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-12 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            No fee ledger rows yet. When invest / invest-more batches execute, each one accrues a
            row here (still uncollected). Example fixtures from 04 §1: ₹6,666 → ₹117.99; ₹7,000 →
            ₹118.00.
          </p>
        </div>
      ) : (
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-border text-xs text-muted-foreground">
              <th className="py-2 font-medium">Kind</th>
              <th className="py-2 font-medium">Basket</th>
              <th className="py-2 font-medium">Base</th>
              <th className="py-2 font-medium">GST</th>
              <th className="py-2 font-medium">Total</th>
              <th className="py-2 font-medium">Collected</th>
              <th className="py-2 font-medium">When</th>
            </tr>
          </thead>
          <tbody>
            {ledger.items.map((row) => (
              <tr key={row.id} className="border-b border-border/60">
                <td className="py-2">{row.kind}</td>
                <td className="py-2">{row.basket_name ?? EMPTY_CELL}</td>
                <td className="py-2 tabular-nums">{rupees(row.base_fee)}</td>
                <td className="py-2 tabular-nums">{rupees(row.gst)}</td>
                <td className="py-2 tabular-nums">{rupees(row.total)}</td>
                <td className="py-2">{row.collected ? "Yes" : "No"}</td>
                <td className="py-2 tabular-nums">{formatDateTimeIST(row.accrued_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <FeeFaq />
    </div>
  );
}
