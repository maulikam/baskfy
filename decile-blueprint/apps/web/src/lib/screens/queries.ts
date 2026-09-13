"use client";

import type {
  ColumnOut,
  FactorOut,
  RankExplanationOut,
  RankHistoryOut,
  RankingPresetOut,
  ScreenDefinition,
  ScreenOut,
  ScreenRunResponse,
  ScreenSelectionOut,
  ScreenSelectionRequest,
  TradingDaysOut,
  UniverseOut,
} from "@baskfy/api-client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { ApiError } from "@/lib/api/errors";

/**
 * The screens surface's data layer.
 *
 * Everything reads through the generated client (docs/02 rule 5: "No hand-written fetch types"),
 * and everything that can fail throws an `ApiError` carrying the RFC 9457 problem document, so
 * `ErrorState` can render the server's own `title`/`detail` and a 402's `upgrade_url` rather than
 * inventing a message.
 *
 * Reference data (`/meta/factors`, `/meta/columns`, `/meta/universes`) is effectively immutable
 * within a deployment — docs/06 makes the factor registry "the single source of truth" and it
 * ships with the build — so it is cached for the session rather than refetched per mount.
 */
const IMMUTABLE = { staleTime: Number.POSITIVE_INFINITY, gcTime: Number.POSITIVE_INFINITY };

export const screenKeys = {
  list: ["screens"] as const,
  one: (publicId: string) => ["screens", publicId] as const,
  preview: (definition: ScreenDefinition, columns: readonly string[]) =>
    ["screens", "preview", definition, columns] as const,
};

/**
 * The screen itself, seeded from the server render.
 *
 * The page is a server component and already has the screen, so this starts with that value and
 * makes no request. What it adds is a *live* copy: `useSaveScreen` writes the saved screen straight
 * into this cache entry, so the editor sees a new `columns` array the moment a save succeeds.
 *
 * The alternative — reading `screen` from the server prop — leaves the editor showing the column
 * set the page was rendered with, because Next's client Router Cache serves the payload it already
 * has when you navigate back from the columns editor. `router.refresh()` does not reliably fix
 * that from the page you are leaving, and a stale table that contradicts a successful save is a
 * worse bug than an extra query key.
 */
export function useScreen(publicId: string, initial: ScreenOut) {
  return useQuery({
    queryKey: screenKeys.one(publicId),
    initialData: initial,
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: async (): Promise<ScreenOut> => {
      const { data, error } = await browserApi().GET("/api/v1/screens/{public_id}", {
        params: { path: { public_id: publicId } },
      });
      if (!data) throw ApiError.from(error, "The screen could not be loaded.");
      return data;
    },
  });
}

export function useFactors() {
  return useQuery({
    queryKey: ["meta", "factors"],
    ...IMMUTABLE,
    queryFn: async (): Promise<FactorOut[]> => {
      const { data, error } = await browserApi().GET("/api/v1/meta/factors");
      if (!data) throw ApiError.from(error, "Could not load the factor registry.");
      return data;
    },
  });
}

export function useColumns() {
  return useQuery({
    queryKey: ["meta", "columns"],
    ...IMMUTABLE,
    queryFn: async (): Promise<ColumnOut[]> => {
      const { data, error } = await browserApi().GET("/api/v1/meta/columns");
      if (!data) throw ApiError.from(error, "Could not load the column list.");
      return data;
    },
  });
}

export function useUniverses() {
  return useQuery({
    queryKey: ["meta", "universes"],
    ...IMMUTABLE,
    queryFn: async (): Promise<UniverseOut[]> => {
      const { data, error } = await browserApi().GET("/api/v1/meta/universes");
      if (!data) throw ApiError.from(error, "Could not load the universes.");
      return data;
    },
  });
}

/**
 * The trading days the historical-date picker may offer — Prompt 9 deliverable 9.
 *
 * docs/01 §2.13: "Site states historical data is available from 1 Nov 2024", and `/meta/status`
 * publishes both ends of the servable range, so the picker never offers a date the run endpoint
 * would answer 422 for.
 */
