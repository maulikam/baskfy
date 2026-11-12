import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { TrailingStopsDesk } from "@/components/gtt-trail/trailing-stops-desk";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchMe } from "@/lib/auth/me";

/** `/trailing-stops` — GTT-TRAIL's per-strategy policy, preview and journal, staff only. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Trailing stops",
  description: "Raise resting GTT stops as profit builds: breakeven, then a trail under the highest close.",
  robots: { index: false, follow: false },
};

export default async function TrailingStopsPage() {
  const me = await fetchMe();
  if (!me?.is_staff) notFound();
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Trailing stops"
        blurb="Per-strategy rules that move a GTT stop up as a position gains. Changes apply only when the desk's master flag is on."
      />
      <SectionTabs section="build" />
      <TrailingStopsDesk />
    </div>
  );
}
