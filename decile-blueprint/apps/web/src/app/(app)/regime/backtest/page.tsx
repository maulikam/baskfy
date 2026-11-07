import type { Metadata } from "next";

import { ReadOnlyFooter } from "@/components/desk/ui";
import { StaffOnly } from "@/components/ops/shared";
import { RegimeBacktestView } from "@/components/regime-backtest/backtest-view";
import { PageHeader } from "@/components/shell/page-header";
import { isStaffServer } from "@/lib/desk-ops/staff";

// Port of the desk's /regime/backtest: reads the stored artifact via same-origin /desk (JSON); read-only.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Regime backtest",
  description: "How each regime-overlay design would have done since 2005, and what it cost to hold.",
  robots: { index: false, follow: false },
};

export default async function RegimeBacktestPage() {
  const staff = await isStaffServer();
  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Regime backtest" blurb="Every overlay design against no overlay: return, drawdown, turnover and cost." />
      {staff ? <RegimeBacktestView /> : <StaffOnly what="The regime backtest" />}
      <ReadOnlyFooter />
    </div>
  );
}
