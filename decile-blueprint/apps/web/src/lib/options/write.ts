import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * The options tab's two writes — and only those two (`docs/options/05` §2, OP5).
 *
 * The path type is a **closed union of two strings**, not a pattern: an event day (a day no sleeve
 * trades — it can only make the book do less) and the settings behind the server's ceilings. A
 * pattern that admitted `/options/*` would admit a confirm route the day somebody wrote one, so
 * there is no pattern. Neither write can build, confirm or close a plan; the desk console is the
 * only surface with a Confirm (`05` §3). `read-only.test.tsx` asserts the union stays this size.
 */
export type OptionsWritePath = "/options/event-day" | "/options/config";
export type OptionsWriteMethod = "POST" | "DELETE" | "PATCH";

export type OptionsFormResult =
  | { readonly ok: true; readonly message: string }
  | {
      readonly ok: false;
      readonly error: string;
      /** The field a 422 named, so the form can put the sentence beside it. */
      readonly field?: string;
      readonly ceiling?: string;
      readonly status?: number;
    };

export type OptionsWriteOutcome =
  | { readonly ok: true; readonly status: number; readonly body: unknown }
  | (Extract<OptionsFormResult, { ok: false }> & { readonly status: number });

const UNREACHABLE =
  "We could not reach the options service, so nothing changed. Try again.";

function timeoutMs(): number {
  const parsed = Number.parseInt(
    process.env.BASKFY_SERVER_FETCH_TIMEOUT_MS?.trim() || "2500",
    10,
  );
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 2500;
}

async function refusal(
  response: Response,
): Promise<Extract<OptionsWriteOutcome, { ok: false }>> {
  let problem: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = await response.json();
    if (parsed !== null && typeof parsed === "object")
      problem = parsed as Record<string, unknown>;
  } catch {
    // A body that is not JSON tells us nothing; the status line below is what is left.
  }
  const text = (key: string): string | undefined => {
    const value = problem?.[key];
    return typeof value === "string" && value.trim() ? value : undefined;
  };
  const field = text("field");
  const ceiling = text("ceiling");
  return {
    ok: false,
    status: response.status,
    error:
      text("detail") ??
      `The options service refused this (HTTP ${response.status}).`,
    ...(field ? { field } : {}),
    ...(ceiling ? { ceiling } : {}),
  };
}

export async function optionsWrite(
  method: OptionsWriteMethod,
  path: OptionsWritePath,
  { body, search }: { body?: unknown; search?: Record<string, string> } = {},
): Promise<OptionsWriteOutcome> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token)
    return { ok: false, status: 401, error: "Sign in again to change this." };
  const url = new URL(`${serverApiOrigin()}/api/v1${path}`);
  for (const [key, value] of Object.entries(search ?? {}))
    url.searchParams.set(key, value);
  try {
    const response = await fetch(url.toString(), {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs()),
    });
    if (!response.ok) return refusal(response);
    const parsed: unknown =
      response.status === 204 ? null : await response.json().catch(() => null);
    return { ok: true, status: response.status, body: parsed };
  } catch {
    return { ok: false, status: 0, error: UNREACHABLE };
  }
}
