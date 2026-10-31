import type { Metadata } from "next";

import { SwingWorkspace } from "@/components/opportunities/swing-workspace";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { PAGES } from "@/lib/vocabulary";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/opportunities/swing"].title,
  description: PAGES["/opportunities/swing"].blurb,
};

export default function OpportunitiesSwingPage() {
  return (
    <div className="space-y-8">
      <PageHeader
        title="Opportunities"
        blurb={PAGES["/opportunities/swing"].blurb}
        meta="Research output from rule-based scans — not advice, and nothing here places an order."
      />
      <SectionTabs section="opportunities" />
      <SwingWorkspace />
    </div>
  );
}
