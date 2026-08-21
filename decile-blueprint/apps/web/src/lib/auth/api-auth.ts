import "server-only";

/**
 * The bridge from Auth.js to `services/api`'s `/auth/*` endpoints — docs/07 §"Account & billing".
 *
 * Prompt 8 shipped this as `stub-endpoints.ts`, calling endpoints that did not exist yet and
 * falling back to a development stub. Prompt 12 built them, so the stub is gone: `verifyPassword`
 * and `verifyOtp` now return an account only when the API says so, and the Argon2id verification
 * happens where the hash lives.
 *
 * What is *not* here is session issuance. The API mints its own access and refresh tokens on
 * `/auth/login`, and Auth.js mints the cookie session; the web app uses the API's access token as
 * `session.accessToken` and lets Auth.js own the browser session. Two refresh mechanisms would be
 * two things to expire, so the API's refresh cookie is used by the *browser* directly on
 * `/auth/refresh` and the server-rendered pages read the Auth.js session. `docs/12a` §5.
 */
import { apiOrigin } from "@/lib/api/config";

export interface Account {
  publicId: string;
  email: string;
  name: string | null;
  accessToken: string;
  accessTokenExpiresAt: number;
}

/** The `SessionOut` shape `services/api` returns from `/auth/login` and `/auth/verify-otp`. */
interface SessionPayload {
  access_token: string;
  expires_in: number;
  public_id: string;
  email: string;
  name: string | null;
}

const MILLISECONDS = 1000;

async function post(path: string, body: unknown): Promise<Response | null> {
  try {
    return await fetch(`${apiOrigin()}/api/v1${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    // The API being unreachable is a failed sign-in, not a successful one.
    return null;
  }
}

function toAccount(payload: SessionPayload): Account {
  return {
    publicId: payload.public_id,
    email: payload.email,
    name: payload.name,
    accessToken: payload.access_token,
    accessTokenExpiresAt: Date.now() + payload.expires_in * MILLISECONDS,
  };
}

async function authenticate(path: string, body: unknown): Promise<Account | null> {
  const response = await post(path, body);
  if (!response?.ok) return null;
  return toAccount((await response.json()) as SessionPayload);
}

export async function verifyPassword(email: string, password: string): Promise<Account | null> {
  return authenticate("/auth/login", { email, password });
}

export async function verifyOtp(email: string, code: string): Promise<Account | null> {
  return authenticate("/auth/verify-otp", { email, code });
}

export interface AcceptedResult {
  /** True when the API accepted the request. It says nothing about whether the address exists. */
  accepted: boolean;
  /** The API's own neutral wording, so the UI does not invent a second version of it. */
  detail: string;
}

const UNAVAILABLE = "We could not reach the accounts service. Try again in a moment.";

async function accepted(path: string, body: unknown): Promise<AcceptedResult> {
  const response = await post(path, body);
  if (!response) return { accepted: false, detail: UNAVAILABLE };
  if (!response.ok) {
    const problem = (await response.json().catch(() => null)) as { detail?: string } | null;
    return { accepted: false, detail: problem?.detail ?? UNAVAILABLE };
  }
  const payload = (await response.json()) as { detail?: string };
  return { accepted: true, detail: payload.detail ?? "Check your inbox." };
}

export async function requestOtp(email: string): Promise<AcceptedResult> {
  return accepted("/auth/request-otp", { email });
}

export async function registerAccount(input: {
  email: string;
  password?: string | undefined;
  name?: string | undefined;
  acceptTerms: boolean;
  acceptMarketing: boolean;
}): Promise<AcceptedResult> {
  return accepted("/auth/register", {
    email: input.email,
    password: input.password || undefined,
    name: input.name || undefined,
    accept_terms: input.acceptTerms,
    accept_marketing: input.acceptMarketing,
  });
}

export async function forgotPassword(email: string): Promise<AcceptedResult> {
  return accepted("/auth/forgot-password", { email });
}

export async function resetPassword(token: string, password: string): Promise<AcceptedResult> {
  const response = await post("/auth/reset-password", { token, password });
  if (!response) return { accepted: false, detail: UNAVAILABLE };
  if (!response.ok) {
    const problem = (await response.json().catch(() => null)) as { detail?: string } | null;
    return { accepted: false, detail: problem?.detail ?? "That reset link is not valid." };
  }
  return { accepted: true, detail: "Your password has been changed." };
}

export async function confirmEmail(token: string): Promise<AcceptedResult> {
  return accepted("/auth/verify-email", { token });
}
