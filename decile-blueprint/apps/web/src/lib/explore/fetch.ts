import "server-only";

import { apiOrigin } from "@/lib/api/config";

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

async function readJson(path: string): Promise<unknown> {
  const response = await fetch(`${apiOrigin()}/api/v1${path}`, { cache: "no-store" });
  if (!response.ok) throw new ExploreUnavailable(`${path} responded ${response.status}`);
  return response.json();
}

function toQuery(params: ExploreListParams): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    query.set(key, value);
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export async function fetchExploreList(params: ExploreListParams = {}): Promise<ExploreList> {
  return (await readJson(`/explore${toQuery(params)}`)) as ExploreList;
}

export async function fetchExploreBasket(slug: string): Promise<ExploreBasketCard> {
  return (await readJson(`/explore/${encodeURIComponent(slug)}`)) as ExploreBasketCard;
}
