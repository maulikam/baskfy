import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { InvoiceTable } from "@/components/billing/invoice-table";
import { fetchInvoices } from "@/lib/billing/fetch";

/**
 * `/invoices` — docs/07: `GET /invoices` and `GET /invoices/{id}/pdf` (Prompt 13 §4).
 *
 * docs/11 §Compliance requires GST-compliant invoices; this is where a customer gets them. Every
 * figure is the API's — the taxable value and each tax head are stored on the payment row when the
 * invoice is raised, not recomputed here, so what this page shows is what the PDF says.
 *
 * Never cached: an invoice list is per-account.
 */
export const metadata: Metadata = {
  title: "Invoices",
  robots: { index: false, follow: false },
};

export default async function InvoicesPage() {
  const page = await fetchInvoices();
  if (page === null) redirect("/login?next=/invoices");

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Invoices</h1>
        <p className="text-sm text-muted-foreground">
          A GST invoice is raised for every payment. Amounts include GST at the rate that applied
          on the day.
        </p>
      </header>

      {page.data.length === 0 ? (
        <p className="rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          No invoices yet. They appear here as soon as a payment is captured —{" "}
          <Link href="/pricing" className="text-accent underline-offset-4 hover:underline">
            see the plans
          </Link>
          .
        </p>
      ) : (
        <InvoiceTable invoices={page.data} />
      )}
    </div>
  );
}
