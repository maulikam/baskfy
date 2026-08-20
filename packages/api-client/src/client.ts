/**
 * The typed Decile API client — docs/07 §"OpenAPI → TypeScript".
 *
 *     "`services/api` emits `openapi.json` on build; `packages/api-client` is generated with
 *      `openapi-typescript` + `openapi-fetch` in CI. Hand-written request types are a CI failure."
 *
 * Everything in `./generated/schema.js` is produced from `openapi.json` and must never be edited
 * by hand; `make openapi && make client` regenerates both, and CI fails if either is stale. This
 * file is the thin, hand-written *wrapper* the doc allows: it binds a base URL and a bearer
 * token, and it re-exports the generated types under names that read well at call sites. It
 * declares no request or response shapes of its own — every one of them comes from `paths`.
 */
import createClient from "openapi-fetch";

import type { components, paths } from "./generated/schema.js";

export type { components, paths } from "./generated/schema.js";

/** Every response and request body, by its OpenAPI schema name. */
export type Schemas = components["schemas"];

export type FactorOut = Schemas["FactorOut"];
export type ColumnOut = Schemas["ColumnOut"];
export type UniverseOut = Schemas["UniverseOut"];
export type StatusOut = Schemas["StatusOut"];
export type TradingDaysOut = Schemas["TradingDaysOut"];
export type ScreenOut = Schemas["ScreenOut"];
export type ScreenListOut = Schemas["ScreenListOut"];
export type ScreenCreate = Schemas["ScreenCreate"];
export type ScreenUpdate = Schemas["ScreenUpdate"];
export type RunRequest = Schemas["RunRequest"];
export type PreviewRequest = Schemas["PreviewRequest"];
export type ScreenRunResponse = Schemas["ScreenRunResponse"];
export type ScreenRunRowOut = Schemas["ScreenRunRowOut"];
export type ScreenRunPage = Schemas["ScreenRunPage"];
export type ProblemOut = Schemas["ProblemOut"];

/** docs/07 §Instruments — the factsheet and its supporting series. */
export type InstrumentHitOut = Schemas["InstrumentHitOut"];
export type InstrumentSearchOut = Schemas["InstrumentSearchOut"];
export type FactsheetOut = Schemas["FactsheetOut"];
export type InstrumentHeaderOut = Schemas["InstrumentHeaderOut"];
export type CellOut = Schemas["CellOut"];
export type MetricCardOut = Schemas["MetricCardOut"];
export type MarketQualityOut = Schemas["MarketQualityOut"];
export type LabelledValueOut = Schemas["LabelledValueOut"];
export type IndexMembershipOut = Schemas["IndexMembershipOut"];
export type CorporateActionOut = Schemas["CorporateActionOut"];
export type CorporateActionsOut = Schemas["CorporateActionsOut"];
export type InstrumentHistoryOut = Schemas["InstrumentHistoryOut"];
export type HistoryPointOut = Schemas["HistoryPointOut"];
export type RankHistoryOut = Schemas["RankHistoryOut"];

/** docs/07 §"Market data surfaces" — the dashboard, breadth, and the listings register. */
export type IndexRowOut = Schemas["IndexRowOut"];
export type IndexDashboardOut = Schemas["IndexDashboardOut"];
export type GaugeOut = Schemas["GaugeOut"];
export type MarketHealthOut = Schemas["MarketHealthOut"];
export type MarketHealthPointOut = Schemas["MarketHealthPointOut"];
export type MarketHealthHistoryOut = Schemas["MarketHealthHistoryOut"];
export type ListingOut = Schemas["ListingOut"];
export type ListingsPage = Schemas["ListingsPage"];

/** docs/07 §"Account & billing" — the auth and profile surface (Prompt 12). */
export type SessionOut = Schemas["SessionOut"];
export type AcceptedOut = Schemas["AcceptedOut"];
export type MeOut = Schemas["MeOut"];
export type EntitlementsOut = Schemas["EntitlementsOut"];
export type DeletionOut = Schemas["DeletionOut"];
export type DataExportOut = Schemas["DataExportOut"];
export type RegisterIn = Schemas["RegisterIn"];
export type LoginIn = Schemas["LoginIn"];
export type ChangePasswordIn = Schemas["ChangePasswordIn"];
export type RankPointOut = Schemas["RankPointOut"];

/** docs/07: "Base: `/api/v1`". */
export const API_PREFIX = "/api/v1";

export interface DecileClientOptions {
  /** Origin of the API, e.g. `http://localhost:8000`. The `/api/v1` prefix is part of every path. */
  baseUrl: string;
  /** docs/07: `Authorization: Bearer <JWT>`, minted by the web app. */
  getAccessToken?: () => string | undefined | Promise<string | undefined>;
  /** Injectable transport. Matches `openapi-fetch`'s own signature, which takes a `Request`. */
  fetch?: (request: Request) => Promise<Response>;
}

export type DecileClient = ReturnType<typeof createDecileClient>;

/**
 * A client with the bearer token attached to every request.
 *
 * The token is read per request rather than captured once, so a refresh in the web app is picked
 * up without rebuilding the client.
 */
export function createDecileClient(options: DecileClientOptions) {
  const client = createClient<paths>(
    options.fetch
      ? { baseUrl: options.baseUrl, fetch: options.fetch }
      : { baseUrl: options.baseUrl },
  );

  if (options.getAccessToken) {
    const readToken = options.getAccessToken;
    client.use({
      async onRequest({ request }) {
        const token = await readToken();
        if (token) {
          request.headers.set("Authorization", `Bearer ${token}`);
        }
        return request;
      },
    });
  }

  return client;
}

/** RFC 9457 `type` values — docs/07 §"Error catalogue". */
export const PROBLEM_TYPES = [
  "invalid-screen-definition",
  "unauthenticated",
  "payment-required",
  "not-found",
  "stale-data-version",
  "no-trading-day",
  "rate-limited",
  "pipeline-degraded",
  "internal-error",
] as const;

export type ProblemType = (typeof PROBLEM_TYPES)[number];

/** Narrow an unknown error body to a problem document. */
export function isProblem(value: unknown): value is ProblemOut {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.type === "string" && typeof candidate.status === "number";
}
