import type { Metadata } from "next";
import Link from "next/link";

import { CompareBar } from "@/components/discover/compare-bar";
import { FilterRail, buildHref, type FilterState } from "@/components/discover/filter-rail";
import {
  ResultsModeToggle,
  ResultsView,
  isResultsMode,
  type ResultsMode,
} from "@/components/discover/results-view";
import { SelectionProvider } from "@/components/discover/selection-provider";
import { DisclosureBlock } from "@/components/explore/disclosure-block";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { ExploreUnavailable, definedParams, fetchExploreList } from "@/lib/explore/fetch";
import { uncomputedMetrics } from "@/lib/discover/metrics";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/discover/all` — the whole catalogue, as a workspace rather than a column.
 *
 * The page this replaces wrapped everything in `max-w-5xl` while the shell's own capsule is
 * `max-w-[104rem]`: forty rem of a desktop screen were being thrown away by the page inside its
 * own chrome. Three columns now — filters that stay put while results scroll, the results, and a
 * rail that explains what the numbers mean and what is missing from them.
 *
 * The rail is not decoration. Every basket here is momentum, run by the same engine, and most of
 * what a reader would want to judge them on is not computed yet; a column that says so beside the
 * results is more use than a longer list of the same six cards.
 */

const BASE = "/discover/all";
const PAGE_SIZE = 24;

function catalogueHref(state: FilterState, pageOffset: number): string {
  const base = buildHref(BASE, state);
  if (pageOffset <= 0) return base;
  return `${base}${base.includes("?") ? "&" : "?"}offset=${pageOffset}`;
}

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/discover/all"].title,
  description: PAGES["/discover/all"].blurb,
  robots: { index: false, follow: false },
};

function first(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export default async function AllBasketsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const viewParam = first(params.view);
  const mode: ResultsMode = isResultsMode(viewParam) ? viewParam : "cards";

  const state: FilterState = definedParams({
    max_min_amount: first(params.max_min_amount),
    access: first(params.access),
    volatility: first(params.volatility),
    category: first(params.category),
    rebalance_frequency: first(params.rebalance_frequency),
    sort: first(params.sort),
    order: first(params.order),
    q: first(params.q),
    view: viewParam,
  });

  /* AFH 5.8: catalogue pages via explore `limit`/`offset`; categories come on the list response. */
  const offsetRaw = Number.parseInt(first(params.offset) ?? "0", 10);
  const offset = Number.isFinite(offsetRaw) && offsetRaw > 0 ? offsetRaw : 0;

  let catalogue;
  try {
    // `view` is a presentation choice, not a catalogue filter — it never reaches the API.
    const { view, ...query } = state;
    void view;
    catalogue = await fetchExploreList(
      definedParams({
        ...query,
        limit: String(PAGE_SIZE),
        offset: String(offset),
      }),
    );
  } catch (error) {
    if (!(error instanceof ExploreUnavailable)) throw error;
    return (
      <div className="flex w-full max-w-[104rem] flex-col gap-6">
        <SectionTabs section="discover" />
        <PageHeader title={PAGES["/discover/all"].title} blurb={PAGES["/discover/all"].blurb} />
        <p className="max-w-prose rounded-md border border-border bg-muted/50 p-4 text-sm text-muted-foreground">
          The catalog could not be loaded. Reload, and if it keeps happening{" "}
          <Link href="/support" className="text-accent underline-offset-4 hover:underline">
            tell us
          </Link>
          .
        </p>
      </div>
    );
  }

  const filtered = Boolean(
    state.max_min_amount ||
      state.access ||
      state.volatility ||
      state.category ||
      state.rebalance_frequency,
  );
  const categories = catalogue.categories?.length
    ? catalogue.categories
    : [...new Set(catalogue.items.flatMap((basket) => basket.categories))].sort();
  const pageItems = catalogue.items;
  const prevOffset = Math.max(0, offset - PAGE_SIZE);
  const nextOffset = offset + PAGE_SIZE;
  const hasPrev = offset > 0;
  const hasNext = nextOffset < catalogue.total;

  return (
    <SelectionProvider>
      <div className="flex w-full max-w-[104rem] flex-col gap-6">
        <SectionTabs section="discover" />
        <PageHeader
          title={PAGES["/discover/all"].title}
          blurb={PAGES["/discover/all"].blurb}
          meta={
            <span className="text-xs text-muted-foreground">
              {catalogue.total} basket{catalogue.total === 1 ? "" : "s"}
              {filtered ? " matching these filters" : ""}
            </span>
          }
        />

        <div className="grid gap-6 lg:grid-cols-[15rem_minmax(0,1fr)] xl:grid-cols-[15rem_minmax(0,1fr)_18rem]">
          <div className="lg:sticky lg:top-20 lg:self-start">
            <FilterRail base={BASE} state={state} categories={categories} />
          </div>

          <div className="min-w-0 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-sm text-muted-foreground" data-testid="catalogue-page-count">
                {pageItems.length === 0
                  ? `0 shown of ${catalogue.total}`
                  : `${offset + 1}–${offset + pageItems.length} of ${catalogue.total}`}
              </p>
              <ResultsModeToggle
                mode={mode}
                hrefFor={(next) => buildHref(BASE, { ...state, view: next })}
              />
            </div>

            {pageItems.length === 0 ? (
              <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
                <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
                  Nothing matches these filters. Clear them to see the full catalog, or check back
                  after the nightly metrics job has run.
                </p>
                <Link
                  href={BASE}
                  className="mt-4 text-sm text-accent underline-offset-4 hover:underline"
                >
                  Clear filters
                </Link>
              </div>
            ) : (
              <ResultsView baskets={pageItems} mode={mode} />
            )}

            {catalogue.total > PAGE_SIZE ? (
              <nav
                aria-label="Catalogue pages"
                className="flex items-center justify-between gap-3"
                data-testid="catalogue-pagination"
              >
                {hasPrev ? (
                  <Link
                    href={catalogueHref(state, prevOffset)}
                    rel="prev"
                    className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
                  >
                    Previous
                  </Link>
                ) : (
                  <span />
                )}
                {hasNext ? (
                  <Link
                    href={catalogueHref(state, nextOffset)}
                    rel="next"
                    className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
                  >
                    Next
                  </Link>
                ) : (
                  <span />
                )}
              </nav>
            ) : null}
          </div>

          <aside
            className="hidden xl:block xl:sticky xl:top-20 xl:self-start"
            aria-label="About these numbers"
            data-testid="discover-rail"
          >
            <div className="space-y-4 rounded-xl border border-border/70 bg-card p-4">
              <div>
                <h2 className="text-sm font-semibold">Reading these cards</h2>
                <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
                  Volatility comes first on purpose. It is how much the basket moves in a year, in
                  both directions — a bigger number is a bumpier ride, not a worse strategy. The
                  return beside it names its own window, because a young basket cannot show a
                  five-year figure and should not borrow one.
                </p>
              </div>
              <div>
                <h2 className="text-sm font-semibold">Not shown, and why</h2>
                <ul className="mt-1.5 space-y-1 text-xs leading-relaxed text-muted-foreground">
                  {uncomputedMetrics()
                    .slice(0, 4)
                    .map((metric) => (
                      <li key={metric.key}>
                        <span className="text-foreground">{metric.label}</span> — not computed in
                        this product yet.
                      </li>
                    ))}
                </ul>
                <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                  They are left blank rather than estimated. A number nobody computed is worse
                  than an empty space.
                </p>
              </div>
              <div>
                <h2 className="text-sm font-semibold">Comparing</h2>
                <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
                  Pick two or three with the Compare button and the bar at the foot of the page
                  will take you to a table measured over the period they all share.
                </p>
              </div>
            </div>
          </aside>
        </div>

        <DisclosureBlock variant="performance-not-verified" />
        <p className="text-xs text-muted-foreground">
          Catalog browsing is read-only — investing builds an order plan elsewhere; nothing here
          places an order.
        </p>

        <CompareBar />
      </div>
    </SelectionProvider>
  );
}
