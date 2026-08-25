"use client";

import type {
  AllocationOut,
  PortfolioDetailOut,
  PortfolioForestOut,
  PortfolioPatchIn,
  PortfolioRollupOut,
  PortfolioWriteDetailOut,
  RebalanceHistoryPage,
  RebalanceOut,
  ScreenOut,
  SleeveIn,
  SleeveListOut,
} from "@baskfy/api-client";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import type { BrokerListOut } from "@/lib/portfolios/rollup";
import type { SyncHoldingsOut } from "@/lib/portfolios/provenance";

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
 * `PortfolioWriteDetailOut`.
 *
 * ## Migration 0019 renamed the response models, and the rename is load-bearing
 *
 * `GET /portfolios` no longer answers with a flat list. It answers with a `PortfolioForestOut`:
 * `data` holds the **roots**, each carrying its `children`, plus an `orphans` array for fragments
 * whose parent is not the caller's. No back-compat alias was added for the old names, deliberately
 * — an alias would let a caller keep reading a tree as a flat list and never find out that half
 * the portfolios are missing from the page.
 */

export const portfolioKeys = {
  list: ["portfolios"] as const,
  one: (id: number) => ["portfolios", id] as const,
  history: (id: number) => ["portfolios", id, "rebalances"] as const,
  sleeves: (id: number) => ["portfolios", id, "sleeves"] as const,
  rollup: (id: number) => ["portfolios", id, "rollup"] as const,
  allocation: (id: number, applyCap: boolean) =>
    ["portfolios", id, "allocation", applyCap] as const,
  brokers: ["brokers", "catalog"] as const,
};

export function usePortfolios(initial?: PortfolioForestOut) {
  return useQuery({
    queryKey: portfolioKeys.list,
    ...(initial ? { initialData: initial } : {}),
    queryFn: async (): Promise<PortfolioForestOut> => {
      const { data, error } = await browserApi().GET("/api/v1/portfolios");
      if (!data) throw ApiError.from(error, "Your portfolios could not be loaded.");
      return data;
    },
  });
}

/**
 * A subtree's holdings split per broker account — `GET /portfolios/{id}/holdings`.
 *
 * Read-only by construction: the endpoint reaches no broker session, marks nothing to market and
 * places nothing. Its money arrives as exact unrounded decimal **strings**; nothing here parses
 * one into a `number`.
 */
export function usePortfolioRollup(id: number | null, initial?: PortfolioRollupOut) {
  return useQuery({
    queryKey: portfolioKeys.rollup(id ?? 0),
    enabled: id !== null,
    ...(initial ? { initialData: initial } : {}),
    queryFn: async (): Promise<PortfolioRollupOut> => {
      const { data, error } = await browserApi().GET(
        "/api/v1/portfolios/{portfolio_id}/holdings",
        { params: { path: { portfolio_id: id ?? 0 } } },
      );
      if (!data) throw ApiError.from(error, "The broker split could not be loaded.");
      return data;
    },
  });
}

/**
 * Roll-ups for several portfolios at once — the containers that declare no broker account.
 *
 * Only those: a node that names an account already knows its own answer, and asking the server
 * for every node in the forest would be a request per row to learn nothing new.
 */
export function usePortfolioRollups(ids: number[]) {
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: portfolioKeys.rollup(id),
      queryFn: async (): Promise<PortfolioRollupOut> => {
        const { data, error } = await browserApi().GET(
          "/api/v1/portfolios/{portfolio_id}/holdings",
          { params: { path: { portfolio_id: id } } },
        );
        if (!data) throw ApiError.from(error, "The broker split could not be loaded.");
        return data;
      },
      staleTime: 15_000,
    })),
  });
}

/**
 * The broker catalog, for saying which brokers can put holdings into a total and which cannot.
 *
 * A consolidated figure that omits nine brokers without saying so is a wrong number wearing a
 * right label; this is where the page learns which nine.
 */
export function useBrokerCatalog(initial?: BrokerListOut) {
  return useQuery({
    queryKey: portfolioKeys.brokers,
    ...(initial ? { initialData: initial } : {}),
    queryFn: async (): Promise<BrokerListOut> => {
      const { data, error } = await browserApi().GET("/api/v1/brokers");
      if (!data) throw ApiError.from(error, "The broker list could not be loaded.");
      return data;
    },
    staleTime: 60_000,
  });
}

/**
 * Read what a broker reports, and keep the label the server put on it.
 *
 * This is a **read**. `sync-holdings` fetches rows and returns them with their provenance; it has
 * no order path, and nothing in this app has one (desk non-negotiable #1).
 */
export function useSyncHoldings() {
  return useMutation({
    mutationFn: async (brokerId: string): Promise<SyncHoldingsOut> => {
      const { data, error } = await browserApi().POST(
        "/api/v1/brokers/{broker_id}/sync-holdings",
        { params: { path: { broker_id: brokerId } } },
      );
      if (!data) throw ApiError.from(error, "That broker could not be read.");
      return data;
    },
  });
}

export function usePortfolio(id: number | null, initial?: PortfolioDetailOut) {
  return useQuery({
    queryKey: portfolioKeys.one(id ?? 0),
    enabled: id !== null,
    ...(initial ? { initialData: initial } : {}),
    queryFn: async (): Promise<PortfolioDetailOut> => {
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
  /** Omit for a root. Supplying one files the new portfolio under an existing portfolio. */
  parentId?: number;
  brokerAccountId?: number;
}

export function useCreatePortfolio() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreatePortfolioInput): Promise<PortfolioWriteDetailOut> => {
      const { data, error } = await browserApi().POST("/api/v1/portfolios", {
        body: {
          name: input.name,
          holdings: input.holdings,
          ...(input.parentId === undefined ? {} : { parent_id: input.parentId }),
          ...(input.brokerAccountId === undefined
            ? {}
            : { broker_account_id: input.brokerAccountId }),
        },
      });
      if (!data) throw ApiError.from(error, "The portfolio could not be created.");
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: portfolioKeys.list }),
  });
}

