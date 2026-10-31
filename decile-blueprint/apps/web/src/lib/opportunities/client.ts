"use client";

import { API_PREFIX } from "@baskfy/api-client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { parseAguiEvent, type AguiEvent, type RunAgentInput } from "@/lib/opportunities/agui";
import { readSseStream } from "@/lib/opportunities/sse";
import type {
  OpportunitiesMeta,
  OptionsAnalysis,
  OptionsAnalyzeRequest,
  ProblemBody,
  ScanProgress,
  SwingScanRequest,
  SwingScanResponse,
  ValidateRequest,
  ValidateResponse,
} from "@/lib/opportunities/types";

export class OpportunitiesError extends Error {
  readonly status: number;
  readonly title: string;
  readonly detail: string;

  constructor(problem: ProblemBody) {
    super(`${problem.title}: ${problem.detail}`);
    this.name = "OpportunitiesError";
    this.status = problem.status;
    this.title = problem.title;
    this.detail = problem.detail;
  }
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error instanceof Error && error.name === "AbortError";
}

function url(path: string): string {
  return `${apiOrigin()}${API_PREFIX}${path}`;
}

async function headers(extra: Record<string, string> = {}): Promise<Record<string, string>> {
  const token = await accessToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

function str(value: unknown, fallback: string): string {
  return typeof value === "string" && value !== "" ? value : fallback;
}

export function problemFrom(body: unknown, status: number): ProblemBody {
  if (typeof body === "object" && body !== null) {
    const record = body as Record<string, unknown>;
    const detail = record.detail;
    return {
      title: str(record.title, `Request failed (${status})`),
      detail: typeof detail === "string" ? detail : detail === undefined ? "" : JSON.stringify(detail),
      status: typeof record.status === "number" ? record.status : status,
    };
  }
  return { title: `Request failed (${status})`, detail: "", status };
}

async function failure(response: Response): Promise<OpportunitiesError> {
  const body = await response.json().then(
    (value: unknown) => value,
    () => null,
  );
  return new OpportunitiesError(problemFrom(body, response.status));
}

async function requestJson<T>(
  path: string,
  init: { method: "GET" | "POST"; body?: unknown; signal?: AbortSignal },
): Promise<T> {
  const response = await fetch(url(path), {
    method: init.method,
    headers: await headers(init.body === undefined ? {} : { "Content-Type": "application/json" }),
    ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    ...(init.signal ? { signal: init.signal } : {}),
    cache: "no-store",
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

export function fetchOpportunitiesMeta(signal?: AbortSignal): Promise<OpportunitiesMeta> {
  return requestJson<OpportunitiesMeta>("/opportunities/meta", {
    method: "GET",
    ...(signal ? { signal } : {}),
  });
}

export function scanSwing(body: SwingScanRequest, signal?: AbortSignal): Promise<SwingScanResponse> {
  return requestJson<SwingScanResponse>("/opportunities/swing/scan", {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
}

export function validateSwing(body: ValidateRequest, signal?: AbortSignal): Promise<ValidateResponse> {
  return requestJson<ValidateResponse>("/opportunities/swing/validate", {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
}

export function analyzeOptions(
  body: OptionsAnalyzeRequest,
  signal?: AbortSignal,
): Promise<OptionsAnalysis> {
  return requestJson<OptionsAnalysis>("/opportunities/options/analyze", {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
}

async function openStream(path: string, body: unknown, signal: AbortSignal): Promise<ReadableStream<Uint8Array>> {
  const response = await fetch(url(path), {
    method: "POST",
    headers: await headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify(body),
    signal,
    cache: "no-store",
  });
  if (!response.ok) throw await failure(response);
  if (!response.body) {
    throw new OpportunitiesError({ title: "Empty stream", detail: "The server sent no body.", status: response.status });
  }
  return response.body;
}

function parseJson(data: string): unknown {
  try {
    return JSON.parse(data) as unknown;
  } catch (_error) {
    return null;
  }
}

export async function streamSwingScan(
  body: SwingScanRequest,
  options: { signal: AbortSignal; onProgress: (progress: ScanProgress) => void },
): Promise<SwingScanResponse> {
  const stream = await openStream("/opportunities/swing/scan/stream", body, options.signal);
  const box: { result: SwingScanResponse | null; problem: ProblemBody | null } = {
    result: null,
    problem: null,
  };
  await readSseStream(stream, (message) => {
    const data = parseJson(message.data);
    if (message.event === "progress" && data !== null) options.onProgress(data as ScanProgress);
    else if (message.event === "result" && data !== null) box.result = data as SwingScanResponse;
    else if (message.event === "error") box.problem = problemFrom(data, 500);
  });
  if (box.problem) throw new OpportunitiesError(box.problem);
  if (!box.result) {
    throw new OpportunitiesError({
      title: "Scan ended without a result",
      detail: "The stream closed before the server sent its result.",
      status: 502,
    });
  }
  return box.result;
}

export async function runAgui(
  input: RunAgentInput,
  options: { signal: AbortSignal; onEvent: (event: AguiEvent) => void },
): Promise<void> {
  const stream = await openStream("/opportunities/agui", input, options.signal);
  await readSseStream(stream, (message) => {
    const event = parseAguiEvent(message.data);
    if (event) options.onEvent(event);
  });
}

export async function addToWatchlist(symbol: string): Promise<"added" | "exists"> {
  const response = await fetch(url("/watchlist/instruments"), {
    method: "POST",
    headers: await headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ symbol }),
  });
  if (response.status === 409) return "exists";
  if (!response.ok) throw await failure(response);
  return "added";
}
