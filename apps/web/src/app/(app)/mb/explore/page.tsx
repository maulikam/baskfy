import type { Metadata } from "next";

import { MbExplorerPanel } from "@/components/mb/explorer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchMbExplorer } from "@/lib/mb/fetch";
import { PAGES } from "@/lib/vocabulary";

/** `/mb/explore` — both models side by side for every scored stock, filtered and ranked in the browser. Read-only. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/mb/explore"].title,
  description: PAGES["/mb/explore"].blurb,
};

export default async function MbExplorePage() {
  const data = await fetchMbExplorer();
  return (
    <div className="space-y-8">
      <PageHeader title={PAGES["/mb/explore"].title} blurb={PAGES["/mb/explore"].blurb} />
      <SectionTabs section="mb" />
      <MbExplorerPanel data={data} />
    </div>
  );
}
