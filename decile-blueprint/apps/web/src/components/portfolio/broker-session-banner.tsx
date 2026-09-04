import Link from "next/link";

import { fetchBrokerCatalog } from "@/lib/brokers/fetch";

/**
 * Say it where the numbers are, not only on /brokers — M84.
 *
 * Kite ends a session at the start of every trading day, so an expired broker is the normal
 * morning state rather than an exception. M79 taught the brokers panel to report it, but nothing
 * said so on the pages that go quietly stale as a result: the portfolio keeps rendering yesterday's
 * marks and looks fine, because a stale number and a fresh one look identical.
 *
 * Only `expired` earns a banner. Never-connected is not a problem to interrupt someone about — the
 * empty states already explain themselves — and a working session needs no notice at all. A banner
 * that appears when nothing is wrong is one people learn to scroll past.
 */
export async function BrokerSessionBanner() {
  let expired: string | null;
  try {
    const catalog = await fetchBrokerCatalog();
    // A catalog that cannot be read is not evidence of an expired session, so it says nothing.
    expired = catalog.brokers.find((b) => b.connection_status === "expired")?.short_name ?? null;
  } catch {
    return null;
  }
  if (expired === null) return null;

  return (
    <div
      role="status"
      data-testid="session-expired-banner"
      className="rounded-lg border border-warning/40 bg-warning-muted px-4 py-3 text-sm text-warning"
    >
      Your {expired} session expired — Kite ends one at the start of each trading day. Prices and
      holdings below are the last synced values until you{" "}
      <Link href="/brokers" className="underline underline-offset-4">
        reconnect
      </Link>
      .
    </div>
  );
}
