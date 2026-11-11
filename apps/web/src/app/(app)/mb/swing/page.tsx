import type { Metadata } from "next";

import { MbSwingPanel } from "@/components/mb/swing";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchMbSwing } from "@/lib/mb/fetch";
import { PAGES } from "@/lib/vocabulary";

/** `/mb/swing` — PM11's swing picks: the market switch, tonight's picks and their paper record. Read-only. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/mb/swing"].title,
  description: PAGES["/mb/swing"].blurb,
};

export default async function MbSwingPage() {
  const data = await fetchMbSwing({ all: true });
  return (
    <div className="space-y-8">
      <PageHeader title={PAGES["/mb/swing"].title} blurb={PAGES["/mb/swing"].blurb} />
      <SectionTabs section="mb" />
      <MbSwingPanel data={data} />
    </div>
  );
}
