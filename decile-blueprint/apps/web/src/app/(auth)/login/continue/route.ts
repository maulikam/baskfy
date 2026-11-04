import { NextResponse } from "next/server";
import { AuthError } from "next-auth";

import { signIn } from "@/lib/auth";
import { localSignInPublicId } from "@/lib/auth/api-auth";
import { localDestination } from "@/lib/auth/local-entry";

/**
 * Signs in as the account this Mac is configured as, then returns to `next`.
 * Registered only because `/login` is public; a website never has the env var, so this
 * redirects back to the Google form.
 */
export async function GET(request: Request): Promise<Response> {
  const url = new URL(request.url);
  if (!localSignInPublicId()) {
    return NextResponse.redirect(new URL("/login", url));
  }
  const dest = localDestination(url.searchParams.get("next"));
  try {
    await signIn("local", { redirectTo: dest });
  } catch (error) {
    if (error instanceof AuthError) {
      return NextResponse.redirect(new URL("/login?error=local", url));
    }
    throw error;
  }
  return NextResponse.redirect(new URL(dest, url));
}
