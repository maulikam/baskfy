import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

import type { OverlapEventType, OverlapOpinionLabel } from "@/lib/overlap/candidates";

/**
 * The writes `/build/overlap` has — a person's correction of a headline's tag and its removal,
 * a person's label on a row's opinion and its removal, and (25 Sep 2026, Maulik: "put laya scan
 * button on overlap page") the "Scan filings with Laya" button, which queues one read of the
 * listed names' exchange filings and reads its progress back. The scan fills display context
 * only; it never reaches a rank, size or order.
 *
 * `fetch-candidates.ts` next door is the read, and this file sits beside it rather than inside
 * it for the reason `lib/twt/write.ts` gives for the same split: a page whose only writer is
 * the narrowest thing that can work is a page whose writes can be read in one sitting.
 *
 *  - **three paths, as a closed union type rather than a pattern.** `/overlap/tags`,
 *    `/overlap/reviews` and `/overlap/catalyst-scan`, nothing else. A pattern would admit a
 *    route this application must never name; the type is the guard.
 *  - **each path its own methods, paired by the type.** On `/overlap/tags`, PUT stores the
 *    person's word on a headline and DELETE removes it; on `/overlap/reviews`, the same pair for
 *    a row's opinion, named by instrument. On `/overlap/catalyst-scan`, POST queues one scan and
 *    GET reads its progress. The overloads pair them, so a POST to the tags path does not
 *    compile.
 *  - **the bearer never leaves the server.** The token comes from the session here, so no action
 *    and no component ever holds one.
 *  - **it never throws.** A control gets a result either way, because a select that silently
 *    does nothing is the failure mode a person retries.
 *
 * A correction is a label on display context — the tag never entered a rank, a size or an order,
 * and `test_overlap_readonly.py` on the API side asserts the route it reaches writes one table.
 */

/** The only paths this application may write to under `/overlap`. Widening it is a deliberate act. */
export type OverlapWritePath = "/overlap/tags" | "/overlap/reviews" | "/overlap/catalyst-scan";

/** What the action gets back. The sentence is chosen by this app's copy, never by the wire. */
export type OverlapWriteOutcome =
  | { readonly ok: true; readonly status: number; readonly body: unknown }
  | { readonly ok: false; readonly status: number };

/** `/overlap/tags`: a word on a headline, or the word taken back. */
export type OverlapTagRequest =
  | {
      readonly method: "PUT";
      readonly body: { headline: string; event_type: OverlapEventType; note: string | null };
    }
  | { readonly method: "DELETE"; readonly query: { headline: string } };

/** `/overlap/reviews`: a word on a row's opinion, or the word taken back. */
export type OverlapReviewRequest =
  | {
      readonly method: "PUT";
      readonly body: { instrument_id: number; label: OverlapOpinionLabel; note: string | null };
    }
  | { readonly method: "DELETE"; readonly query: { instrument_id: number } };

/** `/overlap/catalyst-scan`: queue one scan, or read the last one's progress. */
export type OverlapScanRequest =
  | { readonly method: "POST"; readonly query: { scope: "actionable" | "all" } }
  | { readonly method: "GET" };

export type OverlapWriteRequest = OverlapTagRequest | OverlapReviewRequest | OverlapScanRequest;

function timeoutMs(): number {
  const parsed = Number.parseInt(process.env.BASKFY_SERVER_FETCH_TIMEOUT_MS?.trim() || "2500", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 2500;
}

/** One request, with the session's bearer. Never throws: the caller gets a result either way. */
export async function overlapWrite(
  path: "/overlap/tags",
  request: OverlapTagRequest,
): Promise<OverlapWriteOutcome>;
export async function overlapWrite(
  path: "/overlap/reviews",
  request: OverlapReviewRequest,
): Promise<OverlapWriteOutcome>;
export async function overlapWrite(
  path: "/overlap/catalyst-scan",
  request: OverlapScanRequest,
): Promise<OverlapWriteOutcome>;
export async function overlapWrite(
  path: OverlapWritePath,
  request: OverlapWriteRequest,
): Promise<OverlapWriteOutcome> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return { ok: false, status: 401 };
  const url = new URL(`${serverApiOrigin()}/api/v1${path}`);
  if (request.method === "DELETE" || request.method === "POST") {
    for (const [name, value] of Object.entries(request.query)) {
      url.searchParams.set(name, String(value));
    }
  }
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
