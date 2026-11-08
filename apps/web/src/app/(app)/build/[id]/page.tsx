import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ScreenEditor } from "@/components/screens/screen-editor";
import { serverApi } from "@/lib/api/server";

export const dynamic = "force-dynamic";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<Metadata> {
  const { id } = await params;
  const api = await serverApi();
  const { data } = await api.GET("/api/v1/screens/{public_id}", {
    params: { path: { public_id: id } },
  });
  return { title: data?.name ?? "Screen", robots: { index: false, follow: false } };
}

export default async function BuildScreenPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const api = await serverApi();

  const [screen, status] = await Promise.all([
    api.GET("/api/v1/screens/{public_id}", { params: { path: { public_id: id } } }),
    api.GET("/api/v1/meta/status"),
  ]);

  if (!screen.data) notFound();

  return <ScreenEditor screen={screen.data} status={status.data ?? null} />;
}
