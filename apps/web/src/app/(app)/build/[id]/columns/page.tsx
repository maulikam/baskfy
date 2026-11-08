import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ColumnsEditor } from "@/components/screens/columns-editor";
import { serverApi } from "@/lib/api/server";

/** docs/08 §Routes: "`/screens/[id]/columns` | client". */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Edit columns",
  robots: { index: false, follow: false },
};

export default async function ColumnsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const api = await serverApi();
  const [screen, columns] = await Promise.all([
    api.GET("/api/v1/screens/{public_id}", { params: { path: { public_id: id } } }),
    api.GET("/api/v1/meta/columns"),
  ]);

  if (!screen.data) notFound();

  return <ColumnsEditor screen={screen.data} columns={columns.data ?? []} />;
}
