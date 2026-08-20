import "server-only";

import type { MeOut } from "@decile/api-client";

import { apiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * `GET /me` for the server-rendered account pages — docs/07: "profile + entitlements".
 *
 * Never cached. A profile is per-user and changes the moment the user changes it; a cached
 * `/me` is the classic way to show one account's details to another.
 */
export async function fetchMe(): Promise<MeOut | null> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return null;

  const response = await fetch(`${apiOrigin()}/api/v1/me`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) return null;
  return (await response.json()) as MeOut;
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
    const response = await fetch(`${apiOrigin()}/api/v1${path}`, {
      method,
      headers: { "content-type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
      cache: "no-store",
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
