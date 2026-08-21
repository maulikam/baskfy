"use client";

import type {
  PortfolioOut,
  PortfolioSummaryOut,
  PortfolioWriteOut,
  RebalanceHistoryPage,
  RebalanceOut,
  ScreenOut,
} from "@decile/api-client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { accessToken, browserApi } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { ApiError } from "@/lib/api/errors";

/**
 * The rebalance tracker's data layer — docs/07 §"Portfolios & rebalance".
 *
 * docs/08 §Routes marks `/portfolios` and `/portfolios/[id]/rebalance` as **client** surfaces, so
 * everything here runs in the browser through the generated client (docs/02 rule 5: "No
 * hand-written fetch types"). Failures throw an `ApiError` carrying the RFC 9457 document, which
 * is what lets `ErrorState` render the server's own words.
 *
 * The one exception is the CSV upload: `openapi-fetch` posts JSON, and a multipart body needs a
 * `FormData` the way the browser builds it. `uploadCsv` therefore uses `fetch` directly — with
 * the same bearer token and the same error shape — and is the only hand-written request in this
 * app. It sends no body types of its own; the *response* is still the generated
 * `PortfolioWriteOut`.
 */

export const portfolioKeys = {
  list: ["portfolios"] as const,
  one: (id: number) => ["portfolios", id] as const,
  history: (id: number) => ["portfolios", id, "rebalances"] as const,
};

export function usePortfolios(initial?: PortfolioSummaryOut[]) {
  return useQuery({
    queryKey: portfolioKeys.list,
    ...(initial ? { initialData: initial } : {}),
    queryFn: async (): Promise<PortfolioSummaryOut[]> => {
      const { data, error } = await browserApi().GET("/api/v1/portfolios");
      if (!data) throw ApiError.from(error, "Your portfolios could not be loaded.");
      return data.data;
    },
  });
}

export function usePortfolio(id: number | null, initial?: PortfolioOut) {
  return useQuery({
    queryKey: portfolioKeys.one(id ?? 0),
    enabled: id !== null,
    ...(initial ? { initialData: initial } : {}),
    queryFn: async (): Promise<PortfolioOut> => {
      const { data, error } = await browserApi().GET("/api/v1/portfolios/{portfolio_id}", {
        params: { path: { portfolio_id: id ?? 0 } },
      });
      if (!data) throw ApiError.from(error, "That portfolio could not be loaded.");
      return data;
    },
  });
}

export function useScreens() {
  return useQuery({
    queryKey: ["screens"] as const,
    queryFn: async (): Promise<ScreenOut[]> => {
      const { data, error } = await browserApi().GET("/api/v1/screens");
      if (!data) throw ApiError.from(error, "The screen list could not be loaded.");
      return data.data;
    },
  });
}

export interface CreatePortfolioInput {
  name: string;
  holdings: { symbol: string; quantity?: string | null; avg_price?: string | null }[];
}

export function useCreatePortfolio() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreatePortfolioInput): Promise<PortfolioWriteOut> => {
      const { data, error } = await browserApi().POST("/api/v1/portfolios", { body: input });
      if (!data) throw ApiError.from(error, "The portfolio could not be created.");
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: portfolioKeys.list }),
  });
}

export function useRenamePortfolio() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: { id: number; name: string }): Promise<PortfolioOut> => {
      const { data, error } = await browserApi().PATCH("/api/v1/portfolios/{portfolio_id}", {
        params: { path: { portfolio_id: input.id } },
        body: { name: input.name },
      });
      if (!data) throw ApiError.from(error, "The portfolio could not be renamed.");
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: portfolioKeys.list }),
  });
}

export function useDeletePortfolio() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (id: number): Promise<void> => {
      const { error } = await browserApi().DELETE("/api/v1/portfolios/{portfolio_id}", {
        params: { path: { portfolio_id: id } },
      });
      if (error) throw ApiError.from(error, "The portfolio could not be deleted.");
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: portfolioKeys.list }),
  });
}

export interface UploadInput {
  file: File;
  /** Omit to create a new portfolio; supply one to replace an existing portfolio's holdings. */
  portfolioId?: number;
  name?: string;
}

export function useUploadCsv() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: UploadInput) => uploadCsv(input),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: portfolioKeys.list });
      void client.invalidateQueries({ queryKey: portfolioKeys.one(result.portfolio.id) });
    },
  });
}

/** The one multipart request in the app — see the module docstring. */
async function uploadCsv(input: UploadInput): Promise<PortfolioWriteOut> {
  const body = new FormData();
  body.append("file", input.file);

  const params = new URLSearchParams();
  if (input.portfolioId !== undefined) params.set("portfolio_id", String(input.portfolioId));
  if (input.name) params.set("name", input.name);
  const query = params.size > 0 ? `?${params.toString()}` : "";

  const token = await accessToken();
  const response = await fetch(`${apiOrigin()}/api/v1/portfolios/import-csv${query}`, {
    method: "POST",
    body,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw ApiError.from(payload, "The file could not be imported.", response.status);
  }
  return payload as PortfolioWriteOut;
}

export interface RebalanceInput {
  portfolioId: number;
  screenPublicId: string;
  topN: number;
  holdBuffer: number;
  asOf?: string | null;
}

export function useRebalance() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: RebalanceInput): Promise<RebalanceOut> => {
      const { data, error } = await browserApi().POST(
        "/api/v1/portfolios/{portfolio_id}/rebalance",
        {
          params: { path: { portfolio_id: input.portfolioId } },
          body: {
            screen_public_id: input.screenPublicId,
            top_n: input.topN,
            hold_buffer: input.holdBuffer,
            as_of: input.asOf ?? null,
          },
        },
      );
      if (!data) throw ApiError.from(error, "The rebalance could not be computed.");
      return data;
    },
    onSuccess: (_result, input) =>
      void client.invalidateQueries({ queryKey: portfolioKeys.history(input.portfolioId) }),
  });
}

export function useRebalanceHistory(id: number | null) {
  return useQuery({
    queryKey: portfolioKeys.history(id ?? 0),
    enabled: id !== null,
    queryFn: async (): Promise<RebalanceHistoryPage> => {
      const { data, error } = await browserApi().GET(
        "/api/v1/portfolios/{portfolio_id}/rebalances",
        { params: { path: { portfolio_id: id ?? 0 } } },
      );
      if (!data) throw ApiError.from(error, "The rebalance history could not be loaded.");
      return data;
    },
  });
}
