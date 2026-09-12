"use server";

import { revalidatePath } from "next/cache";

import { serverApiOrigin } from "@/lib/api/config";
import { SERVER_FETCH_TIMEOUT_MS } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";
import type { MutationResult } from "@/lib/auth/me";

/**
 * Dismissing a pending action — the X on the home carousel (SC9).
 *
 * A **server action**, not a browser `fetch`, for the reason `actions/support.ts` gives: the CSP
 * in `src/middleware.ts` names the API origin in `connect-src`, and every browser-side call is
 * one more cross-origin surface to keep in step. This posts same-origin and the server carries
 * the bearer token, which also means the session token never has to reach client JavaScript.
 *
 * `POST /cb/pending-actions/{id}/dismiss` only stamps `dismissed_at` on the caller's own row.
 * There is no resolve action here and no order path anywhere near it — resolving is what the
 * underlying flow does when the thing is actually fixed, not what a card's close button means.
 */

const UNREACHABLE = "We could not reach the service. The card will come back on refresh.";

export async function dismissPendingAction(id: string): Promise<MutationResult> {
  const trimmed = id.trim();
  if (!/^\d+$/.test(trimmed)) {
    // `cb_pending_action.id` is a bigint. Anything else never came from a card we rendered.
    return { ok: false, message: "That is not an action we know about." };
  }

  const session = await auth();
  const token = session?.accessToken;
  if (!token) return { ok: false, message: "Your session has expired. Sign in again." };

  let response: Response;
  try {
    response = await fetch(`${serverApiOrigin()}/api/v1/cb/pending-actions/${trimmed}/dismiss`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(SERVER_FETCH_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, message: UNREACHABLE };
  }

  if (!response.ok) {
    return {
      ok: false,
      message: response.status === 404 ? "That action is already gone." : UNREACHABLE,
    };
  }

  // Both surfaces render the same list, so both have to forget the card.
  revalidatePath("/home");
  revalidatePath("/portfolio/overview");
  return { ok: true, message: "Dismissed." };
}
