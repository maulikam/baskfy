import type { Metadata } from "next";

import { DeskNav, ReadOnlyFooter } from "@/components/desk/ui";
import { PerformanceView } from "@/components/performance/performance-view";
import { DeskUnavailable, type Holdings, fetchHoldings } from "@/lib/desk/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/performance` — the desk's Performance page, ported (Maulik prefers it to the old summary).
 * The record comes from same-origin `/desk/performance/data`; holdings from the API's snapshot.
 * **Read-only.** Nothing on this page can move money.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/performance"].title,
  description: "The desk's performance record: holdings, returns, risk ratios, drawdown, benchmark and closed trades.",
};

export default async function PerformancePage() {
  let holdings: Holdings | null = null;
  let reason: string | null = null;
  try {
    holdings = await fetchHoldings();
  } catch (error) {
    if (!(error instanceof DeskUnavailable)) throw error;
    reason = error.status === 404 ? "no end-of-day snapshot yet" : "the desk did not answer";
  }

  return (
    <div className="flex flex-col gap-5">
      <PerformanceView title={PAGES["/performance"].title} holdings={holdings} holdingsReason={reason} />
      <DeskNav current="/performance" />
      <ReadOnlyFooter />
    </div>
  );
}
