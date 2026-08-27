import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";
import { bodyForDraft, type BenchmarkOption } from "@/lib/portfolio/draft-mapping";
import type { PortfolioDraft } from "@/lib/portfolio/organize";

/**
 * The write behind §6.7's confirm button — `POST /portfolio`.
 *
 * The translation from the screen's vocabulary to the ledger's lives in
 * `@/lib/portfolio/draft-mapping`, which is pure and separately tested. This module is the part
 * that talks to the network.
 *
 * WHY A SERVER MODULE AND NOT A BROWSER FETCH
 * -------------------------------------------
 * `server-only`, like every other module in this directory. The bearer token lives in the server
 * session; a browser `fetch` to the API would have to be handed one, which is how a token ends up
 * somewhere the page's own scripts can read.
 *
 * WHAT THIS IS NOT
 * ----------------
 * Not an order path. Creating a portfolio files holdings the user **already owns** into a
 * grouping they named; it moves no shares, touches no broker and places nothing. §9's "this page
 * never places an order" is unaffected.
 */

export type { BenchmarkOption } from "@/lib/portfolio/draft-mapping";

/**
 * What the confirm button gets back.
 *
 * A refusal carries the server's own sentence. Criterion 2's conflict is the case that matters:
 * the API answers "HDFC Bank is already in Long term", and that sentence is worth more to the
 * user than any wording this layer could invent, so it is passed through rather than replaced
 * with "Something went wrong".
 */
export type CreateResult =
  | { readonly ok: true; readonly portfolioId: number }
  | { readonly ok: false; readonly reason: string };

const UNREACHABLE =
  "We could not reach the portfolio service, so nothing was created. Your holdings are unchanged.";

async function authHeaders(): Promise<HeadersInit> {
  const session = await auth();
  const token = session?.accessToken;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** The benchmark choices, from the API rather than from a table of ids in this repository. */
export async function fetchBenchmarkOptions(): Promise<readonly BenchmarkOption[]> {
  try {
    const response = await fetch(`${serverApiOrigin()}/api/v1/meta/universes`, {
      headers: await authHeaders(),
      cache: "no-store",
    });
    if (!response.ok) return [];
    const rows: unknown = await response.json();
    return Array.isArray(rows) ? (rows as BenchmarkOption[]) : [];
  } catch {
    // An unreachable universes list must not stop a portfolio being created: the benchmark is an
    // overlay on a chart, and §6.3 already defines the no-benchmark case. Failing the whole
    // write for it would be the tail wagging the dog.
    return [];
  }
}

/**
 * Create the portfolio §6.7's flow described.
 *
 * Reads the benchmark options first so the name the user chose becomes the id the column wants.
 * A failure to resolve it is not a failure to create — see {@link benchmarkIndexId}.
 */
export async function createPortfolio(draft: PortfolioDraft): Promise<CreateResult> {
  const body = bodyForDraft(draft, await fetchBenchmarkOptions());
  try {
    const response = await fetch(`${serverApiOrigin()}/api/v1/portfolio`, {
      method: "POST",
      headers: { ...(await authHeaders()), "content-type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (response.ok) {
      const created: unknown = await response.json();
      return { ok: true, portfolioId: portfolioIdOf(created) };
    }
    return { ok: false, reason: await refusalReason(response) };
  } catch {
    return { ok: false, reason: UNREACHABLE };
  }
}

/**
 * The server's own sentence, or a plain one when it did not give a usable one.
 *
 * The API answers a problem+json body whose `detail` names the stock and the portfolio it is
 * already in. That is the sentence the user needs; inventing a friendlier one would drop the two
 * facts that let them fix it.
 */
async function refusalReason(response: Response): Promise<string> {
  try {
    const problem: unknown = await response.json();
    if (problem !== null && typeof problem === "object") {
      const detail = (problem as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
      const title = (problem as { title?: unknown }).title;
      if (typeof title === "string" && title.trim()) return title;
    }
  } catch {
    // A body that is not JSON tells us nothing; fall through to the status line.
  }
  return `The portfolio service refused this (HTTP ${response.status}), so nothing was created.`;
}

function portfolioIdOf(created: unknown): number {
  if (created !== null && typeof created === "object") {
    const id = (created as { portfolio_id?: unknown; id?: unknown }).portfolio_id;
    if (typeof id === "number") return id;
    const fallback = (created as { id?: unknown }).id;
    if (typeof fallback === "number") return fallback;
  }
  return 0;
}
