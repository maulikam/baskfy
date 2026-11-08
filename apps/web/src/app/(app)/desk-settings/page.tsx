import type { Metadata } from "next";

import { SettingsForm } from "@/components/desk-settings/settings-form";
import { StaffOnly } from "@/components/ops/shared";
import { PageHeader } from "@/components/shell/page-header";
import { isStaffServer } from "@/lib/desk-ops/staff";

// Port of the desk's /settings: saves go to same-origin POST /desk/settings; staff only, confirm first.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Desk settings",
  description: "The momentum desk's tunable thresholds, where each value comes from, and every change.",
  robots: { index: false, follow: false },
};

export default async function DeskSettingsPage() {
  const staff = await isStaffServer();
  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Desk settings" blurb="Sizing, rebalance discipline, stops and the regime overlay — with every change on record." />
      {staff ? <SettingsForm /> : <StaffOnly what="Desk settings" />}
    </div>
  );
}