export interface PortfolioPatchInput {
  name?: string;
  /**
   * **Absent and `null` are different requests.** Omitting `parentId` leaves the parent alone;
   * passing `null` promotes the portfolio to a root. The server tells them apart with
   * `model_fields_set`, so the body this builds must actually omit the key rather than send a
   * `null` that means "unchanged" — those are two different moves and one of them is destructive.
   */
  parentId?: number | null;
  brokerAccountId?: number | null;
}

/** Exported so a test can prove the two requests differ, which no round-trip assertion can. */
export function buildPatchBody(input: PortfolioPatchInput): PortfolioPatchIn {
  const body: PortfolioPatchIn = {};
  if (input.name !== undefined) body.name = input.name;
  if ("parentId" in input && input.parentId !== undefined) body.parent_id = input.parentId;
  if ("brokerAccountId" in input && input.brokerAccountId !== undefined) {
    body.broker_account_id = input.brokerAccountId;
  }
  return body;
}

/**
 * Rename, move, or re-attribute — `PATCH /portfolios/{id}`.
 *
 * Re-parenting can fail for reasons that are not the caller's fault to guess at (a cycle, the
 * depth cap, a parent that is not theirs), and the server's own words come back through
 * `ApiError` rather than being replaced by a cheerful generic sentence.
 */
export function usePatchPortfolio() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (
      input: PortfolioPatchInput & { id: number },
    ): Promise<PortfolioDetailOut> => {
      const { id, ...patch } = input;
      const { data, error } = await browserApi().PATCH("/api/v1/portfolios/{portfolio_id}", {
        params: { path: { portfolio_id: id } },
        body: buildPatchBody(patch),
      });
      if (!data) throw ApiError.from(error, "That portfolio could not be updated.");
      return data;
    },
    onSuccess: (_result, input) => {
      void client.invalidateQueries({ queryKey: portfolioKeys.list });
      void client.invalidateQueries({ queryKey: portfolioKeys.rollup(input.id) });
    },
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
async function uploadCsv(input: UploadInput): Promise<PortfolioWriteDetailOut> {
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
  return payload as PortfolioWriteDetailOut;
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

/*
 * Sleeves — M34. A portfolio run as several screens plus a slice run by hand.
 *
 * The allocation is a *derived* read, not stored: a sleeve is a standing instruction ("this much,
 * from this screen"), so saving the sleeves invalidates the allocation and it is computed again
 * from the screens' current output. `applyCap` is part of the key because the two answers are
 * genuinely different documents, and caching them together would show the capped figures to
 * someone who never ticked the box.
 */

export function useSleeveMap(ids: number[], initial?: Record<number, SleeveListOut>) {
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: portfolioKeys.sleeves(id),
      queryFn: async (): Promise<SleeveListOut> => {
        const { data, error } = await browserApi().GET("/api/v1/portfolios/{portfolio_id}/sleeves", {
          params: { path: { portfolio_id: id } },
        });
        if (!data) throw ApiError.from(error, "The sleeves could not be loaded.");
        return data;
      },
      ...(initial?.[id] ? { initialData: initial[id] } : {}),
      // The overview asks once per named book. Default staleTime 0 would refetch every sleeve on
      // mount — a dozen extra round-trips for a book that just came from the server.
      staleTime: 15_000,
    })),
  });
}

export function useSleeves(id: number, initial?: SleeveListOut) {
  return useQuery({
    queryKey: portfolioKeys.sleeves(id),
    queryFn: async (): Promise<SleeveListOut> => {
      const { data, error } = await browserApi().GET("/api/v1/portfolios/{portfolio_id}/sleeves", {
        params: { path: { portfolio_id: id } },
      });
      if (!data) throw ApiError.from(error, "The sleeves could not be loaded.");
      return data;
    },
    ...(initial ? { initialData: initial } : {}),
  });
}

export function useAllocation(id: number, applyCap: boolean) {
  return useQuery({
    queryKey: portfolioKeys.allocation(id, applyCap),
    queryFn: async (): Promise<AllocationOut> => {
      const { data, error } = await browserApi().GET(
        "/api/v1/portfolios/{portfolio_id}/allocation",
        { params: { path: { portfolio_id: id }, query: { apply_regime_cap: applyCap } } },
      );
      if (!data) throw ApiError.from(error, "The allocation could not be computed.");
      return data;
    },
  });
}

export function useSaveSleeves(id: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (sleeves: SleeveIn[]): Promise<SleeveListOut> => {
      const { data, error } = await browserApi().PUT("/api/v1/portfolios/{portfolio_id}/sleeves", {
        params: { path: { portfolio_id: id } },
        body: { sleeves },
      });
      if (!data) throw ApiError.from(error, "The sleeves could not be saved.");
      return data;
    },
    onSuccess: async (data) => {
      client.setQueryData(portfolioKeys.sleeves(id), data);
      // Both answers are stale now, capped and uncapped alike.
      await client.invalidateQueries({ queryKey: ["portfolios", id, "allocation"] });
    },
  });
}
