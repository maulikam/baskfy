import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { RebalanceDesk } from "@/components/rebalance/rebalance-desk";
import { SwingOpportunities } from "@/components/rebalance/swing-opportunities";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchMe } from "@/lib/auth/me";

/**
 * `/rebalance` — the desk's weekly momentum rebalance, staff only. Analyze issues a plan; orders
 * go only through same-origin `POST /desk/execute` with confirm=true and that plan_id. No auto-execute.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Weekly rebalance",
  description: "Upload the weekly scan, review the order plan, confirm it on the desk.",
  robots: { index: false, follow: false },
};

export default async function RebalancePage() {
  const me = await fetchMe();
  if (!me?.is_staff) notFound();
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Weekly rebalance"
        blurb="Upload the weekly scan, review every line of the plan, then confirm. Orders go through the desk's gateway."
      />
      <SectionTabs section="build" />
      <RebalanceDesk />
      <SwingOpportunities />
    </div>
  );
}
