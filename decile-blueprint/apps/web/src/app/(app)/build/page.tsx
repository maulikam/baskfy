import type { Metadata } from "next";

import { ScreensList } from "@/components/screens/screens-list";
import { serverApi } from "@/lib/api/server";
import { PAGES } from "@/lib/vocabulary";

export const metadata: Metadata = {
  title: PAGES["/build"].title,
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function BuildPage() {
  const api = await serverApi();
  const { data, error } = await api.GET("/api/v1/screens");

  return <ScreensList initial={data?.data ?? null} error={data ? null : (error ?? "unreachable")} />;
}
