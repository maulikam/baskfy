import "server-only";

import type { Schemas } from "@baskfy/api-client";

import { serverApiOrigin } from "@/lib/api/config";
import { serverFetchJson, serverFetchJsonOrNull } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";
import type { GroupingSuggestion } from "@/lib/portfolio/organize";

/**
 * Server-side reads for PORTFOLIO_REDESIGN.md §6.6's Unallocated centrepiece and §6.7's flow.
 *
 * Every read can come back "we do not have this", and each of those is a *different* absence that
 * the page renders differently:
 *
 * * **No response at all** — the overview API is not reachable. The page says so; it does not
 *   render an empty Unallocated section, because "nothing is unallocated" and "we could not ask"
 *   are opposite messages and only one of them is reassuring.
 * * **A response with nothing in it** — a real, connected, fully-allocated account. That is the
 *   "everything is allocated" state and it is good news.
 * * **Suggestions with nothing behind them** — `GET /portfolio/suggestions` serves
 *   `baskfy_core.grouping_suggestions.suggest_groupings`, and it answers with its own
 *   `unavailable_reason` when none of the three bases had an input to work from (no sectors, no
 *   purchase dates, no models followed). {@link fetchGroupingSuggestions} passes that sentence
 *   through untouched, so §6.6's screen prints the reason the server gave rather than an empty
 *   "no ideas" list — and {@link SUGGESTIONS_UNAVAILABLE} is left for the one case the server
 *   cannot describe, which is not answering at all.
 *
 * No order helpers. Nothing on these surfaces places an order: creating a portfolio
 * (`POST /portfolio`) files holdings the user already owns into a grouping they named, and the
 * shares never move.
 */

export type OverviewOut = Schemas["OverviewOut"];
export type HoldingsOut = Schemas["baskfy_api__routers__portfolio_overview__HoldingsOut"];

async function authHeaders(): Promise<HeadersInit> {
  const session = await auth();
  const token = session?.accessToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function tryJson(path: string): Promise<unknown> {
  return serverFetchJsonOrNull({
    url: `${serverApiOrigin()}/api/v1${path}`,
    headers: await authHeaders(),
  });
}

/** The overview payload, or `null` when the API did not answer. `null` is not an empty account. */
export async function fetchPortfolioOverview(): Promise<OverviewOut | null> {
  const data = await tryJson("/portfolio/overview");
  return data === null ? null : (data as OverviewOut);
}

/**
 * Why the overview is missing, when it is — because "could not load" and "you are not allowed to
 * see this" are different sentences and need different next actions.
 *
 * `serverFetchJsonOrNull` collapses every failure to `null`, which is right for a surface that
 * prefers an empty state to a crash and wrong for a screen that has to TELL a person what
 * happened. A reader who has lost access should be told to ask the account owner; a reader
 * looking at an outage should be told to try again. Offering "try again" to the first is a loop
 * they cannot exit.
 *
 * The status is read off the message `serverFetchJson` throws (`"<url> responded 403"`), which is
 * the only place it survives. Brittle, and better than the alternative of not distinguishing them
 * at all — if that message changes this falls back to "unreachable", which is the safe direction.
 */
export type OverviewFailure = "restricted" | "unreachable" | null;

export async function readPortfolioOverview(): Promise<{
  overview: OverviewOut | null;
  failure: OverviewFailure;
}> {
  try {
    const data = await serverFetchJson({
      url: `${serverApiOrigin()}/api/v1/portfolio/overview`,
      headers: await authHeaders(),
    });
    return { overview: data as OverviewOut, failure: null };
  } catch (error) {
    const message = error instanceof Error ? error.message : "";
    const restricted = /responded 40[13]$/.test(message);
    return { overview: null, failure: restricted ? "restricted" : "unreachable" };
  }
}

/** The flat broker-level truth (§2), or `null` when the API did not answer. */
export async function fetchPortfolioHoldings(): Promise<HoldingsOut | null> {
  const data = await tryJson("/portfolio/holdings");
  return data === null ? null : (data as HoldingsOut);
}

/** What §6.6's first-run helper got back, and — when it got nothing — why. */
export interface SuggestionsResult {
  suggestions: GroupingSuggestion[];
  /** `null` when suggestions were actually computed, even if there were none to make. */
  unavailableReason: string | null;
  /** The sector map the §6.7 picker filters by. Empty when instrument sectors are not synced. */
  sectors: Record<string, string>;
}

interface SuggestionsPayload {
  suggestions?: GroupingSuggestion[];
  sectors?: Record<string, string>;
  /** Set by the API only when no basis could be computed at all. Never composed here. */
  unavailable_reason?: string | null;
}

export const SUGGESTIONS_UNAVAILABLE =
  "We could not reach the suggestions service just now. Sorting by hand below works today.";

/**
 * The ranked suggestions §6.6 sorts an unallocated pile with.
 *
 * The shape returned by the endpoint is `grouping_suggestions.GroupingSuggestion` serialised as
 * it stands — the ranking, the rationale sentence and the coverage are all computed in
 * `packages/core`, where they are tested, and are not recomputed here.
 */
export async function fetchGroupingSuggestions(): Promise<SuggestionsResult> {
  const data = await tryJson("/portfolio/suggestions");
  if (data === null) {
    return { suggestions: [], unavailableReason: SUGGESTIONS_UNAVAILABLE, sectors: {} };
  }
  const payload = data as SuggestionsPayload;
  return {
    suggestions: payload.suggestions ?? [],
    unavailableReason: payload.unavailable_reason ?? null,
    sectors: payload.sectors ?? {},
  };
}