export function useTradingDays(from: string | null, to: string | null) {
  return useQuery({
    queryKey: ["meta", "trading-days", from, to],
    enabled: from !== null && to !== null,
    staleTime: 60 * 60_000,
    queryFn: async (): Promise<TradingDaysOut> => {
      const { data, error } = await browserApi().GET("/api/v1/meta/trading-days", {
        params: { query: { from: from as string, to: to as string } },
      });
      if (!data) throw ApiError.from(error, "Could not load the trading calendar.");
      return data;
    },
  });
}

export interface PreviewInput {
  definition: ScreenDefinition;
  columns: readonly string[];
  enabled: boolean;
}

/**
 * docs/08 §"Screen editor": "Debounced live preview (400 ms) hitting `POST /screens/preview`".
 *
 * The debounce lives in the caller (`useDebounced`), so the query key changes only after the user
 * pauses; TanStack Query then dedupes and caches by that key, which means going back to a
 * configuration you have already previewed is instant and costs no request.
 *
 * `placeholderData: keepPreviousData` is what docs/08 §"Results panel" asks for — "Loading:
 * skeleton rows, never a spinner over stale data. Stale-while-revalidate with a subtle 'updating'
 * pill."
 */
export function usePreview({ definition, columns, enabled }: PreviewInput) {
  return useQuery({
    queryKey: screenKeys.preview(definition, columns),
    enabled,
    placeholderData: (previous) => previous,
    queryFn: async (): Promise<ScreenRunResponse> => {
      const { data, error } = await browserApi().POST("/api/v1/screens/preview", {
        body: { definition, columns: [...columns] },
      });
      if (!data) throw ApiError.from(error, "The screen could not be run.");
      return data;
    },
  });
}

export interface SavePayload {
  publicId: string;
  name?: string;
  definition?: ScreenDefinition;
  columns?: readonly string[];
}

/** docs/08: the explicit "Update & Apply Filters" button persists via PATCH. */
export function useSaveScreen() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (payload: SavePayload): Promise<ScreenOut> => {
      const { publicId, ...rest } = payload;
      const { data, error } = await browserApi().PATCH("/api/v1/screens/{public_id}", {
        params: { path: { public_id: publicId } },
        body: {
          ...(rest.name === undefined ? {} : { name: rest.name }),
          ...(rest.definition === undefined ? {} : { definition: rest.definition }),
          ...(rest.columns === undefined ? {} : { columns: [...rest.columns] }),
        },
      });
      if (!data) throw ApiError.from(error, "The screen could not be saved.");
      return data;
    },
    onSuccess: (screen) => {
      client.setQueryData(screenKeys.one(screen.public_id), screen);
      void client.invalidateQueries({ queryKey: screenKeys.list });
    },
  });
}

export function useDuplicateScreen() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (publicId: string): Promise<ScreenOut> => {
      const { data, error } = await browserApi().POST("/api/v1/screens/{public_id}/duplicate", {
        params: { path: { public_id: publicId } },
        body: {},
      });
      if (!data) throw ApiError.from(error, "The screen could not be duplicated.");
      return data;
    },
    onSuccess: () => client.invalidateQueries({ queryKey: screenKeys.list }),
  });
}

export function useDeleteScreen() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (publicId: string): Promise<void> => {
      const { error } = await browserApi().DELETE("/api/v1/screens/{public_id}", {
        params: { path: { public_id: publicId } },
      });
      if (error) throw ApiError.from(error, "The screen could not be deleted.");
    },
    onSuccess: () => client.invalidateQueries({ queryKey: screenKeys.list }),
  });
}

export function useCreateScreen() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      name: string;
      definition: ScreenDefinition;
      columns?: readonly string[];
    }): Promise<ScreenOut> => {
      const { data, error } = await browserApi().POST("/api/v1/screens", {
        body: {
          name: payload.name,
          definition: payload.definition,
          ...(payload.columns === undefined ? {} : { columns: [...payload.columns] }),
        },
      });
      if (!data) throw ApiError.from(error, "The screen could not be created.");
      return data;
    },
    onSuccess: () => client.invalidateQueries({ queryKey: screenKeys.list }),
  });
}

