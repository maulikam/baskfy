import type { Metadata } from "next";

import { BacktestsList } from "@/components/backtests/backtests-list";
import { serverApi } from "@/lib/api/server";
import { PAGES } from "@/lib/vocabulary";

export const metadata: Metadata = {
  title: PAGES["/build/backtests"].title,
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

/** Fallback only when /meta/status is unreachable — never a second source of truth. */
const FALLBACK_DATA_START = "2011-01-01";

export default async function BuildBacktestsPage() {
  const api = await serverApi();
  const [runs, screens, status] = await Promise.all([
    api.GET("/api/v1/backtests"),
    api.GET("/api/v1/screens"),
    api.GET("/api/v1/meta/status"),
  ]);

  const earliest = status.data?.data_start_date ?? FALLBACK_DATA_START;
  const latest = status.data?.as_of ?? earliest;

  return (
    <BacktestsList
      initial={runs.data?.data ?? null}
      screens={screens.data?.data ?? []}
      earliest={earliest}
      latest={latest}
      error={runs.data ? null : (runs.error ?? "unreachable")}
    />
  );
}
