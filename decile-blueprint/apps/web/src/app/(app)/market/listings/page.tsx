import type { Metadata, Route } from "next";
import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { PAGES } from "@/lib/vocabulary";
import { ListingsFilters } from "@/components/market/listings-filters";
import { EMPTY_CELL, formatTradeDate } from "@/lib/format";
import { fetchListings } from "@/lib/market/fetch";

/**
 * `/listings` — Prompt 11 deliverable 4, docs/07's `GET /listings?from&to&series=&cursor=`.
 * docs/08 §Routes: "RSC + cursor pagination".
 *
 * Cursor, not page number, all the way to the URL. The register grows every night, so an offset
 * would quietly repeat and skip rows between one page and the next — and "page 7" is not a stable
 * thing to link to when the rows above it move. `?cursor=` is.
 *
 * The consequence is that there is no "previous" link and no page count: a keyset cursor points
 * forward only. Browser Back is the previous page, and it works exactly right because each page
 * is a distinct URL.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/market/listings"].title,
  description: "Every instrument listed on the NSE, newest first.",
};

/** docs/07 §Conventions: "Cursor pagination: `?limit=100&cursor=…`". */
const PAGE_SIZE = 100;

/** The series NSE uses for equity-like instruments, offered as a filter. */
const SERIES_OPTIONS = ["EQ", "BE", "BZ", "SM", "ST", "GS", "IV", "MF"] as const;

function first(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export default async function ListingsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const cursor = first(params.cursor);
  const series = first(params.series) ?? "";
  const search = first(params.search) ?? "";

  const page = await fetchListings({ cursor, series, search });

  /* `typedRoutes` cannot check a route built from a runtime cursor, so the cast is at the one
     place the string is assembled — from our own route and our own encoded cursor. */
  const nextHref = ((): Route | null => {
    if (!page.next_cursor) return null;
    const query = new URLSearchParams();
    if (series) query.set("series", series);
    if (search) query.set("search", search);
    query.set("cursor", page.next_cursor);
    return `/market/listings?${query.toString()}` as Route;
  })();

  return (
    <>
      <SectionTabs section="market" />
      <PageHeader
        title={PAGES["/market/listings"].title}
        blurb={PAGES["/market/listings"].blurb}
        meta={`Newest listings first. Listing dates may reflect when we first saw a name in the pipeline, not the exchange IPO date — treat clustered same-day rows with care. ${PAGE_SIZE} at a time.`}
      />

      <p
        role="status"
        className="rounded-xl border border-border bg-muted/50 px-4 py-3 text-sm text-muted-foreground"
      >
        Data note: until the listings register is reconciled against exchange IPO dates, this table
        can show many names sharing one backfill day. Prefer Market → Today for live index moves.
      </p>

      <ListingsFilters series={series} search={search} options={SERIES_OPTIONS} />

      {page.data.length === 0 ? (
        <p className="py-12 text-center text-sm text-muted-foreground">
          Nothing matches what you have filtered for.
        </p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">
            Listings, newest first, with listing date, symbol, name and series
          </caption>
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th scope="col" className="py-2 pr-3 text-left font-medium">
                Listed on
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium">
                Symbol
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium">
                Name
              </th>
              <th scope="col" className="py-2 pl-3 text-left font-medium">
                Series
              </th>
            </tr>
          </thead>
          <tbody>
            {page.data.map((row) => (
              <tr key={`${row.symbol}-${row.series ?? ""}`} className="border-t border-border">
                <td className="py-1.5 pr-3 tnum text-muted-foreground">
                  {row.listed_on ? formatTradeDate(row.listed_on) : EMPTY_CELL}
                </td>
                <th scope="row" className="px-3 py-1.5 text-left font-medium">
                  <Link
                    href={`/instruments/${row.symbol}`}
                    className="underline-offset-2 hover:underline"
                  >
                    {row.symbol}
                  </Link>
                </th>
                <td className="px-3 py-1.5">{row.name}</td>
                <td className="py-1.5 pl-3 text-muted-foreground">{row.series ?? EMPTY_CELL}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <nav aria-label="Pagination" className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground tnum">
          {page.data.length} rows on this page
        </p>
        {nextHref ? (
          <Link
            href={nextHref}
            rel="next"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Next page
          </Link>
        ) : (
          <p className="text-xs text-muted-foreground">End of the register.</p>
        )}
      </nav>
    </>
  );
}