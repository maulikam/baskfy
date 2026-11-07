import type { Metadata } from "next";

import { OpsBoard } from "@/components/ops/ops-board";
import { StaffOnly } from "@/components/ops/shared";
import { PageHeader } from "@/components/shell/page-header";
import { isStaffServer } from "@/lib/desk-ops/staff";

// Port of the desk's /ops: allowlisted operations via same-origin POST /desk/ops/run; staff only.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Operations",
  description: "Run the desk's allowlisted operations and read what each run did.",
  robots: { index: false, follow: false },
};

export default async function OpsPage() {
  const staff = await isStaffServer();
  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Operations" blurb="The desk's allowlisted jobs, their history and the daily collection's health." />
      {staff ? <OpsBoard /> : <StaffOnly what="Operations" />}
    </div>
  );
}
