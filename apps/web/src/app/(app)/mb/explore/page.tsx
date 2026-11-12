import type { Metadata } from "next";

import { MbExplorerPanel } from "@/components/mb/explorer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchMbExplorer } from "@/lib/mb/fetch";
import { PAGES } from "@/lib/vocabulary";
import { fetchInstrumentWatchlist } from "@/lib/watchlist/instruments";

/** `/mb/explore` — both models side by side for every scored stock, filtered and ranked in the browser. Read-only. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/mb/explore"].title,
  description: PAGES["/mb/explore"].blurb,
};

export default async function MbExplorePage() {
  const [data, watch] = await Promise.all([fetchMbExplorer(), fetchInstrumentWatchlist()]);
  return (
    <div className="space-y-8">
      <PageHeader title={PAGES["/mb/explore"].title} blurb={PAGES["/mb/explore"].blurb} />
      <SectionTabs section="mb" />
      <MbExplorerPanel data={data} watched={watch.items.map((i) => i.symbol)} />
    </div>
  );
}
