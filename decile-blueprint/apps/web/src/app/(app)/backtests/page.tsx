import type { Metadata } from "next";

import { BacktestsList } from "@/components/backtests/backtests-list";
import { serverApi } from "@/lib/api/server";

/**
 * `/backtests` — docs/08 §Routes: "client + polling/SSE".
 *
 * The first paint is fetched on the server so the list is not a spinner; everything after that is
 * the client component's, because a queued run has to update itself.
 *
 * `DATA_START` is docs/01 §2.13's promise ("historical data is available from 1 Nov 2024"), which
 * `baskfy_api.screener.DATA_START_DATE` enforces as a `422 no-trading-day`. The form uses it as
 * the earliest selectable date so a user cannot ask for a window the service will refuse.
 */
export const metadata: Metadata = {
  title: "Backtests",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

const DATA_START = "2024-11-01";

export default async function BacktestsPage() {
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
