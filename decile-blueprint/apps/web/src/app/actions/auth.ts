"use server";

import { AuthError } from "next-auth";

import { signIn } from "@/lib/auth";
import {
  forgotPassword,
  registerAccount,
  requestOtp,
  resetPassword,
} from "@/lib/auth/api-auth";

/**
 * Server actions for the placeholder auth pages — Prompt 8 deliverable 5.
 *
 * Server actions rather than client fetches, for two reasons that both matter here. The API
 * origin and the session cookie are server-side concerns, and a form that posts to a server
 * action still works with JavaScript disabled — which for a sign-in page is the difference
 * between a degraded experience and no way in at all.
 */

export interface FormResult {
  ok: boolean;
  message: string;
}

const GENERIC_FAILURE = "Those details did not match an account.";

/** Mirrors `decile_api.security.MIN_PASSWORD_LENGTH`, so the form says so before the round trip. */
const MIN_PASSWORD_LENGTH = 8;

/** Where a sign-in with no `?next=` lands. */
const DEFAULT_DESTINATION = "/screens";

/**
 * `FormData.get` returns `string | File | null`. Stringifying a `File` yields "[object File]",
 * which would be silently treated as an email address, so a non-string is read as absent.
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

export async function signInWithPassword(
  _previous: FormResult | null,
  formData: FormData,
): Promise<FormResult> {
  const email = field(formData, "email");
  const password = field(formData, "password");
  if (!email || !password) return { ok: false, message: "Enter your email and password." };

  try {
    await signIn("password", { email, password, redirectTo: destination(formData) });
    return { ok: true, message: "Signed in." };
  } catch (error) {
    if (error instanceof AuthError) return { ok: false, message: GENERIC_FAILURE };
    // A redirect is thrown, not returned; re-throwing lets Next complete the navigation.
    throw error;
  }
}

export async function sendOtp(
  _previous: FormResult | null,
  formData: FormData,
): Promise<FormResult> {
  const email = field(formData, "email");
  if (!email) return { ok: false, message: "Enter your email address." };

  const result = await requestOtp(email);
  return { ok: result.accepted, message: result.detail };
}

export async function register(
  _previous: FormResult | null,
  formData: FormData,
): Promise<FormResult> {
  const email = field(formData, "email");
  const password = field(formData, "password");
  if (!email) return { ok: false, message: "Enter your email address." };
  if (formData.get("accept_terms") !== "on") {
    return { ok: false, message: "Please accept the Terms and the Privacy Policy." };
  }

  const result = await registerAccount({
    email,
    password: password || undefined,
    name: field(formData, "name") || undefined,
    acceptTerms: true,
    acceptMarketing: formData.get("accept_marketing") === "on",
  });
  return { ok: result.accepted, message: result.detail };
}

export async function sendResetLink(
  _previous: FormResult | null,
  formData: FormData,
): Promise<FormResult> {
  const email = field(formData, "email");
  if (!email) return { ok: false, message: "Enter your email address." };
  const result = await forgotPassword(email);
  return { ok: result.accepted, message: result.detail };
}

export async function chooseNewPassword(
  _previous: FormResult | null,
  formData: FormData,
): Promise<FormResult> {
  const token = field(formData, "token");
  const password = field(formData, "password");
  if (!token) return { ok: false, message: "That reset link is incomplete." };
  if (password.length < MIN_PASSWORD_LENGTH) {
    return { ok: false, message: `Passwords must be at least ${MIN_PASSWORD_LENGTH} characters.` };
  }
  const result = await resetPassword(token, password);
  return { ok: result.accepted, message: result.detail };
}

export async function signInWithOtp(
  _previous: FormResult | null,
  formData: FormData,
): Promise<FormResult> {
  const email = field(formData, "email");
  const code = field(formData, "code");
  if (!email || !code) return { ok: false, message: "Enter your email and the six-digit code." };

  try {
    await signIn("otp", { email, code, redirectTo: destination(formData) });
    return { ok: true, message: "Signed in." };
  } catch (error) {
    if (error instanceof AuthError) return { ok: false, message: "That code is not valid." };
    throw error;
  }
}
