import { Download } from "lucide-react";
import type { InvoiceOut } from "@baskfy/api-client";

import { apiOrigin } from "@/lib/api/config";
import { formatTradeDate } from "@/lib/format";

/**
 * The invoice list — docs/07 §"Account & billing".
 *
 * A plain table rather than the virtualised `DataTable`: an account has tens of invoices, not
 * thousands, and the download is a link rather than a row action.
 *
 * Amounts are printed from the API's exact strings without going through `Number`, because
 * CLAUDE.md house rule 9 keeps money exact and a paisa lost to a float on an invoice is a
 * reconciliation someone has to do by hand.
 */
export function InvoiceTable({ invoices }: { invoices: readonly InvoiceOut[] }) {
  return (
    <table className="w-full border-collapse text-sm">
      <caption className="sr-only">Your invoices, newest first</caption>
      <thead>
        <tr className="border-b border-border text-left text-xs uppercase text-muted-foreground">
          <th scope="col" className="py-2 pr-3 font-medium">
            Invoice
          </th>
          <th scope="col" className="py-2 pr-3 font-medium">
            Date
          </th>
          <th scope="col" className="py-2 pr-3 font-medium">
            Plan
          </th>
          <th scope="col" className="py-2 pr-3 text-right font-medium">
            Taxable
          </th>
          <th scope="col" className="py-2 pr-3 text-right font-medium">
            GST
          </th>
          <th scope="col" className="py-2 pr-3 text-right font-medium">
            Total
          </th>
          <th scope="col" className="py-2 font-medium">
            <span className="sr-only">Download</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {invoices.map((invoice) => (
          <tr key={invoice.invoice_number} className="border-b border-border/60">
            <td className="py-2 pr-3 font-mono text-xs">{invoice.invoice_number}</td>
            <td className="py-2 pr-3 tabular-nums">{formatTradeDate(invoice.invoice_date)}</td>
            <td className="py-2 pr-3">{invoice.plan_code ?? "—"}</td>
            <td className="py-2 pr-3 text-right tabular-nums">{invoice.taxable_inr ?? "—"}</td>
            <td className="py-2 pr-3 text-right tabular-nums">{invoice.gst_inr ?? "—"}</td>
            <td className="py-2 pr-3 text-right font-medium tabular-nums">{invoice.amount_inr}</td>
            <td className="py-2">
              <a
                href={`${apiOrigin()}${invoice.pdf_url}`}
                className="inline-flex items-center gap-1 text-accent underline-offset-4 hover:underline"
              >
                <Download aria-hidden="true" className="size-3.5" />
                PDF
                <span className="sr-only"> for invoice {invoice.invoice_number}</span>
              </a>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
