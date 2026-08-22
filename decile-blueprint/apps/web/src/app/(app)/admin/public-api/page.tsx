import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { serverApi } from "@/lib/api/server";

/**
 * `/admin/public-api` — PROMPTS.md Prompt 20 §2: "make that dependency explicit in the code **and
 * the admin UI**".
 *
 * This page has no buttons, and that is the point. The public tier is held closed by two things:
 * an environment variable an operator controls, and a source constant that only a commit can
 * change. Putting a toggle here would make the second one a lie.
 *
 * Everything on the page is the server's own answer, including docs/11's sentence — it is read
 * from `baskfy_core.public_api.DATA_REDISTRIBUTION_REVIEW`, not retyped here, so a staff member
 * reading this is reading the requirement the code is actually enforcing.
 */
export const metadata: Metadata = {
  title: "Public API gate",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function AdminPublicApiPage() {
  const api = await serverApi();
  const { data, response } = await api.GET("/api/v1/admin/public-api", {});
  if (!data) {
    // A non-staff caller gets 404 from the API (`docs/DECISIONS.md` §17.3); mirror it here rather
    // than rendering an empty page that hints the route exists.
    if (response.status === 404) notFound();
    throw new Error(`/admin/public-api responded ${response.status}`);
  }

  return (
    <div className="max-w-3xl space-y-6">
      <header className="space-y-1">
        <h1 className="text-xl font-medium">Public API gate</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          The public read API serves only when <em>both</em> locks are open. There is deliberately
          no control on this page: one lock is an environment variable, the other is a constant in
          the source, and the second can only be opened by a commit somebody signed.
        </p>
      </header>

      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <dt className="text-muted-foreground">Serving</dt>
        <dd className={data.serving ? "text-negative" : "text-positive"}>
          {data.serving ? "YES — the public tier is live" : "no"}
        </dd>
        <dt className="text-muted-foreground">
          <code className="font-mono text-xs">BASKFY_PUBLIC_API_ENABLED</code>
        </dt>
        <dd>{data.flag_enabled ? "true" : "false"}</dd>
        <dt className="text-muted-foreground">Data-redistribution review</dt>
        <dd className={data.review_signed_off ? "" : "text-negative"}>
          {data.review_signed_off
            ? `signed off ${data.signed_off_on} by ${data.signed_off_by}`
            : "NOT SIGNED OFF"}
        </dd>
        <dt className="text-muted-foreground">Opinion</dt>
        <dd>{data.opinion_reference || "none on file"}</dd>
        <dt className="text-muted-foreground">Version</dt>
        <dd>
          <code className="font-mono text-xs">
            {data.prefix} ({data.api_version})
          </code>
        </dd>
      </dl>

      <section className="space-y-2 rounded-xl border border-border/70 bg-muted/50 p-4">
        <h2 className="text-sm font-medium">The requirement, from docs/11</h2>
        <p className="max-w-prose text-sm text-muted-foreground">{data.requirement}</p>
      </section>

      <section className="grid gap-6 sm:grid-cols-2">
        <div className="space-y-2">
          <h2 className="text-sm font-medium">Served, if it opens ({data.served_fields.length})</h2>
          <p className="font-mono text-xs leading-relaxed text-muted-foreground">
            {data.served_fields.join(" · ")}
          </p>
        </div>
        <div className="space-y-2">
          <h2 className="text-sm font-medium">Withheld ({data.withheld_fields.length})</h2>
          <p className="font-mono text-xs leading-relaxed text-muted-foreground">
            {data.withheld_fields.join(" · ")}
          </p>
          <p className="max-w-prose text-xs text-muted-foreground">
            Every field denominated in rupees per share. A caller cannot read a price out of this
            API, which is the line the licence draws.
          </p>
        </div>
      </section>
    </div>
  );
}
