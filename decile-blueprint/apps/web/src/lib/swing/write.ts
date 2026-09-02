import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * The swing hub's writes — SW14, `docs/swing/05` §2 and `02` Track A.
 *
 * Track A allows this surface exactly the writes that "change no money": a watchlist row added,
 * annotated, re-confirmed or dismissed, and the settings form. Each server action under
 * `(app)/swing` and `(app)/me/swing` makes **one** call through here, with the bearer token from
 * the server session — the token never reaches the browser — and hands the form a typed result
 * rather than throwing at it.
 *
 * What this is not: an order path. The paths a caller may name are the ones Track A permits,
 * and they are a closed type rather than a pattern — the desk console's confirm route would
 * match any pattern that admits `/swing/watch`, and that route is precisely what Track C §4
 * keeps out of this application. `lib/swing/__tests__/read-only.test.ts` scans this file for the
 * verbs that place, and `(app)/swing/__tests__/read-only.test.tsx` enumerates the actions that
 * call it.
 */

export type SwingWritePath = "/swing/watch" | `/swing/watch/${number}` | "/swing/config";

/** What a form gets back. `ok: false` carries the server's own sentence, and the ceiling if one was crossed. */
export type SwingFormResult =
  | { readonly ok: true; readonly message: string }
  | {
      readonly ok: false;
      readonly error: string;
      /** The field a 422 `setting-above-ceiling` named, so the form can put the sentence beside it. */
      readonly field?: string;
      readonly ceiling?: string;
      readonly env_var?: string;
      readonly status?: number;
    };

export type SwingWriteOutcome =
  | { readonly ok: true; readonly body: unknown; readonly status: number }
  | (Extract<SwingFormResult, { ok: false }> & { readonly status: number });

const UNREACHABLE =
  "We could not reach the swing service, so nothing changed. Try again in a moment.";

function timeoutMs(): number {
  const parsed = Number.parseInt(process.env.BASKFY_SERVER_FETCH_TIMEOUT_MS?.trim() || "2500", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 2500;
}

/**
 * The server's refusal, as the fields the form renders.
 *
 * The API answers problem+json. A `setting-above-ceiling` carries `field`, `ceiling` and
 * `env_var` (`baskfy_api.problems.setting_above_ceiling`), and those are worth more to the
 * person than any sentence this layer could write: "max 1.0% — set by the server" is rendered
 * from them. Anything else passes its `detail` through.
 */
async function refusal(response: Response): Promise<Extract<SwingWriteOutcome, { ok: false }>> {
  let problem: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = await response.json();
    if (parsed !== null && typeof parsed === "object") problem = parsed as Record<string, unknown>;
  } catch {
    // A body that is not JSON tells us nothing; the status line below is what is left.
  }
  const text = (key: string): string | undefined => {
    const value = problem?.[key];
    return typeof value === "string" && value.trim() ? value : undefined;
  };
  const detail =
    text("detail") ??
    text("title") ??
    `The swing service refused this (HTTP ${response.status}), so nothing changed.`;
  const field = text("field");
  const ceiling = text("ceiling");
  const envVar = text("env_var");
  return {
    ok: false,
    error: detail,
    status: response.status,
    ...(field ? { field } : {}),
    ...(ceiling ? { ceiling } : {}),
    ...(envVar ? { env_var: envVar } : {}),
  };
}

/** One request, with the session's bearer. Never throws: the form gets a result either way. */
export async function swingWrite(
  method: "POST" | "PATCH" | "DELETE",
  path: SwingWritePath,
  body?: Record<string, unknown>,
): Promise<SwingWriteOutcome> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return { ok: false, error: "You are not signed in.", status: 401 };
  try {
    const response = await fetch(`${serverApiOrigin()}/api/v1${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs()),
    });
    if (!response.ok) return refusal(response);
    const parsed: unknown = await response.json().catch(() => null);
    return { ok: true, body: parsed, status: response.status };
  } catch {
    return { ok: false, error: UNREACHABLE, status: 0 };
  }
}
