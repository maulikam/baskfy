import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { StopsDesk } from "@/components/stops/stops-desk";
import { fetchMe } from "@/lib/auth/me";

/**
 * `/stops` — review and arm GTT stops, staff only. The review is the desk's `GET /desk/stops`;
 * arming is same-origin `POST /desk/stops/arm` with confirm=true and the plan_id it issued.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Stops",
  description: "Positions without a working stop, and the GTT triggers that would cover them.",
  robots: { index: false, follow: false },
};

export default async function StopsPage() {
  const me = await fetchMe();
  if (!me?.is_staff) notFound();
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Stops"
        blurb="Every position checked against the broker's live triggers. Nothing is placed without an explicit confirmation."
      />
      <SectionTabs section="build" />
      <StopsDesk />
    </div>
  );
}
