import "server-only";

import { createDecileClient, type DecileClient } from "@decile/api-client";

import { auth } from "@/lib/auth";
import { apiOrigin } from "@/lib/api/config";

/**
 * The API client for server components and route handlers — docs/03 §"Request path for a screen
 * run": "Next.js server component calls the API with the user's JWT."
 *
 * Created per request rather than module-scoped: the token is per user, and a module-scoped
 * client would capture whichever user rendered first. `server-only` makes importing this from a
 * client component a build error rather than a leaked token.
 */
export async function serverApi(): Promise<DecileClient> {
  const session = await auth();
  const token = session?.accessToken;
  return createDecileClient({
    baseUrl: apiOrigin(),
    ...(token ? { getAccessToken: () => token } : {}),
  });
}

/** An unauthenticated client, for the public pages that read `/meta/*` during SSG. */
export function publicApi(): DecileClient {
  return createDecileClient({ baseUrl: apiOrigin() });
}
