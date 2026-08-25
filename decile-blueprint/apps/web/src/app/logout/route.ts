import { signOut } from "@/lib/auth";

/**
 * A GET route rather than a form post, because the user menu is a link. Auth.js clears the session
 * cookie and redirects; there is no state of our own to tear down, since the access token lives
 * only inside that cookie's JWT.
 *
 * `redirectTo: "/"` is doing more work than it looks like. It is a **document** navigation, not a
 * client-side one, so Next's router cache — which still holds the RSC payloads of every gated page
 * visited this session — is discarded with the old document. Combined with `no-store` on those
 * pages (`src/middleware.ts`), that leaves the browser with nothing signed-in to go back to: the
 * Back button issues a real request, the request carries no cookie, and the gate answers `/login`.
 *
 * `force-dynamic` because a cached sign-out is a sign-out that happens once.
 */
export const dynamic = "force-dynamic";

export async function GET() {
  await signOut({ redirectTo: "/" });
}
