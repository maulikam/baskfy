"use client";

import { API_PREFIX } from "@baskfy/api-client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { isMarketOpen } from "@/lib/market/session";

/** Read client for `/api/v1/indices*` and the latest swing-candidate run. */

export type FreshnessState = "live" | "close" | "stale";

export interface Freshness {
  state: FreshnessState;
  as_of: string | null;
}

export type IndexGroup = "Broad market" | "Sectoral" | "Thematic" | "Strategy" | (string & {});

export interface IndexRow {
  id: number;
  slug: string;
  name: string;
  group: IndexGroup;
  live: boolean;
  level: number | null;
  chg_pct: number | null;
  vs20_pct: number | null;
  cross_10_20: boolean | null;
  above_20: boolean | null;
  ret_5d: number | null;
  ret_30d: number | null;
  ret_1y: number | null;
  hi_52w: number | null;
  lo_52w: number | null;
  dist_52w_hi_pct: number | null;
  pe: number | null;
  pb: number | null;
  div_yield: number | null;
  spark_30d: number[] | null;
  swing_hits: number | null;
  stale: boolean;
  as_of: string | null;
}

export interface RegimeSummary {
  above_20: number;
  total: number;
  cross_pct: number | null;
  breadth_30d: { date: string; above_20_pct: number | null }[];
}

export interface IndicesList {
  freshness: Freshness;
  data: IndexRow[];
  regime: RegimeSummary | null;
}

export interface IndexDetailRow extends IndexRow {
  open: number | null;
  high: number | null;
  low: number | null;
  prev_close: number | null;
  constituents_up: number | null;
  constituents_down: number | null;
  constituents_as_of: string | null;
}

export interface IndexDetail {
  freshness: Freshness;
  index: IndexDetailRow;
  leading: boolean;
}

export interface Constituent {
  symbol: string;
  name: string | null;
  isin: string | null;
  weight: number | null;
  ltp: number | null;
  chg_pct: number | null;
  vs20_pct: number | null;
  above_20: boolean | null;
  dist_52w_hi_pct: number | null;
  hits: string[];
}

export interface ConstituentsPayload {
  freshness: Freshness;
  constituents_as_of: string | null;
  stale_constituents: boolean;
  data: Constituent[];
}

export interface HistoryPoint {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  dma10: number | null;
  dma20: number | null;
  dma50: number | null;
}

export interface RefreshResult {
  refreshed_master: number;
  constituents_ok: number;
  constituents_failed: string[];
}

export interface SwingCandidate {
  symbol: string;
  scanner: string;
  setup: string | null;
  entry: number | null;
  stop: number | null;
  risk_pct: number | null;
  via_indices: { index: string; strength: number | null }[];
  universe_score: number | null;
  score: number | null;
  rank: number | null;
}

export interface SwingCandidates {
  run_at: string | null;
  mode: string | null;
  leading: { index: string; strength: number | null; rs: number | null }[];
  data: SwingCandidate[];
}

export class IndicesError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "IndicesError";
    this.status = status;
  }
}

async function headers(extra: Record<string, string> = {}): Promise<Record<string, string>> {
  const token = await accessToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${apiOrigin()}${API_PREFIX}${path}`, {
    headers: await headers(),
    cache: "no-store",
    signal: signal ?? AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new IndicesError(response.status, `${path} answered ${response.status}`);
  return (await response.json()) as T;
}

export function fetchIndices(signal?: AbortSignal): Promise<IndicesList> {
  return getJson<IndicesList>("/indices", signal);
}

export function fetchIndexDetail(id: string | number, signal?: AbortSignal): Promise<IndexDetail> {
  return getJson<IndexDetail>(`/indices/${encodeURIComponent(String(id))}`, signal);
}

export function fetchIndexConstituents(
  id: string | number,
  signal?: AbortSignal,
): Promise<ConstituentsPayload> {
  return getJson<ConstituentsPayload>(
    `/indices/${encodeURIComponent(String(id))}/constituents`,
    signal,
  );
}

export async function fetchIndexHistory(
  id: string | number,
  days = 365,
  signal?: AbortSignal,
): Promise<HistoryPoint[]> {
  const body = await getJson<{ data: HistoryPoint[] }>(
    `/indices/${encodeURIComponent(String(id))}/history?days=${days}`,
    signal,
  );
  return body.data;
}

export function fetchSwingCandidates(signal?: AbortSignal): Promise<SwingCandidates> {
  return getJson<SwingCandidates>("/swing/candidates?run=latest&mode=index_led", signal);
}

function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const hit = document.cookie.split(";").find((c) => c.trim().startsWith("baskfy_csrf="));
  return hit ? decodeURIComponent(hit.trim().slice("baskfy_csrf=".length)) : null;
}

/** Staff only: re-joins the master list and re-reads constituents. Never touches orders. */
export async function refreshIndices(): Promise<RefreshResult> {
  const csrf = csrfToken();
  const response = await fetch(`${apiOrigin()}${API_PREFIX}/indices/refresh`, {
    method: "POST",
    headers: await headers(csrf ? { "X-CSRF-Token": csrf } : {}),
    credentials: "include",
    cache: "no-store",
    signal: AbortSignal.timeout(120_000),
  });
  if (!response.ok) throw new IndicesError(response.status, `refresh answered ${response.status}`);
  return (await response.json()) as RefreshResult;
}

/** 30 s while the session is open, 5 min otherwise — the other live surfaces' rhythm. */
export function indicesPollInterval(): number {
  return isMarketOpen() ? 30_000 : 5 * 60_000;
}

const IST_TIME = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});
const IST_DAY = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  timeZone: "Asia/Kolkata",
});

function parse(iso: string | null): Date | null {
  if (!iso) return null;
  const d = new Date(iso.length === 10 ? `${iso}T15:30:00+05:30` : iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function istTime(iso: string | null): string {
  const d = parse(iso);
  return d ? IST_TIME.format(d) : "";
}

export function istDay(iso: string | null): string {
  const d = parse(iso);
  return d ? IST_DAY.format(d) : "";
}

/** `Live · 14:32:05` / `Close of 5 Nov` / `Stale · as of 5 Nov 14:31:10`. */
export function freshnessLabel(f: Freshness | null | undefined, fetchFailed = false): string {
  if (!f) return fetchFailed ? "Could not reach the index service" : "Loading";
  const day = istDay(f.as_of);
  const time = istTime(f.as_of);
  if (fetchFailed) return `Cached · as of ${[day, time].filter(Boolean).join(" ")}`;
  if (f.state === "live") return `Live · ${time}`;
  if (f.state === "close") return day ? `Close of ${day}` : "Last close";
  return `Stale · as of ${[day, time].filter(Boolean).join(" ")}`;
}

export function freshnessTone(
  f: Freshness | null | undefined,
  fetchFailed = false,
): "positive" | "warning" | "negative" | "neutral" {
  if (!f) return fetchFailed ? "warning" : "neutral";
  if (fetchFailed) return "warning";
  return f.state === "live" ? "positive" : f.state === "close" ? "warning" : "negative";
}
