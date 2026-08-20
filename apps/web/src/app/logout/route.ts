import { signOut } from "@/lib/auth";

/**
 * A GET route rather than a form post, because the user menu is a link. Auth.js clears the session
 * cookie and redirects; there is no state of our own to tear down, since the access token lives
 * only inside that cookie's JWT.
 */
export async function GET() {
  await signOut({ redirectTo: "/" });
}
