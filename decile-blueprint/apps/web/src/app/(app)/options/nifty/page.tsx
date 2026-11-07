import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { NiftyConsole } from "@/components/options-desk/nifty-console";
import { PageHeader } from "@/components/shell/page-header";
import { OptionsSubNav } from "@/components/shell/section-tabs";
import { fetchMe } from "@/lib/auth/me";

/** `/options/nifty` — the desk's NIFTY options console in the shell. Staff only; POSTs go to `/desk`. */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "NIFTY desk · Options",
  description: "Today's NIFTY options plans and open positions, with confirm and close.",
  robots: { index: false, follow: false },
};

export default async function OptionsNiftyDeskPage() {
  const me = await fetchMe();
  if (!me?.is_staff) notFound();
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="NIFTY desk"
        blurb="Today's NIFTY options plans and open positions, with confirm and close."
      />
      <OptionsSubNav />
      <NiftyConsole />
    </div>
  );
}
