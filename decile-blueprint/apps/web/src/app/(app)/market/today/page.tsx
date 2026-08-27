import type { Metadata } from "next";

import { IndexDashboard } from "@/components/market/index-dashboard";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { EmptyState } from "@/components/data/empty-state";
import { fetchIndexDashboardOrDegraded } from "@/lib/market/fetch";
import { PAGES } from "@/lib/vocabulary";

export const revalidate = 3600;

export const metadata: Metadata = {
  title: PAGES["/market/today"].title,
  description:
    "Every NSE index with its level, change, P/E, P/B and dividend yield, sortable and searchable.",
};

export default async function MarketTodayPage() {
  const board = await fetchIndexDashboardOrDegraded();

  /*
   * A pipeline that has published nothing is a state, not a crash. This page used to await the
   * throwing fetch, so an unpublished deployment answered a full-page "Application error: a
   * server-side exception has occurred" — from a backend that had politely explained itself with
   * `503 pipeline-degraded`. docs/11 §Reliability asks for the opposite: render, and say so.
   */
  if (board === null) {
    return (
      <>
        <SectionTabs section="market" />
        <PageHeader title={PAGES["/market/today"].title} blurb={PAGES["/market/today"].blurb} />
        <EmptyState
          title="No index data published yet"
          reason="The nightly pipeline has not published a run, so there is no close to show. Index levels appear here once a run passes its checks."
        />
      </>
    );
  }

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
