/**
 * The wire shapes `/home` renders — SC9.
 *
 * Split from `fetch.ts` because that module is `server-only`: a component that needs the *shape*
 * of a trending list must not have to drag a server module (and next-auth behind it) into a
 * browser bundle or a jsdom test to get it. Types here, I/O there.
 *
 * `Collection` is not redefined: it belongs to `lib/collections`, which owns that endpoint.
 */

/** One row of a ranked list. `metric_display` is rounded by the API — never re-round it. */
export interface TrendingEntry {
  rank: number;
  basket_slug: string;
  basket_name: string;
  metric_value: string | number | null;
  metric_date: string | null;
  metric_display: string;
}

export interface TrendingList {
  key: string;
  title: string;
  /** The sentence saying what this list actually ranks. Rendered, never dropped. */
  ranks_by: string;
  metric_label: string;
  metric_kind: string;
  population_based: boolean;
  population: number | null;
  eligible: number;
  entries: TrendingEntry[];
  withheld_reason: string | null;
  withheld_note: string | null;
  price_return_caveat: boolean;
}

export interface Trending {
  items: TrendingList[];
  count: number;
  catalog_size: number;
  min_entries: number;
  min_population: number;
  return_convention: string;
  dividends_included: boolean;
  return_convention_note: string;
}

export interface UpdatePost {
  id: string;
  title: string;
  body_md: string;
  published_at: string;
  source: string;
}

export const EMPTY_TRENDING: Trending = {
  items: [],
  count: 0,
  catalog_size: 0,
  min_entries: 0,
  min_population: 0,
  return_convention: "",
  dividends_included: false,
  return_convention_note: "",
};
