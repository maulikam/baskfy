import "server-only";

/**
 * The bridge from Auth.js to `services/api`'s `/auth/*` endpoints — docs/07 §"Account & billing".
 *
 * There is exactly one call left. Google sign-in replaced registration, the email OTP and the
 * password (`docs/DECISIONS-MERGE.md` M46), so `verifyPassword`, `verifyOtp`, `requestOtp`,
 * `registerAccount`, `forgotPassword`, `resetPassword` and `confirmEmail` are all gone along with
 * the endpoints behind them.
 *
 * What is *not* here is session issuance. The API mints its own access and refresh tokens and
 * Auth.js mints the cookie session; the web app carries the API's access token as
 * `session.accessToken` and lets Auth.js own the browser session. Two refresh mechanisms would be
 * two things to expire, so the API's refresh cookie is used by the *browser* directly on
 * `/auth/refresh` and the server-rendered pages read the Auth.js session. `docs/12a` §5.
 */
import { serverApiOrigin } from "@/lib/api/config";

export interface Account {
  publicId: string;
  email: string;
  name: string | null;
  accessToken: string;
  accessTokenExpiresAt: number;
  /**
   * The generation this session must carry. Stamped into the Auth.js token and compared against
   * `GET /me` on every gated render — see `src/app/(app)/layout.tsx`. The API bumps it whenever an
   * account is deleted, which is the only way a JWT session can be killed before its thirty days
   * are up (`NEEDS-MAULIK.md` §22).
   */
  sessionEpoch: number;
}

/** The `SessionOut` shape `services/api` returns from `/auth/google`. */
interface SessionPayload {
  access_token: string;
  expires_in: number;
  public_id: string;
  email: string;
  name: string | null;
  session_epoch?: number;
}

const MILLISECONDS = 1000;

async function post(path: string, body: unknown): Promise<Response | null> {
  try {
    return await fetch(`${serverApiOrigin()}/api/v1${path}`, {
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
    /* `?? 0` rather than a required field: an API deployed before the epoch existed omits it, and
       0 is the generation every pre-existing account was backfilled to (migration 0025). Reading
       a missing value as "generation zero" therefore agrees with the database rather than
       inventing a mismatch that would sign the user straight back out. */
    sessionEpoch: payload.session_epoch ?? 0,
  };
}

/**
 * Exchange Google's ID token for an API session.
 *
 * **The token is forwarded verbatim and nothing else is sent.** Auth.js has already validated it
 * once, but this app is not the security boundary: the API checks Google's signature, our client
 * id in `aud`, and `email_verified` itself (`baskfy_api.auth_google`). If this function posted the
 * profile Auth.js parsed instead, a bug anywhere in this app would become "sign in as anyone" —
 * so the *only* thing the API is told is the thing Google signed.
 *
 * `null` means no session: a rejected token and an unreachable API are the same answer here,
 * because neither is a sign-in.
 */
export async function exchangeGoogleIdToken(idToken: string): Promise<Account | null> {
  const response = await post("/auth/google", { id_token: idToken });
  if (!response?.ok) return null;
  return toAccount((await response.json()) as SessionPayload);
}
