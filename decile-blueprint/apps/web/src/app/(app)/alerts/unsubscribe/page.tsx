import type { Metadata } from "next";
import Link from "next/link";

import { unsubscribeFromAlert } from "@/app/actions/integrations";

/**
 * The target of the "Stop this alert" link in every alert email — Prompt 20 §3.
 *
 * **It acts on load, with no session and no button.** A one-click unsubscribe is one click: a
 * page that asked the recipient to sign in first would be useless to exactly the people most
 * likely to use it, and one that asked them to confirm would fail the "one-click" part of what
 * the phrase means. The token is 128 unguessable bits scoped to one alert, and the endpoint
 * answers the same thing whether it matched or not (`baskfy_api.routers.alerts`).
 *
 * A mail client that prefetches links will therefore unsubscribe on prefetch. That is the
 * documented trade of one-click unsubscribe and is the behaviour RFC 8058 standardises for the
 * `List-Unsubscribe-Post` header; the alternative loses more subscribers to friction than this
 * loses to prefetch.
 */
export const metadata: Metadata = {
  title: "Unsubscribe",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function UnsubscribePage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token = "" } = await searchParams;
  const result = token
    ? await unsubscribeFromAlert(token)
    : { ok: false, message: "That link is missing its token." };

  return (
    <div className="flex max-w-xl flex-col gap-4">
      <h1 className="text-xl font-semibold tracking-tight">
        {result.ok ? "Unsubscribed" : "That link did not work"}
      </h1>
      <p className="text-sm text-muted-foreground">{result.message}</p>
      <p className="text-sm text-muted-foreground">
        You can turn it back on, or change how often it arrives, on{" "}
        <Link href="/alerts" className="text-accent underline-offset-4 hover:underline">
          your alerts page
        </Link>
        .
      </p>
    </div>
  );
}
