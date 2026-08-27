import type { Metadata } from "next";
import Link from "next/link";

import { CollectionShelves } from "@/components/collections/collection-shelves";
import { CompareBar } from "@/components/discover/compare-bar";
import { GoalComposer } from "@/components/discover/goal-composer";
import { SelectionProvider } from "@/components/discover/selection-provider";
import { MatchBreakdown, StartingChoices } from "@/components/discover/starting-choices";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { type Collection, fetchCollections } from "@/lib/collections/fetch";
import { preferencesToParams, startingChoices } from "@/lib/discover/match";
import { hasStatedPreferences, preferencesFromParams } from "@/lib/discover/preferences";
import { ExploreUnavailable, definedParams, fetchExploreList } from "@/lib/explore/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/discover` — the hub, was `/baskets`.
 *
 * The page a reader lands on asks them a question instead of showing them a wall. Say how you
 * want to invest; get three baskets that match it, each with the arithmetic of the match written
 * out. Everything else — the whole catalogue, collections, comparison, saved — is a tab away.
 *
 * **This is filtering and the page keeps saying so.** D3 is unreviewed and Baskfy is not a
 * registered adviser, so nothing here is "for you" or "best": each result names which preferences
 * it matched and which it did not, and `lib/discover/match` is where that wording lives.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/discover"].title,
  description: PAGES["/discover"].blurb,
  robots: { index: false, follow: false },
};

export default async function DiscoverPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const prefs = preferencesFromParams(params);
  const stated = hasStatedPreferences(params);

  let catalogue;
  try {
    catalogue = await fetchExploreList(definedParams(preferencesToParams(prefs)));
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
    catalogue = null;
  }

  // Shelves are secondary. A hub that renders without them beats one that does not render.
  let collections: Collection[] = [];
  try {
    collections = (await fetchCollections()).items;
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
  }

  const choices = catalogue ? startingChoices(catalogue.items, prefs) : [];

  return (
    <SelectionProvider>
      <div className="flex w-full max-w-[104rem] flex-col gap-8">
        <SectionTabs section="discover" />
        <PageHeader title={PAGES["/discover"].title} blurb={PAGES["/discover"].blurb} />

        <section className="rounded-xl border border-border/70 bg-card p-5 sm:p-6">
          <GoalComposer initial={prefs} />
        </section>

        {catalogue === null ? (
          <p className="max-w-prose rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
            The catalogue could not be loaded. Reload, and if it keeps happening{" "}
            <Link href="/support" className="text-accent underline-offset-4 hover:underline">
              tell us
            </Link>
            .
          </p>
        ) : (
          <section className="space-y-4" aria-labelledby="starting-choices-heading">
            <div>
              <h2 id="starting-choices-heading" className="text-lg font-semibold tracking-tight">
                {stated ? "Baskets matching your preferences" : "Somewhere to start"}
              </h2>
              <p className="mt-1 max-w-[70ch] text-sm text-muted-foreground">
                {stated
                  ? `${catalogue.total} basket${catalogue.total === 1 ? "" : "s"} passed the filter. These three are the closest, the calmest and the strongest of them.`
                  : "Three baskets from the catalogue, before you have told us anything. Set your preferences above to filter them."}
              </p>
            </div>

            <StartingChoices choices={choices} />

            {choices.length > 0 ? (
              <div className="grid gap-3 lg:grid-cols-3">
                {choices.map((choice) => (
                  <MatchBreakdown key={choice.kind} choice={choice} />
                ))}
              </div>
            ) : null}

            <Link
              href="/discover/all"
              className="inline-block text-sm text-accent underline-offset-4 hover:underline"
            >
              See all {catalogue.total} baskets →
            </Link>
          </section>
        )}

        <CollectionShelves collections={collections} />

        <DisclosureBlock variant="performance-not-verified" />
        <p className="text-xs text-muted-foreground">
          Discover is read-only — nothing on it places an order, and investing builds an order
          plan you confirm elsewhere.
        </p>

        <CompareBar />
      </div>
    </SelectionProvider>
  );
}
