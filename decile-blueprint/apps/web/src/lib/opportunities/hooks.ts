"use client";

import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import {
  OpportunitiesError,
  analyzeOptions,
  fetchOpportunitiesMeta,
  isAbort,
  scanSwing,
  streamSwingScan,
} from "@/lib/opportunities/client";
import { createLatestGuard } from "@/lib/opportunities/latest";
import type {
  OptionsAnalysis,
  OptionsAnalyzeRequest,
  ProblemBody,
  ScanProgress,
  SwingScanRequest,
  SwingScanResponse,
} from "@/lib/opportunities/types";

export const META_QUERY_KEY = ["opportunities", "meta"] as const;

export function useOpportunitiesMeta() {
  return useQuery({
    queryKey: META_QUERY_KEY,
    queryFn: ({ signal }) => fetchOpportunitiesMeta(signal),
    staleTime: 5 * 60_000,
    retry: 1,
  });
}

export function toProblem(error: unknown): ProblemBody {
  if (error instanceof OpportunitiesError) {
    return { title: error.title, detail: error.detail, status: error.status };
  }
  return {
    title: "Could not reach the server",
    detail: error instanceof Error ? error.message : "Network error",
    status: 0,
  };
}

export type RunPhase = "idle" | "running" | "done" | "cancelled" | "error";

export interface SwingScanState {
  phase: RunPhase;
  progress: ScanProgress | null;
  result: SwingScanResponse | null;
  error: ProblemBody | null;
}

const IDLE_SCAN: SwingScanState = { phase: "idle", progress: null, result: null, error: null };

export function useSwingScan() {
  const [guard] = useState(createLatestGuard);
  const [state, setState] = useState<SwingScanState>(IDLE_SCAN);

  useEffect(() => () => guard.cancel(), [guard]);

  const run = useCallback(
    async (body: SwingScanRequest) => {
      const ticket = guard.begin();
      setState((prev) => ({ ...prev, phase: "running", progress: null, error: null }));
      try {
        let result: SwingScanResponse;
        try {
          result = await streamSwingScan(body, {
            signal: ticket.signal,
            onProgress: (progress) => {
              if (guard.isCurrent(ticket.id)) setState((prev) => ({ ...prev, progress }));
            },
          });
        } catch (error) {
          if (error instanceof OpportunitiesError && (error.status === 404 || error.status === 405)) {
            result = await scanSwing(body, ticket.signal);
          } else {
            throw error;
          }
        }
        if (!guard.isCurrent(ticket.id)) return;
        setState({ phase: "done", progress: null, result, error: null });
      } catch (error) {
        if (!guard.isCurrent(ticket.id)) return;
        if (isAbort(error)) {
          setState((prev) => ({ ...prev, phase: "cancelled", progress: null }));
          return;
        }
        setState((prev) => ({ ...prev, phase: "error", progress: null, error: toProblem(error) }));
      }
    },
    [guard],
  );

  const cancel = useCallback(() => {
    guard.cancel();
    setState((prev) => ({ ...prev, phase: "cancelled", progress: null }));
  }, [guard]);

  return { state, run, cancel };
}

export interface OptionsState {
  phase: RunPhase;
  result: OptionsAnalysis | null;
  error: ProblemBody | null;
}

export function useOptionsAnalysis() {
  const [guard] = useState(createLatestGuard);
  const [state, setState] = useState<OptionsState>({ phase: "idle", result: null, error: null });

  useEffect(() => () => guard.cancel(), [guard]);

  const run = useCallback(
    async (body: OptionsAnalyzeRequest) => {
      const ticket = guard.begin();
      setState((prev) => ({ ...prev, phase: "running", error: null }));
      try {
        const result = await analyzeOptions(body, ticket.signal);
        if (!guard.isCurrent(ticket.id)) return;
        setState({ phase: "done", result, error: null });
      } catch (error) {
        if (!guard.isCurrent(ticket.id)) return;
        if (isAbort(error)) {
          setState((prev) => ({ ...prev, phase: "cancelled" }));
          return;
        }
        setState((prev) => ({ ...prev, phase: "error", error: toProblem(error) }));
      }
    },
    [guard],
  );

  const cancel = useCallback(() => {
    guard.cancel();
    setState((prev) => ({ ...prev, phase: "cancelled" }));
  }, [guard]);

  return { state, run, cancel };
}
