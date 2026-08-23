import type { Metadata } from "next";

import { IndexDashboard } from "@/components/market/index-dashboard";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { fetchIndexDashboard } from "@/lib/market/fetch";
import { PAGES } from "@/lib/vocabulary";

export const revalidate = 3600;

export const metadata: Metadata = {
  title: PAGES["/market/today"].title,
  description:
    "Every NSE index with its level, change, P/E, P/B and dividend yield, sortable and searchable.",
};

export default async function MarketTodayPage() {
  const board = await fetchIndexDashboard();

  return (
    <>
      <SectionTabs section="market" />
      <PageHeader
        title={PAGES["/market/today"].title}
        blurb={PAGES["/market/today"].blurb}
        meta={`${board.data.length} indices, priced at the close on ${formatTradeDate(board.as_of)}. Sorted by today's move, biggest first — click any column heading to re-sort.`}
      />
      <IndexDashboard rows={board.data} asOf={formatTradeDate(board.as_of)} />
    </>
  );
}
