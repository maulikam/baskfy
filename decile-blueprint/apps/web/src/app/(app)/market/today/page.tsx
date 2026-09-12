import type { Metadata } from "next";

import { HeadlineStrip } from "@/components/market/headline-strip";
import { IndexDashboard } from "@/components/market/index-dashboard";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { EmptyState } from "@/components/data/empty-state";
import { fetchIndexDashboardOrDegraded } from "@/lib/market/fetch";
import { PAGES } from "@/lib/vocabulary";

/* Page-level `revalidate` is inert under `(app)/layout` (reads cookies) — AUDIT 4.7. */

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
    <div className="flex flex-col gap-4">
      <SectionTabs section="market" />
      {/* AFH 5.6: tight header — title only; the strip carries the numbers people come for. */}
      <PageHeader title={PAGES["/market/today"].title} />
      <HeadlineStrip rows={board.data} />
      <p className="text-xs text-muted-foreground">
        {board.data.length} indices · close of {formatTradeDate(board.as_of)}
      </p>
      <IndexDashboard rows={board.data} asOf={formatTradeDate(board.as_of)} />
    </div>
  );
}
