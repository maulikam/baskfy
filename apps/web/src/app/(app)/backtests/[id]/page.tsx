import type { Metadata } from "next";

import { BacktestResult } from "@/components/backtests/backtest-result";
import { serverApi } from "@/lib/api/server";

/**
 * `/backtests/[id]` — docs/08 §Routes: "client + polling/SSE".
 *
 * Server-rendered once so a finished run paints immediately, then handed to the client component,
 * which polls while the run is not terminal and subscribes to the SSE stream for the progress bar.
 */
export const metadata: Metadata = {
  title: "Backtest",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function BacktestPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const api = await serverApi();
  const { data, error } = await api.GET("/api/v1/backtests/{public_id}", {
    params: { path: { public_id: id } },
  });

  return (
    <BacktestResult
      publicId={id}
      initial={data ?? null}
      error={data ? null : (error ?? "unreachable")}
    />
  );
}
