import type { Metadata } from "next";

import { IndexDashboard } from "@/components/market/index-dashboard";
import { PageHeader } from "@/components/shell/page-header";
import { formatTradeDate } from "@/lib/format";
import { fetchIndexDashboard } from "@/lib/market/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/dashboard` — docs/01 §7, docs/08 §Dashboard. docs/08 §Routes: "RSC, revalidate on
 * `data_version`".
 *
 * The whole table is fetched once here, on the server, and handed to the client component as a
 * prop. Sorting and searching never go back to the network: ~145 rows with a 30-point sparkline
 * each is a payload the browser can sort in a frame, and a refetch per header click would be
 * slower *and* would let the page show two different days' data in one session.
 */
export const revalidate = 3600;

export const metadata: Metadata = {
  title: PAGES["/dashboard"].title,
  description:
    "Every NSE index with its level, change, P/E, P/B and dividend yield, sortable and searchable.",
};

export default async function DashboardPage() {
  const board = await fetchIndexDashboard();

  return (
    <>
      <PageHeader
        title={PAGES["/dashboard"].title}
        blurb={PAGES["/dashboard"].blurb}
        meta={`${board.data.length} indices, priced at the close on ${formatTradeDate(board.as_of)}. Sorted by today's move, biggest first — click any column heading to re-sort.`}
      />

      <IndexDashboard rows={board.data} asOf={formatTradeDate(board.as_of)} />
    </>
  );
}
