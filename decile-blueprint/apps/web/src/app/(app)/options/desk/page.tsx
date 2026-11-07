import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { FnoConsole } from "@/components/options-desk/fno-console";
import { PageHeader } from "@/components/shell/page-header";
import { OptionsSubNav } from "@/components/shell/section-tabs";
import { fetchMe } from "@/lib/auth/me";

/** `/options/desk` — the desk's `/fno` console in the shell. Staff only; confirms POST to `/desk/fno/execute`. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "F&O desk · Options",
  description: "Today's F&O plans with live legs, open structures, and the confirm for each plan.",
  robots: { index: false, follow: false },
};

export default async function OptionsDeskPage() {
  const me = await fetchMe();
  if (!me?.is_staff) notFound();
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="F&O desk"
        blurb="Today's F&O plans with live legs, open structures, and the confirm for each plan."
      />
      <OptionsSubNav />
      <FnoConsole />
    </div>
  );
}
