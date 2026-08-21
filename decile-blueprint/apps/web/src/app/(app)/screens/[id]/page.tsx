import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ScreenEditor } from "@/components/screens/screen-editor";
import { serverApi } from "@/lib/api/server";

/**
 * docs/08 §Routes: "`/screens/new`, `/screens/[id]` | client-heavy form + **RSC first paint of
 * results**".
 *
 * The screen itself is fetched on the server, so the editor's header, name and saved definition
 * are in the first HTML. The *results* are not: they depend on the working definition, which the
 * URL may already have modified, and a server-rendered preview of the saved definition would be
 * replaced a frame later — a layout shift on every load of a shared link.
 */
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

export default async function ScreenPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const api = await serverApi();

  const [screen, status] = await Promise.all([
    api.GET("/api/v1/screens/{public_id}", { params: { path: { public_id: id } } }),
    api.GET("/api/v1/meta/status"),
  ]);

  if (!screen.data) notFound();

  return <ScreenEditor screen={screen.data} status={status.data ?? null} />;
}
