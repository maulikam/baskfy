import type { Metadata } from "next";

import { ScreensList } from "@/components/screens/screens-list";
import { serverApi } from "@/lib/api/server";

/**
 * docs/08 §Routes: "`/screens` | RSC list". docs/01 §1: "List of 'Example Screens' (6, read-only
 * templates) and 'Your Screens'."
 *
 * Fetched on the server so the first paint already has the list — `GET /screens` returns both
 * sections in one call (docs/07 §Screens), and the client component takes it from there for the
 * run / duplicate / delete actions.
 */
export const metadata: Metadata = {
  title: "Screens",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function ScreensPage() {
  const api = await serverApi();
  const { data, error } = await api.GET("/api/v1/screens");

  return <ScreensList initial={data?.data ?? null} error={data ? null : (error ?? "unreachable")} />;
}
