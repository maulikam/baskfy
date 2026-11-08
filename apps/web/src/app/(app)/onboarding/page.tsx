import type { Metadata } from "next";

import { OnboardingWizard } from "@/components/onboarding/onboarding-wizard";
import { PageHeader } from "@/components/shell/page-header";
import { fetchBrokerCatalog } from "@/lib/brokers/fetch";
import { ExploreUnavailable, fetchExploreList } from "@/lib/explore/fetch";
import { fetchPortfolioOverview } from "@/lib/portfolio/fetch";

/**
 * `/onboarding` — first-login path: connect → import → preferences → pick a basket (AF I.4).
 *
 * A sign-in that named no `?next=` lands here: `actions/auth.ts` `DEFAULT_DESTINATION` was
 * pointed at this page in `dd23b59`.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Get started",
  description: "Connect a broker, import holdings, and pick a first basket.",
  robots: { index: false, follow: false },
};

export default async function OnboardingPage() {
  // An unreachable catalogue reads as "no broker connected": the wizard's first step is to
  // connect one, which is the right thing to offer either way.
  const catalog = await fetchBrokerCatalog().catch(() => null);
  const brokerConnected = catalog?.brokers.some((broker) => broker.connected) ?? false;

  const overview = await fetchPortfolioOverview().catch(() => null);
  const holdingsSynced = Boolean(
    overview?.sync_summary &&
      !/not synced|no broker|never/i.test(overview.sync_summary),
  );

  let sampleBaskets: { slug: string; name: string }[] = [];
  try {
    sampleBaskets = (await fetchExploreList({ sort: "name", order: "asc" })).items
      .slice(0, 3)
      .map((item) => ({ slug: item.slug, name: item.name }));
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
  }

  return (
    <div className="flex max-w-2xl flex-col gap-6" data-testid="onboarding-page">
      <PageHeader
        title="Get started"
        blurb="Connect, import, say how you invest, then open a basket. Nothing here places an order."
      />
      <OnboardingWizard
        brokerConnected={brokerConnected}
        holdingsSynced={holdingsSynced}
        sampleBaskets={sampleBaskets}
      />
    </div>
  );
}
