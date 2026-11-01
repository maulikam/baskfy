import type { Metadata } from "next";
import { Suspense } from "react";

import { OptionsWorkspace } from "@/components/opportunities/options-workspace";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { PAGES } from "@/lib/vocabulary";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/opportunities/options"].title,
  description: PAGES["/opportunities/options"].blurb,
};

export default function OpportunitiesOptionsPage() {
  return (
    <div className="space-y-8">
      <PageHeader
        title="Opportunities"
        blurb={PAGES["/opportunities/options"].blurb}
        meta="Expiry payoff is exact for the legs shown; quotes and Greeks are as of the analysis time. Not advice, and nothing here places an order."
      />
      <SectionTabs section="opportunities" />
      <Suspense fallback={<Skeleton className="h-48 w-full" />}>
        <OptionsWorkspace />
      </Suspense>
    </div>
  );
}
