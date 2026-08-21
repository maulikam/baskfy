"use client";

import type {
  ColumnOut,
  FactorOut,
  ScreenDefinition,
  ScreenOut,
  ScreenRunResponse,
  TradingDaysOut,
  UniverseOut,
} from "@decile/api-client";
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
