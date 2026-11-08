import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { UserLookup } from "@/components/admin/user-lookup";
import { serverApi } from "@/lib/api/server";

/**
 * `/admin/users` — PROMPTS.md Prompt 17 §4's "user lookup" and "entitlement override".
 *
 * The query and the selected account are URL state (`?q=…&id=…`) rather than component state, so
 * a support conversation can be resumed from a pasted link and a reload does not lose the search.
 * That is the same choice `nuqs` makes for the screens filters (docs/02 §"Client data").
 */
export const metadata: Metadata = {
  title: "User lookup",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function AdminUsersPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; id?: string }>;
}) {
  const { q = "", id } = await searchParams;
  const api = await serverApi();

  const results = q
    ? await api.GET("/api/v1/admin/users", { params: { query: { q, limit: 20 } } })
    : null;
  if (results && !results.data && results.response.status === 404) notFound();

  const selected = id
    ? await api.GET("/api/v1/admin/users/{public_id}", { params: { path: { public_id: id } } })
    : null;

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-xl font-medium">User lookup</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          Overrides are layered on top of the plan by the same resolution every gated endpoint
          uses, so what this page shows is what the account actually gets. Give a reason and an
          expiry — a support grant with no end is how a comp account is created by accident.
        </p>
      </header>
      <UserLookup query={q} results={results?.data?.data ?? []} selected={selected?.data ?? null} />
    </div>
  );
}
