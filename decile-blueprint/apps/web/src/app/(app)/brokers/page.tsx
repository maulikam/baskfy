import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { BrokerGrid } from "@/components/brokers/broker-grid";
import { PageHeader } from "@/components/shell/page-header";
import { auth } from "@/lib/auth";
import { BrokersUnavailable, fetchBrokerCatalog } from "@/lib/brokers/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/brokers` — M41 / P5.8. Connect your broker in a few clicks.
 *
 * The grid is real; live OAuth is source-gated on D3 (docs/smallcase Track C). While the gate
 * is shut, Connect explains why and points at CSV import so the portfolio path still works.
 */
export const metadata: Metadata = {
  title: PAGES["/brokers"].title,
  description: PAGES["/brokers"].blurb,
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function BrokersPage() {
  const session = await auth();
  if (!session?.accessToken) redirect("/login?next=/brokers");

  let catalog;
  try {
    catalog = await fetchBrokerCatalog();
  } catch (error) {
    if (!(error instanceof BrokersUnavailable)) throw error;
    return (
      <>
        <PageHeader title={PAGES["/brokers"].title} blurb={PAGES["/brokers"].blurb} />
        <p className="max-w-prose rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          The broker list could not be loaded. Reload, and if it keeps happening{" "}
          <Link href="/support" className="text-accent underline-offset-4 hover:underline">
            tell us
          </Link>
          .
        </p>
      </>
    );
  }

  return (
    <div className="flex max-w-5xl flex-col gap-6">
      <PageHeader
        title={PAGES["/brokers"].title}
        blurb={PAGES["/brokers"].blurb}
        meta={
          <span className="text-xs text-muted-foreground">
            {catalog.brokers.length} brokers · {catalog.adapters_wired} adapter
            {catalog.adapters_wired === 1 ? "" : "s"} wired
          </span>
        }
      />

      {!catalog.gate.live_oauth_enabled ? (
        <p className="rounded-md border border-border bg-muted/50 px-4 py-3 text-sm text-muted-foreground">
          <strong className="font-medium text-foreground">Connect is ready in the UI.</strong> Live
          broker login opens after Baskfy&apos;s regulatory posture is recorded — until then a
          click shows the flow and explains why the redirect is held. You can still manage a
          portfolio by uploading a holdings CSV on{" "}
          <Link href="/portfolios" className="text-accent underline-offset-4 hover:underline">
            My portfolios
          </Link>
          .
        </p>
      ) : null}

      <BrokerGrid brokers={catalog.brokers} gate={catalog.gate} />
    </div>
  );
}
