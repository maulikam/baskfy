import "server-only";

import { ServerFetchStatusError } from "@/lib/api/server-fetch";

/**
 * **"Nothing has been written yet" is not the same answer as "you were refused".**
 *
 * `gates/sleeve-read-contract.md` C7, the general form of the bug that started the 12 Sep 2026
 * audit. Every sleeve's `fetch.ts` funnelled *every* non-OK response through `readOrNull` into
 * `null`, and every page renders `null` as its empty state — the swing hub's "No flags today",
 * the TWT hub's "Nothing has been read for this strategy yet". So a signed-in second account
 * (the box holds six, with portfolios for users 1 **and** 6) was told the strategy had never run
 * over a database holding 157 setups; a deployment whose sole tenant was misconfigured showed an
 * ordinary quiet day; and a 500 looked like a calm Sunday.
 *
 * ## Why this reads the body and not the status
 *
 * The sole-tenant refusal is deliberately a **404** and not a 403 — M43.4, "a 403 would confirm
 * that the surface holds somebody's data" — which makes it, by design, indistinguishable from
 * absence to anything that looks only at the status. It is also a status this layer sees for
 * honest reasons: a scan run id that does not exist, an instrument that does not exist. So the
 * API marks the refusal with an RFC 9457 extension member and this module reads it. The constant
 * is `baskfy_api.curated_tenant.SOLE_TENANT_REFUSED`; the two spellings are a contract.
 *
 * ## What is deliberately still `null`
 *
 * A timeout, a network error, and any other 4xx. A slow or unreachable API renders the empty
 * state, as it did before — `server-fetch.ts`'s whole reason for existing is that an RSC render
 * must fail fast into empty UI rather than hang. Making a 4s timeout take a trading page down
 * is a bigger behaviour change than this audit asked for, and it is named here rather than
 * quietly folded in: **a timed-out read still renders as "nothing yet"**, and that is the
 * remaining half of C7.
 */
export const SOLE_TENANT_REFUSED = "not-the-sole-tenant";

/** Why a sleeve read produced no data, when the reason is not "there is none". */
export interface SleeveUnavailable {
  /**
   * `refused` — this account is not the one this deployment serves.
   * `degraded` — the deployment cannot answer (no sole tenant configured, pipeline degraded).
   * `error` — the API failed.
   */
  kind: "refused" | "degraded" | "error";
  status: number;
  /** The problem's `detail`, for an operator; never the only thing a page shows a person. */
  detail: string;
}

const SERVER_ERROR_FLOOR = 500;
const NOT_FOUND = 404;
const SERVICE_UNAVAILABLE = 503;

function detailOf(problem: Record<string, unknown> | null, fallback: string): string {
  const detail = problem?.detail;
  return typeof detail === "string" && detail.length > 0 ? detail : fallback;
}

/**
 * The verdict on a failed read: `null` when the page's empty state is the honest answer, and a
 * {@link SleeveUnavailable} when it is not.
 */
export function sleeveUnavailable(error: unknown): SleeveUnavailable | null {
  if (!(error instanceof ServerFetchStatusError)) return null;
  const { status, problem } = error;
  if (status === NOT_FOUND) {
    return problem?.reason === SOLE_TENANT_REFUSED
      ? {
          kind: "refused",
          status,
          detail: detailOf(problem, "This account is not the one this deployment serves."),
        }
      : null;
  }
  if (status === SERVICE_UNAVAILABLE) {
    return {
      kind: "degraded",
      status,
      detail: detailOf(problem, "This strategy is unavailable."),
    };
  }
  if (status >= SERVER_ERROR_FLOOR) {
    return {
      kind: "error",
      status,
      detail: detailOf(problem, "This strategy could not be read."),
    };
  }
  return null;
}

/**
 * Thrown by a sleeve read that was refused, degraded or errored, so the page cannot render it as
 * an empty database. The route segment's `error.tsx` turns it into a sentence a person can act
 * on; there is no code path in which it becomes "no flags today".
 */
export class SleeveUnavailableError extends Error {
  readonly kind: SleeveUnavailable["kind"];
  readonly status: number;
  readonly detail: string;

  constructor(path: string, unavailable: SleeveUnavailable) {
    super(`${path}: ${unavailable.kind} (${unavailable.status}) — ${unavailable.detail}`);
    this.name = "SleeveUnavailableError";
    this.kind = unavailable.kind;
    this.status = unavailable.status;
    this.detail = unavailable.detail;
  }
}
