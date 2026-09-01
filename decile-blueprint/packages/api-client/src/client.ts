/**
 * The typed Baskfy API client — docs/07 §"OpenAPI → TypeScript".
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

/** The federated ⌘K catalog search (`baskfynavrefactorreport` §F11). */
export type CatalogHitOut = Schemas["CatalogHitOut"];
export type CatalogSearchOut = Schemas["CatalogSearchOut"];
/** `instrument` | `index` | `basket` | `screen` — narrowed, so a kind→href map cannot miss one. */
export type CatalogKind = CatalogHitOut["kind"];

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
export type GoogleSignInIn = Schemas["GoogleSignInIn"];
export type RankPointOut = Schemas["RankPointOut"];

/** docs/07 §"Account & billing" — plans, checkout and invoices (Prompt 13). */
export type PlanOut = Schemas["PlanOut"];
export type PlanListOut = Schemas["PlanListOut"];
export type PlanFeatureOut = Schemas["PlanFeatureOut"];
export type CheckoutSessionIn = Schemas["CheckoutSessionIn"];
export type CheckoutSessionOut = Schemas["CheckoutSessionOut"];
export type InvoiceOut = Schemas["InvoiceOut"];
export type InvoicePage = Schemas["InvoicePage"];

/** docs/07 §"Portfolios & rebalance" — the tracker (Prompt 14).
 *
 * Migration 0019 made a portfolio a node in a forest rather than a flat row, so the response
 * models were renamed with it: a listing is now a `PortfolioForestOut` of roots with nested
 * `children` plus an `orphans` array, and a single portfolio carries `parent_id`,
 * `broker_account_id`, `depth` and `child_ids`. The old flat names are gone rather than
 * aliased — an alias would let a caller keep reading a tree as a list and never find out.
 */
export type PortfolioDetailOut = Schemas["baskfy_api__routers__portfolios__PortfolioDetailOut"];
export type PortfolioNodeOut = Schemas["PortfolioNodeOut"];
export type PortfolioForestOut = Schemas["PortfolioForestOut"];
export type PortfolioWriteDetailOut = Schemas["PortfolioWriteDetailOut"];
export type PortfolioCreateIn = Schemas["PortfolioCreateIn"];
export type PortfolioPatchIn = Schemas["PortfolioPatchIn"];
export type PortfolioRollupOut = Schemas["PortfolioRollupOut"];
export type PortfolioLinkBody = Schemas["PortfolioLinkBody"];
export type HoldingIn = Schemas["HoldingIn"];
export type HoldingOut = Schemas["HoldingOut"];
export type HoldingsIn = Schemas["HoldingsIn"];
export type ImportReportOut = Schemas["ImportReportOut"];
export type ImportRowOut = Schemas["ImportRowOut"];

/** M34 — a portfolio divided into sleeves, and the amounts that follow. */
export type SleeveIn = Schemas["SleeveIn"];
export type SleeveOut = Schemas["SleeveOut"];
export type SleeveListOut = Schemas["SleeveListOut"];
export type SleeveAllocationOut = Schemas["SleeveAllocationOut"];
export type AllocationRowOut = Schemas["AllocationRowOut"];
export type AllocationOut = Schemas["AllocationOut"];
export type StanceOut = Schemas["StanceOut"];
export type SkippedRowOut = Schemas["SkippedRowOut"];
export type CandidateOut = Schemas["CandidateOut"];
export type MatchStatus = Schemas["MatchStatus"];
export type RebalanceIn = Schemas["RebalanceIn"];
export type RebalanceOut = Schemas["RebalanceOut"];
export type RebalanceNameOut = Schemas["RebalanceNameOut"];
export type TargetWeightOut = Schemas["TargetWeightOut"];
export type RebalanceSummaryOut = Schemas["RebalanceSummaryOut"];
export type RebalanceHistoryPage = Schemas["RebalanceHistoryPage"];
export type ExitReason = Schemas["ExitReason"];

/** docs/07 §Backtests — the engine's surface (Prompt 15).
 *
 * `BacktestConfig` splits into an `-Input` and an `-Output` variant because Pydantic serialises
 * `Decimal` as a string on the way out and accepts a number on the way in; `openapi-typescript`
 * models that faithfully rather than collapsing the two. `BacktestConfigIn` is what a form posts,
 * `BacktestConfigOut` is what a stored run echoes back.
 */
export type BacktestConfigIn = Schemas["BacktestConfig-Input"];
export type BacktestConfigOut = Schemas["BacktestConfig-Output"];
export type BacktestCreate = Schemas["BacktestCreate"];
export type BacktestAcceptedOut = Schemas["BacktestAcceptedOut"];
export type BacktestOut = Schemas["BacktestOut"];
export type BacktestSummaryOut = Schemas["BacktestSummaryOut"];
export type BacktestListOut = Schemas["BacktestListOut"];
export type BacktestHoldingOut = Schemas["BacktestHoldingOut"];
export type HoldingPage = Schemas["HoldingPage"];
export type TradeOut = Schemas["TradeOut"];
export type TradePage = Schemas["TradePage"];
export type EquityPointOut = Schemas["EquityPointOut"];
export type DrawdownPointOut = Schemas["baskfy_api__schemas__DrawdownPointOut"];
export type MonthlyReturnOut = Schemas["MonthlyReturnOut"];
export type FragilityRunOut = Schemas["FragilityRunOut"];
export type ExportLinkOut = Schemas["ExportLinkOut"];

