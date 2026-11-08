import "server-only";

import { createBaskfyClient, type BaskfyClient } from "@baskfy/api-client";

import { auth } from "@/lib/auth";
import { serverApiOrigin } from "@/lib/api/config";
import { timedFetch } from "@/lib/api/server-fetch";
import { currentTraceparent } from "@/lib/api/trace";

/**
 * The API client for server components and route handlers — docs/03 §"Request path for a screen
 * run": "Next.js server component calls the API with the user's JWT."
 *
 * Created per request rather than module-scoped: the token is per user, and a module-scoped
 * client would capture whichever user rendered first. `server-only` makes importing this from a
 * client component a build error rather than a leaked token.
 *
 * Tree-5 RSC perf: every hop uses {@link timedFetch} so a slow/unreachable API cannot stall an
 * RSC navigation for tens of seconds (observed 5–20s on /portfolios et al.).
 */
export async function serverApi(): Promise<BaskfyClient> {
  const session = await auth();
  const token = session?.accessToken;
  return createBaskfyClient({
    baseUrl: serverApiOrigin(),
    fetch: timedFetch(),
    ...(token ? { getAccessToken: () => token } : {}),
    // Prompt 17 §1: carry the render's span across the hop, so a slow page and the screen query
    // behind it are one trace in Tempo rather than two.
    getTraceparent: currentTraceparent,
  });
}

/** An unauthenticated client, for the public pages that read `/meta/*` during SSG. */
export function publicApi(): BaskfyClient {
  return createBaskfyClient({
    baseUrl: serverApiOrigin(),
    fetch: timedFetch(),
    getTraceparent: currentTraceparent,
  });
}
