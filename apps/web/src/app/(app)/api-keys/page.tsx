import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { ApiKeyManager } from "@/components/integrations/api-key-manager";
import { serverApi } from "@/lib/api/server";
import { auth } from "@/lib/auth";

/**
 * `/api-keys` — PROMPTS.md Prompt 20 §1: "creation, scoping (read-only), rotation, revocation,
 * per-key rate limits, and a usage dashboard."
 *
 * The page says out loud that the surface these keys open **is not serving yet**, because it is
 * not: docs/11 §Compliance requires a written data-redistribution opinion before the public API
 * tier is enabled, and the response's `data_redistribution_signed_off` is the server's own answer
 * to whether that has happened. A key page that implied otherwise would be selling something the
 * account cannot use.
 *
 * Never cached — a key list is per-account, and the usage figures move.
 */
export const metadata: Metadata = {
  title: "API keys",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function ApiKeysPage() {
  const session = await auth();
  if (!session?.accessToken) redirect("/login?next=/api-keys");

  const api = await serverApi();
  const { data } = await api.GET("/api/v1/keys", {});

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">API keys</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          Read-only credentials for the public analytics API. Presented in the{" "}
          <code className="font-mono text-xs">X-API-Key</code> header. A key reads derived
          analytics — factor values, screen results and market breadth — and nothing else.
        </p>
      </header>

      {data && !data.public_api_enabled ? (
        <p className="rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          <strong className="font-medium text-foreground">
            The public API is not serving requests yet.
          </strong>{" "}
          Keys can be created and revoked now, and they will start working the day the tier opens.
          It is held closed by a data-licensing review: the market data behind these analytics is
          licensed for our own use, and serving it to third parties needs written clearance first.{" "}
          <Link href="/support" className="text-accent underline-offset-4 hover:underline">
            Ask us where that stands
          </Link>
          .
        </p>
      ) : null}

      {data ? (
        <ApiKeyManager keys={data.keys} />
      ) : (
        <p className="rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          Your keys could not be loaded. Reload, and if it keeps happening tell us.
        </p>
      )}
    </div>
  );
}
