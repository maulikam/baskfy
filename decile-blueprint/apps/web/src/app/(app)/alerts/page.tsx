import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { AlertManager } from "@/components/integrations/alert-manager";
import { serverApi } from "@/lib/api/server";
import { auth } from "@/lib/auth";

/**
 * `/alerts` — PROMPTS.md Prompt 20 §3's subscriptions, and the "manage your alerts" link every
 * alert email carries.
 *
 * Never cached: the list is per-account and the last-sent stamps move nightly.
 */
export const metadata: Metadata = {
  title: "Screen alerts",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function AlertsPage() {
  const session = await auth();
  if (!session?.accessToken) redirect("/login?next=/alerts");

  const api = await serverApi();
  const [alerts, screens] = await Promise.all([
    api.GET("/api/v1/alerts", {}),
    api.GET("/api/v1/screens", {}),
  ]);
  const data = alerts.data;

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Screen alerts</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          An email after each publish, listing what entered the screen, what left it, and what
          moved. Computed by comparing the night&apos;s run with the previous one — a screen whose
          definition you edited is skipped rather than reported, because the difference would be
          your edit rather than the market.
        </p>
      </header>

      {data ? (
        <AlertManager
          alerts={data.alerts}
          screens={(screens.data?.data ?? []).map((screen) => ({
            public_id: screen.public_id,
            name: screen.name,
          }))}
        />
      ) : (
        <p className="rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          Your alerts could not be loaded. Reload, and if it keeps happening tell us.
        </p>
      )}
    </div>
  );
}
