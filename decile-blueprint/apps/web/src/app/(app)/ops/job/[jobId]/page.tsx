import type { Metadata } from "next";

import { OpsJobView } from "@/components/ops/ops-job";
import { StaffOnly } from "@/components/ops/shared";
import { PageHeader } from "@/components/shell/page-header";
import { isStaffServer } from "@/lib/desk-ops/staff";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Operation run",
  robots: { index: false, follow: false },
};

export default async function OpsJobPage({ params }: { params: Promise<{ jobId: string }> }) {
  const [{ jobId }, staff] = await Promise.all([params, isStaffServer()]);
  return (
    <div className="flex flex-col gap-5">
      <PageHeader title={`Run ${jobId}`} blurb="One operation's command, exit code and full output." />
      {staff ? <OpsJobView jobId={jobId} /> : <StaffOnly what="Operations" />}
    </div>
  );
}
