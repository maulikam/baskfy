import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * The one write `/vbt` has — "Scan now", and nothing else, ever.
 *
 * `fetch.ts` next door says in its own header that there is no write helper in it "and there is
 * not going to be one". That sentence is still true of *that* file and still true of the claim it
 * was making: `docs/vbt/02` Track C §4 gives the web app no route under `/vbt` that can reach the
 * gateway, and a volume-breakout line still becomes an order in the desk console, on a click
 * Maulik makes, and nowhere else. **Queuing a detector is not that.** A scan reads bars and
 * writes detection rows; it can no more buy a share than the nightly job can.
 *
 * So this file exists beside the read helper rather than inside it, and it is deliberately the
 * narrowest thing that can work:
 *
 *  - **one path, as a closed union type rather than a pattern.** A pattern that admitted
 *    `/vbt/scan` would admit the desk's confirm route as well, and that route is exactly what
 *    Track C §4 keeps out of this application. The type is the guard; the test is the reminder.
 *  - **one method.** POST. There is nothing here to PATCH and nothing to DELETE.
 *  - **the bearer never leaves the server.** The token comes from the session here, so no action
 *    and no component ever holds one.
 *  - **it never throws.** A form gets a result either way, because a button that silently does
 *    nothing is the failure mode a person retries until they have queued six runs.
 *
 * The status is returned rather than the server's sentence, on purpose — see `../../app/(app)/vbt/copy.ts`.
 */

/** The only path this application may write to under `/vbt`. Widening it is a deliberate act. */
export type VbtWritePath = "/vbt/scan";

/** What a form gets back. The sentence is chosen by this app's copy, never by the wire. */
export type VbtScanResult =
  | { readonly ok: true; readonly message: string }
  | { readonly ok: false; readonly error: string };

/**
 * The raw outcome: whether the server took it, and the status it answered with.
 *
 * `status` is `0` when the service could not be reached at all — a distinct case from any answer
 * it could have given, and the one where "nothing changed" is most worth saying out loud.
 */
export type VbtWriteOutcome =
  | { readonly ok: true; readonly status: number; readonly body: unknown }
  | { readonly ok: false; readonly status: number };

function timeoutMs(): number {
  const parsed = Number.parseInt(process.env.BASKFY_SERVER_FETCH_TIMEOUT_MS?.trim() || "2500", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 2500;
}

/** One request, with the session's bearer. Never throws: the form gets a result either way. */
export async function vbtWrite(path: VbtWritePath): Promise<VbtWriteOutcome> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return { ok: false, status: 401 };
  try {
    const response = await fetch(`${serverApiOrigin()}/api/v1${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs()),
    });
    if (!response.ok) return { ok: false, status: response.status };
    const parsed: unknown = await response.json().catch(() => null);
    return { ok: true, status: response.status, body: parsed };
  } catch {
    return { ok: false, status: 0 };
  }
}
