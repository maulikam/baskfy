import type { Metadata } from "next";

import { LifecycleBoard } from "@/components/lifecycle/lifecycle-board";
import { StaffOnly } from "@/components/ops/shared";
import { PageHeader } from "@/components/shell/page-header";
import { isStaffServer } from "@/lib/desk-ops/staff";

// Port of the desk's /lifecycle: reads /desk/lifecycle/data; adopt posts to /desk/lifecycle/adopt (staff, confirm).
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Lifecycle",
  description: "Every open strategy trade, entry to exit, against the broker.",
  robots: { index: false, follow: false },
};

export default async function LifecyclePage() {
  const staff = await isStaffServer();
  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Lifecycle" blurb="Every open trade across the swing, TWT and VBT strategies, entry to exit, against the broker." />
      {staff ? <LifecycleBoard /> : <StaffOnly what="Lifecycle" />}
    </div>
  );
}
