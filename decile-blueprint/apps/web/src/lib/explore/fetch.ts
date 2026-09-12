import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { ServerFetchTimeoutError, serverFetchJson } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

/**
 * Server-side reads for `/explore` and `/basket/[slug]` — SC5.
 *
 * Catalog data comes from `GET /api/v1/explore` (SC2). There is no write helper here: Invest
 * CTAs hand off to `PlanHandoffPanel` or the desk console, never an order-placing endpoint.
 */

export class ExploreUnavailable extends Error {}

export interface ExploreManagerBrief {
  slug: string;
  name: string;
  kind: string;
}

export interface ExploreMetrics {
  as_of_date: string | null;
  min_amount: string | number | null;
  volatility_bucket: string | null;
  volatility_value: string | number | null;
  ret_1m: string | number | null;
  ret_6m: string | number | null;
  ret_1y: string | number | null;
  cagr_3y: string | number | null;
  cagr_5y: string | number | null;
  since_inception_pct: string | number | null;
  headline_label: string | null;
  headline_pct: string | number | null;
  /**
   * Every figure above is a PRICE return. `ohlcv_daily.close` carries splits and bonuses but
   * not cash dividends (docs/DECISIONS-MERGE.md M39.3), so a total-return series would be
   * higher by roughly the dividend yield — about 1.2% a year on NSE, compounding.
   * CLAUDE.md: "any new surface that shows a return owes the reader the same sentence."
   */
  return_convention: string;
  dividends_included: boolean;
  return_convention_note: string;
}

export interface ExploreBasketCard {
  slug: string;
  name: string;
  access: string;
  visibility: string;
  type: string;
  categories: string[];
  rebalance_frequency: string;
  source: string;
  description_md: string | null;
  launched_at: string | null;
  manager: ExploreManagerBrief;
  metrics: ExploreMetrics | null;
}

export interface ExploreList {
  items: ExploreBasketCard[];
  total: number;
}

/** Query params documented in `DOCUMENTED_LIST_PARAMS` / DECISIONS-SC SC2. */
export interface ExploreListParams {
  max_min_amount?: string;
  access?: string;
  volatility?: string;
  category?: string;
  rebalance_frequency?: string;
  basket_type?: string;
  include_new?: string;
  sort?: string;
  order?: string;
  q?: string;
}

/** Drop keys whose values are undefined — required under `exactOptionalPropertyTypes`. */
export function definedParams<T extends object>(
  input: T,
): { [K in keyof T]?: Exclude<T[K], undefined> } {
  const out = {} as { [K in keyof T]?: Exclude<T[K], undefined> };
  for (const key of Object.keys(input) as (keyof T)[]) {
    const value = input[key];
    if (value !== undefined && value !== "") {
      out[key] = value as Exclude<T[typeof key], undefined>;
    }
  }
  return out;
}

/**
 * Read one JSON document from the API with the caller's bearer token and the shared
 * timeout handling. Exported so `lib/collections` can reuse it: a second copy of the auth
 * and timeout logic is a second place for it to drift, and the collections surface reaches
 * the same `/explore/*` namespace anyway.
 */
export async function readExploreJson(path: string): Promise<unknown> {
  const session = await auth();
  const token = session?.accessToken;
  try {
    return await serverFetchJson({
      url: `${serverApiOrigin()}/api/v1${path}`,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
  } catch (error) {
    if (error instanceof ServerFetchTimeoutError) {
      throw new ExploreUnavailable(`${path} timed out after ${error.timeoutMs}ms`);
    }
    throw new ExploreUnavailable(
      error instanceof Error ? error.message : `${path} unavailable`,
    );
  }
}

function toQuery(params: ExploreListParams): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params) as [string, string | undefined][]) {
    if (value === undefined || value === "") continue;
    query.set(key, value);
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export async function fetchExploreList(params: ExploreListParams = {}): Promise<ExploreList> {
  return (await readExploreJson(`/explore${toQuery(params)}`)) as ExploreList;
}

export async function fetchExploreBasket(slug: string): Promise<ExploreBasketCard> {
  return (await readExploreJson(`/explore/${encodeURIComponent(slug)}`)) as ExploreBasketCard;
}

export interface ExploreConstituent {
  symbol: string;
  name: string | null;
  segment: string;
  /** Fraction of the basket as stored (`0.0500`). Prefer `weight_pct` for display. */
  weight: string;
  /** Percent of the basket for display (`5.00`), already rounded by the API (house rule 8). */
  weight_pct: string;
}

export interface ExploreConstituents {
  slug: string;
  version_no: number;
  effective_date: string;
  label: string;
  added_count: number;
  removed_count: number;
  /** How many versions this basket has published. */
  version_count: number;
  constituents: ExploreConstituent[];
}

export async function fetchExploreConstituents(slug: string): Promise<ExploreConstituents> {
  return (await readExploreJson(
    `/explore/${encodeURIComponent(slug)}/constituents`,
  )) as ExploreConstituents;
}

export interface ExploreManager {
  slug: string;
  name: string;
  kind: string;
  sebi_reg_no: string | null;
  bio: string | null;
  strategies: string[];
  disclosures_md: string | null;
}

export async function fetchExploreManager(slug: string): Promise<ExploreManager> {
  return (await readExploreJson(`/explore/managers/${encodeURIComponent(slug)}`)) as ExploreManager;
}