/**
 * The staff surface (Prompt 17). `docs/07` does not describe `/admin`; `docs/09` §Observability
 * does, in one line, and `docs/02` rule 5 ("typed end to end ... no hand-written fetch types")
 * applies to it like everything else. See `baskfy_api.routers.admin`.
 */
export type PipelineStepOut = Schemas["PipelineStepOut"];
export type PipelineRunDetailOut = Schemas["PipelineRunDetailOut"];
export type PipelineRunListOut = Schemas["PipelineRunListOut"];
export type DataVersionOut = Schemas["DataVersionOut"];
export type DataVersionListOut = Schemas["DataVersionListOut"];
export type ProviderHealthOut = Schemas["ProviderHealthOut"];
export type ProviderHealthListOut = Schemas["ProviderHealthListOut"];
export type AdminUserOut = Schemas["AdminUserOut"];
export type AdminUserListOut = Schemas["AdminUserListOut"];
export type AdminUserDetailOut = Schemas["AdminUserDetailOut"];
export type EntitlementOverrideOut = Schemas["EntitlementOverrideOut"];
export type EntitlementOverrideIn = Schemas["EntitlementOverrideIn"];
export type AdminActionOut = Schemas["AdminActionOut"];
export type AdminActionListOut = Schemas["AdminActionListOut"];
export type TaskAcceptedOut = Schemas["TaskAcceptedOut"];

/**
 * The resync button (leaf 3.1). `GET /admin/resync` is a dry inspection that changes nothing;
 * `POST /admin/resync` closes what it can and reports what it could not.
 */
export type ResyncFindingOut = Schemas["ResyncFindingOut"];
export type ResyncOutcomeOut = Schemas["ResyncOutcomeOut"];
export type ResyncPlanOut = Schemas["ResyncPlanOut"];

/** The contact form's payload (Prompt 18 §2). Not in docs/07 — `docs/DECISIONS.md` §18.5. */
export type SupportMessageIn = Schemas["SupportMessageIn"];

/**
 * API keys, screen alerts and outbound webhooks (Prompt 20). Not in docs/07 either, and typed for
 * the same reason `/admin` is — see `docs/DECISIONS.md` §20.
 *
 * Note what is **not** here: nothing from `/api/public/v1`. The public tier's routes are absent
 * from `openapi.json` because the router is not mounted while the data-redistribution review is
 * outstanding (`docs/DECISIONS.md` §20.1), so there is nothing to generate a type from.
 */
export type ApiKeyOut = Schemas["ApiKeyOut"];
export type ApiKeyListOut = Schemas["ApiKeyListOut"];
export type ApiKeyCreate = Schemas["ApiKeyCreate"];
export type ApiKeyIssuedOut = Schemas["ApiKeyIssuedOut"];
export type ApiKeyUsageOut = Schemas["ApiKeyUsageOut"];
export type ApiKeyUsagePointOut = Schemas["ApiKeyUsagePointOut"];
export type ScreenAlertOut = Schemas["ScreenAlertOut"];
export type ScreenAlertListOut = Schemas["ScreenAlertListOut"];
export type ScreenAlertCreate = Schemas["ScreenAlertCreate"];
export type ScreenAlertUpdate = Schemas["ScreenAlertUpdate"];
export type ScreenAlertDeliveryOut = Schemas["ScreenAlertDeliveryOut"];
export type UnsubscribeOut = Schemas["UnsubscribeOut"];
export type WebhookEndpointOut = Schemas["WebhookEndpointOut"];
export type WebhookEndpointListOut = Schemas["WebhookEndpointListOut"];
export type WebhookEndpointCreate = Schemas["WebhookEndpointCreate"];
export type WebhookEndpointWithSecretOut = Schemas["WebhookEndpointWithSecretOut"];
export type WebhookDeliveryOut = Schemas["WebhookDeliveryOut"];
export type PublicApiGateOut = Schemas["PublicApiGateOut"];

/** docs/07: "Base: `/api/v1`". */
export const API_PREFIX = "/api/v1";

export interface BaskfyClientOptions {
  /** Origin of the API, e.g. `http://localhost:8000`. The `/api/v1` prefix is part of every path. */
  baseUrl: string;
  /** docs/07: `Authorization: Bearer <JWT>`, minted by the web app. */
  getAccessToken?: () => string | undefined | Promise<string | undefined>;
  /** Injectable transport. Matches `openapi-fetch`'s own signature, which takes a `Request`. */
  fetch?: (request: Request) => Promise<Response>;
  /**
   * W3C trace context for the web → API hop (PROMPTS.md Prompt 17 §1: "OpenTelemetry traces
   * across web → API → worker → database").
   *
   * Returns a `traceparent` value, or `undefined` when nothing is being traced. The API's
   * `FastAPIInstrumentor` extracts it, so a screen run appears as a child of the page render
   * rather than as an unrelated root span.
   *
   * A function rather than a value: a client is created per request (`apps/web/src/lib/api/server`)
   * but the *span* changes within one, and a captured header would attribute every later call to
   * whichever span happened to be active at construction.
   */
  getTraceparent?: () => string | undefined;
}

export type BaskfyClient = ReturnType<typeof createBaskfyClient>;

/**
 * A client with the bearer token attached to every request.
 *
 * The token is read per request rather than captured once, so a refresh in the web app is picked
 * up without rebuilding the client.
 */
export function createBaskfyClient(options: BaskfyClientOptions) {
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

  if (options.getTraceparent) {
    const readTrace = options.getTraceparent;
    client.use({
      onRequest({ request }) {
        const traceparent = readTrace();
        if (traceparent) {
          request.headers.set("traceparent", traceparent);
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
