import "server-only";

import { cache } from "react";

import { ExploreNotFound, ExploreUnavailable, readExploreJson } from "@/lib/explore/fetch";

export interface BasketVersionSummary {
  version_no: number;
  effective_date: string;
  label: string;
  added_count: number;
  removed_count: number;
  notes_md: string | null;
  constituent_count: number;
}

export interface BasketVersions {
  slug: string;
  versions: BasketVersionSummary[];
  count: number;
}

export interface VersionDiffLine {
  symbol: string;
  name: string | null;
  change: string;
  weight_from: string | null;
  weight_to: string | null;
  weight_pct_from: string | null;
  weight_pct_to: string | null;
}

export interface VersionDiff {
  slug: string;
  from_version: number;
  to_version: number;
  from_effective_date: string;
  to_effective_date: string;
  added: VersionDiffLine[];
  removed: VersionDiffLine[];
  weight_changed: VersionDiffLine[];
  unchanged_count: number;
}

export const fetchBasketVersions = cache(async function fetchBasketVersions(
  slug: string,
): Promise<BasketVersions> {
  try {
    return (await readExploreJson(
      `/explore/${encodeURIComponent(slug)}/versions`,
    )) as BasketVersions;
  } catch (error) {
    if (error instanceof ExploreNotFound || error instanceof ExploreUnavailable) throw error;
    throw new ExploreUnavailable(
      error instanceof Error ? error.message : "versions unavailable",
    );
  }
});
