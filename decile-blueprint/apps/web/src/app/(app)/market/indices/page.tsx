import type { Metadata } from "next";

import { IndicesView } from "@/components/market/indices/indices-view";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { PAGES } from "@/lib/vocabulary";

export const metadata: Metadata = {
  title: PAGES["/market/indices"].title,
  description: PAGES["/market/indices"].blurb,
};

export default function MarketIndicesPage() {
  return (
    <div className="flex flex-col gap-4">
      <SectionTabs section="market" />
      <PageHeader title={PAGES["/market/indices"].title} blurb={PAGES["/market/indices"].blurb} />
      <IndicesView />
    </div>
  );
}
