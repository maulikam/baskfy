import "server-only";

import type { MeOut } from "@baskfy/api-client";

import { apiOrigin } from "@/lib/api/config";
import { serverFetchJsonOrNull } from "@/lib/api/server-fetch";
import { auth } from "@/lib/auth";

/**
 * `GET /me` for the server-rendered account pages — docs/07: "profile + entitlements".
 *
 * Never cached. A profile is per-user and changes the moment the user changes it; a cached
 * `/me` is the classic way to show one account's details to another.
 *
 * Tree-5: timed like every other RSC→API hop — `(app)/layout` awaits this on every navigation,
 * so an unbounded `/me` stalls the whole shell (observed 5–20s list pages).
 */
export async function fetchMe(): Promise<MeOut | null> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return null;

  const data = await serverFetchJsonOrNull({
    url: `${apiOrigin()}/api/v1/me`,
    headers: { Authorization: `Bearer ${token}` },
  });
  return (data as MeOut | null) ?? null;
}

export type MutationResult = { ok: boolean; message: string };

const UNREACHABLE = "We could not reach the accounts service. Try again in a moment.";

/** POST/PATCH/DELETE against `/me*` with the session's bearer token attached. */
export async function callMe(
  path: string,
  method: "POST" | "PATCH" | "DELETE",
  body: unknown,
): Promise<{ ok: boolean; detail: string; status: number }> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return { ok: false, detail: "You are not signed in.", status: 401 };

  try {
    const timeoutMs = Number.parseInt(
      process.env.BASKFY_SERVER_FETCH_TIMEOUT_MS?.trim() || "2500",
      10,
    );
    const response = await fetch(`${apiOrigin()}/api/v1${path}`, {
      method,
      headers: { "content-type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 2500),
    });
    if (response.ok) return { ok: true, detail: "", status: response.status };
    const problem = (await response.json().catch(() => null)) as { detail?: string } | null;
    return {
      ok: false,
      detail: problem?.detail ?? "That did not work.",
      status: response.status,
    };
  } catch {
    return { ok: false, detail: UNREACHABLE, status: 0 };
  }
}
