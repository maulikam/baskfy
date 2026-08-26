"use server";

import { AuthError } from "next-auth";

import { signIn } from "@/lib/auth";

/**
 * The one server action the sign-in page needs.
 *
 * Everything else that lived here — `signInWithPassword`, `sendOtp`, `register`, `sendResetLink`,
 * `chooseNewPassword`, `signInWithOtp` — went with the endpoints behind it when Google became the
 * only way in (`docs/DECISIONS-MERGE.md` M46).
 *
 * A server action rather than a client `signIn()` call, for the reason it always was: a form that
 * posts to a server action still works with JavaScript disabled, which for a sign-in page is the
 * difference between a degraded experience and no way in at all.
 */

export interface FormResult {
  ok: boolean;
  message: string;
}

/**
 * Where a sign-in with no `?next=` lands.
 *
 * `/home`, which is what `lib/nav.ts` calls the first destination: "What you hold, what needs a
 * decision, and what is worth a look." `/build` was right when the screener was the whole
 * product, and stopped being right when SC9 gave the product a landing surface.
 */
const DEFAULT_DESTINATION = "/home";

/**
 * `FormData.get` returns `string | File | null`. Stringifying a `File` yields "[object File]",
 * so a non-string is read as absent.
 */
function field(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value : "";
}

/**
 * Where to send someone after signing in.
 *
 * `?next=` is echoed back by the middleware's gate, and is validated the same way it is there:
 * a relative path only. An unvalidated `next` is an open redirect, which is a phishing primitive.
 */
function destination(formData: FormData): string {
  const next = field(formData, "next");
  if (next.startsWith("/") && !next.startsWith("//")) return next;
  return DEFAULT_DESTINATION;
}

/**
 * Hand off to Google.
 *
 * This never returns on the happy path: `signIn` throws a redirect, Next completes the
 * navigation, and the user comes back through `/api/auth/callback/google`. The `FormResult` exists
 * for the case where Auth.js refuses before it can redirect — a missing client id, most likely,
 * which is what an unconfigured deployment looks like from here.
 */
export async function signInWithGoogle(
  _previous: FormResult | null,
  formData: FormData,
): Promise<FormResult> {
  try {
    await signIn("google", { redirectTo: destination(formData) });
    return { ok: true, message: "Signed in." };
  } catch (error) {
    if (error instanceof AuthError) {
      return { ok: false, message: "We could not reach Google sign-in. Try again in a moment." };
    }
    // A redirect is thrown, not returned; re-throwing lets Next complete the navigation.
    throw error;
  }
}