/**
 * docs/07: this stock's rank over time in a screen — read from ``screen_run``, not recomputed.
 *
 * Only enabled for a saved screen (``screenPublicId``) and an open peek symbol. Preview-only
 * runs have no audit trail to chart.
 */
export function useRankHistory(symbol: string | null, screenPublicId: string | undefined) {
  return useQuery({
    queryKey: ["instruments", symbol, "rank-history", screenPublicId] as const,
    enabled: Boolean(symbol && screenPublicId),
    staleTime: 60_000,
    queryFn: async (): Promise<RankHistoryOut> => {
      const { data, error } = await browserApi().GET(
        "/api/v1/instruments/{symbol}/rank-history",
        {
          params: {
            path: { symbol: symbol as string },
            query: { screen: screenPublicId as string, limit: 60 },
          },
        },
      );
      if (!data) throw ApiError.from(error, "Rank history could not be loaded.");
      return data;
    },
  });
}

export interface RankExplanationInput {
  definition: ScreenDefinition;
  symbol: string | null;
  /** The run's own date and data version, so the explanation describes the rows on screen. */
  asOf?: string | undefined;
  dataVersion?: number | undefined;
}

/** A ranked screen is one with ranking terms; `/screens/explain` refuses any other definition. */
export function hasRankingTerms(definition: ScreenDefinition | undefined): boolean {
  return (definition?.ranking_terms.length ?? 0) > 0;
}

/**
 * docs/ranking/PLAN.md C6: `POST /screens/explain` — why one stock ranks where it does.
 *
 * Only enabled for a definition with `ranking_terms` and an open peek symbol: the API refuses a
 * definition without terms, because docs/06's SQL ranking has no term scores to explain. The
 * run's `as_of` and `data_version` are passed through so the drawer and the table read the same
 * run (a newer data version is a 409, not a silently different answer).
 */
export function useRankExplanation({ definition, symbol, asOf, dataVersion }: RankExplanationInput) {
  return useQuery({
    queryKey: ["screens", "explain", definition, symbol, asOf, dataVersion] as const,
    enabled: Boolean(symbol) && hasRankingTerms(definition),
    staleTime: 60_000,
    queryFn: async (): Promise<RankExplanationOut> => {
      const { data, error } = await browserApi().POST("/api/v1/screens/explain", {
        body: {
          definition,
          symbol: symbol as string,
          ...(asOf === undefined ? {} : { as_of: asOf }),
          ...(dataVersion === undefined ? {} : { data_version: dataVersion }),
        },
      });
      if (!data) throw ApiError.from(error, "The ranking explanation could not be loaded.");
      return data;
    },
  });
}

/**
 * docs/ranking/PLAN.md §1.5 / C6: `GET /meta/ranking-presets` — the named presets with their
 * promotion status. Reference data served straight from core, so it is cached like the registry.
 */
export function useRankingPresets() {
  return useQuery({
    queryKey: ["meta", "ranking-presets"],
    ...IMMUTABLE,
    queryFn: async (): Promise<RankingPresetOut[]> => {
      const { data, error } = await browserApi().GET("/api/v1/meta/ranking-presets");
      if (!data) throw ApiError.from(error, "Could not load the ranking presets.");
      return data;
    },
  });
}

/**
 * docs/ranking/PLAN.md C5/C6: `POST /screens/selection` — how a portfolio fits a ranked screen.
 *
 * **Informational only.** A mutation only because it is a POST the user asks for with a button;
 * it writes nothing, returns no plan id, and no route accepts its answer. It never touches the
 * screen's own results, so the quality ranking on the page is not reordered by it.
 */
export function usePortfolioFit() {
  return useMutation({
    mutationFn: async (body: ScreenSelectionRequest): Promise<ScreenSelectionOut> => {
      const { data, error } = await browserApi().POST("/api/v1/screens/selection", { body });
      if (!data) throw ApiError.from(error, "Portfolio fit could not be checked.");
      return data;
    },
  });
}
