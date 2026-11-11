import type { Metadata } from "next";

import { MbPaperPanel } from "@/components/mb/paper";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchMbPaper } from "@/lib/mb/fetch";
import { PAGES } from "@/lib/vocabulary";

/** `/mb/paper` — the paper book (PM8). Read-only, paper only; the backtest is labelled as one. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/mb/paper"].title,
  description: PAGES["/mb/paper"].blurb,
};

export default async function MbPaperPage() {
  const paper = await fetchMbPaper();
  return (
    <div className="space-y-8">
      <PageHeader title={PAGES["/mb/paper"].title} blurb={PAGES["/mb/paper"].blurb} />
      <SectionTabs section="mb" />
      <MbPaperPanel paper={paper} />
    </div>
  );
}
