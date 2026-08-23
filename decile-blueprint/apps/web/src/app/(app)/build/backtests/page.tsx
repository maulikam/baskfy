import type { Metadata } from "next";

import { BacktestsList } from "@/components/backtests/backtests-list";
import { serverApi } from "@/lib/api/server";
import { PAGES } from "@/lib/vocabulary";

export const metadata: Metadata = {
  title: PAGES["/build/backtests"].title,
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

const DATA_START = "2024-11-01";

export default async function BuildBacktestsPage() {
  const api = await serverApi();
  const [runs, screens, status] = await Promise.all([
    api.GET("/api/v1/backtests"),
    api.GET("/api/v1/screens"),
    api.GET("/api/v1/meta/status"),
  ]);

  return (
    <BacktestsList
      initial={runs.data?.data ?? null}
      screens={screens.data?.data ?? []}
      earliest={DATA_START}
      latest={status.data?.as_of ?? DATA_START}
      error={runs.data ? null : (runs.error ?? "unreachable")}
    />
  );
}
