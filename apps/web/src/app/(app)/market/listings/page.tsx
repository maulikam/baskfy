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
 *
 * AFH 5.8: Previous is a URL back-stack (`prev` = the cursor that opened this page). The API's
 * `total` is the filtered register size so the footer can say "N of M".
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

function listingsHref(parts: {
  cursor?: string | undefined;
  prev?: string | undefined;
  series: string;
  search: string;
}): Route {
  const query = new URLSearchParams();
  if (parts.series) query.set("series", parts.series);
  if (parts.search) query.set("search", parts.search);
  if (parts.cursor) query.set("cursor", parts.cursor);
  if (parts.prev) query.set("prev", parts.prev);
  const qs = query.toString();
  return qs ? `/market/listings?${qs}` : "/market/listings";
}

export default async function ListingsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const cursor = first(params.cursor);
  const prev = first(params.prev);
  const series = first(params.series) ?? "";
  const search = first(params.search) ?? "";

  const page = await fetchListings({ cursor, series, search });

  const nextHref = page.next_cursor
    ? listingsHref({
        cursor: page.next_cursor,
        /* Remember how we got here so Previous can return to this page's cursor (or none). */
        prev: cursor ?? "",
        series,
        search,
      })
    : null;

  const prevHref =
    prev !== undefined
      ? listingsHref({
          cursor: prev === "" ? undefined : prev,
          series,
          search,
        })
      : null;

  return (
    <>
      <SectionTabs section="market" />
      <PageHeader
        title={PAGES["/market/listings"].title}
        blurb={PAGES["/market/listings"].blurb}
        meta={`Newest listings first. ${PAGE_SIZE} at a time.`}
      />

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
        <p className="text-xs text-muted-foreground tnum" data-testid="listings-page-count">
          {page.data.length} on this page · {page.total ?? 0} matching
          {page.next_cursor ? " · more ahead" : " · end of register"}
        </p>
        <div className="flex items-center gap-2">
          {prevHref ? (
            <Link
              href={prevHref}
              rel="prev"
              data-testid="listings-prev"
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Previous
            </Link>
          ) : null}
          {nextHref ? (
            <Link
              href={nextHref}
              rel="next"
              data-testid="listings-next"
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
            >
              Next page
            </Link>
          ) : (
            <p className="text-xs text-muted-foreground">End of the register.</p>
          )}
        </div>
      </nav>
    </>
  );
}
