import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

import type { OverlapEventType } from "@/lib/overlap/candidates";

/**
 * The one write `/build/overlap` has — a person's correction of a headline's tag, and its
 * removal. Nothing else, ever.
 *
 * `fetch-candidates.ts` next door is the read, and this file sits beside it rather than inside
 * it for the reason `lib/twt/write.ts` gives for the same split: a page whose only writer is
 * the narrowest thing that can work is a page whose writes can be read in one sitting.
 *
 *  - **one path, as a closed union type rather than a pattern.** `/overlap/tags` and nothing
 *    else. A pattern would admit a route this application must never name; the type is the guard.
 *  - **two methods.** PUT stores the person's word; DELETE removes it. There is no POST.
 *  - **the bearer never leaves the server.** The token comes from the session here, so no action
 *    and no component ever holds one.
 *  - **it never throws.** A control gets a result either way, because a select that silently
 *    does nothing is the failure mode a person retries.
 *
 * A correction is a label on display context — the tag never entered a rank, a size or an order,
 * and `test_overlap_readonly.py` on the API side asserts the route it reaches writes one table.
 */

/** The only path this application may write to under `/overlap`. Widening it is a deliberate act. */
export type OverlapWritePath = "/overlap/tags";

/** What the action gets back. The sentence is chosen by this app's copy, never by the wire. */
export type OverlapWriteOutcome =
  | { readonly ok: true; readonly status: number; readonly body: unknown }
  | { readonly ok: false; readonly status: number };

export type OverlapWriteRequest =
  | {
      readonly method: "PUT";
      readonly body: { headline: string; event_type: OverlapEventType; note: string | null };
    }
  | { readonly method: "DELETE"; readonly query: { headline: string } };

function timeoutMs(): number {
  const parsed = Number.parseInt(process.env.BASKFY_SERVER_FETCH_TIMEOUT_MS?.trim() || "2500", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 2500;
}

/** One request, with the session's bearer. Never throws: the caller gets a result either way. */
export async function overlapWrite(
  path: OverlapWritePath,
  request: OverlapWriteRequest,
): Promise<OverlapWriteOutcome> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return { ok: false, status: 401 };
  const url = new URL(`${serverApiOrigin()}/api/v1${path}`);
  if (request.method === "DELETE") url.searchParams.set("headline", request.query.headline);
  try {
    const response = await fetch(url, {
      method: request.method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(request.method === "PUT" ? { "Content-Type": "application/json" } : {}),
      },
      ...(request.method === "PUT" ? { body: JSON.stringify(request.body) } : {}),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs()),
    });
    if (!response.ok) return { ok: false, status: response.status };
    if (response.status === 204) return { ok: true, status: response.status, body: null };
    const parsed: unknown = await response.json().catch(() => null);
    return { ok: true, status: response.status, body: parsed };
  } catch {
    return { ok: false, status: 0 };
  }
}
