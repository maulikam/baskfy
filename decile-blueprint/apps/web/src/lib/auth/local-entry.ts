/**
 * Where this Mac sends a signed-out visitor when `BASKFY_LOCAL_SIGNIN_PUBLIC_ID` is set.
 *
 * The account is named by the deployment, not chosen in a form, so there is no sign-in page.
 * A failed attempt stays on `/login` (`failed`) instead of redirecting forever.
 */

const FALLBACK = "/home";

export function localDestination(next: string | null | undefined): string {
  if (next && next.startsWith("/") && !next.startsWith("//")) return next;
  return FALLBACK;
}

/** null when the last attempt failed and the login page should stay put. */
export function localContinueHref(
  next: string | null | undefined,
  failed: boolean,
): string | null {
  if (failed) return null;
  return `/login/continue?next=${encodeURIComponent(localDestination(next))}`;
}
