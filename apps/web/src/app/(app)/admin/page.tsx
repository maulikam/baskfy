import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { DataVersions } from "@/components/admin/data-versions";
import { ProviderHealth } from "@/components/admin/provider-health";
import { ReprocessForm } from "@/components/admin/reprocess-form";
import { formatDateTimeIST } from "@/lib/format";
import { serverApi } from "@/lib/api/server";

/**
 * `/admin` — the staff surface's front page (PROMPTS.md Prompt 17 deliverable 4).
 *
 * Not in `NAV_GROUPS`: `src/lib/nav.ts` is pinned to docs/08's sidebar IA by a test and docs/08
 * lists no admin section, so the link lives in the user menu and only for staff. That is a
 * courtesy — `require_staff` on the API is the enforcement, and it answers **404** to a non-staff
 * caller (`docs/DECISIONS.md` §17.3). This page renders the same 404 when the API does, so the
 * two agree.
 *
 * `noindex` and `force-dynamic`: it is per-request, per-caller and must never be cached or
 * crawled.
 */
export const metadata: Metadata = {
  title: "Admin",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  const api = await serverApi();
  const [versions, providers, actions] = await Promise.all([
    api.GET("/api/v1/admin/data-versions", { params: { query: { limit: 20 } } }),
    api.GET("/api/v1/admin/providers"),
    api.GET("/api/v1/admin/actions", { params: { query: { limit: 20 } } }),
  ]);

  // A non-staff caller gets 404 from every one of those. Render Next's own 404 rather than an
  // empty admin page, so the browser and the API say the same thing.
  if (!versions.data && versions.response.status === 404) notFound();

  return (
    <div className="space-y-10">
      <header className="space-y-1">
        <h1 className="text-xl font-medium">Admin</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          Staff only. Every action here writes an{" "}
          <code className="font-mono">admin_action</code> row naming you. The runbooks in{" "}
          <code className="font-mono">docs/runbooks/</code> say what to do with what you find.
        </p>
        <p className="text-sm">
          <Link className="underline underline-offset-2" href="/admin/pipeline">
            Pipeline runs
          </Link>{" "}
          ·{" "}
          <Link className="underline underline-offset-2" href="/admin/users">
            User lookup
          </Link>
        </p>
      </header>

      <section className="space-y-3">
        <h2 className="text-base font-medium">Published data versions</h2>
        <DataVersions
          rows={versions.data?.data ?? []}
          current={versions.data?.current ?? 0}
        />
      </section>

      <section className="space-y-3">
        <h2 className="text-base font-medium">Provider health</h2>
        <ProviderHealth providers={providers.data?.data ?? []} />
      </section>

      <section className="space-y-3">
        <h2 className="text-base font-medium">Reprocess an instrument</h2>
        <ReprocessForm />
      </section>

      <section className="space-y-3">
        <h2 className="text-base font-medium">Recent staff actions</h2>
        {actions.data && actions.data.data.length > 0 ? (
          <ul className="space-y-1 text-sm">
            {actions.data.data.map((entry, index) => (
              <li key={`${entry.created_at}-${index}`} className="font-mono text-xs">
                {formatDateTimeIST(entry.created_at)} · {entry.actor ?? "unknown"} ·{" "}
                {entry.action} · {entry.target}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">
            Nothing yet. Re-runs, reprocesses and entitlement overrides land here.
          </p>
        )}
      </section>
    </div>
  );
}
