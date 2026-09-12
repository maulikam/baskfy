import NextAuth, { type DefaultSession } from "next-auth";
import Google from "next-auth/providers/google";

import { ACCESS_TOKEN_TTL_SECONDS, mintAccessToken } from "@/lib/auth/jwt";
import { exchangeGoogleIdToken } from "@/lib/auth/api-auth";

/**
 * Auth.js v5 — Google is the only provider (`docs/DECISIONS-MERGE.md` M46).
 *
 * WHAT THIS REPLACED
 * ------------------
 * Two `Credentials` providers: `password` (Argon2id, verified by the API) and `otp` (a six-digit
 * code mailed by the API). Both are gone, along with `/register`, `/verify-email`,
 * `/forgot-password` and `/reset-password`. The immediate cause was operational — SES sits in its
 * sandbox, so every verification mail to an unverified recipient was refused and five consecutive
 * sign-ups dead-ended — but the change stands on its own: five accounts existed and none had a
 * password set, so there was nothing to migrate and a whole category of things to stop getting
 * wrong.
 *
 * Four things are worth reading before changing anything here.
 *
 * 1. **The session strategy is `jwt`.** It was forced before, because Auth.js v5 cannot use
 *    database sessions with the Credentials provider. With Google it is now a choice, and it stays
 *    for the reason it always held in practice: `services/api` is the system of record for
 *    accounts, and a second session store would be a second answer to "who is this".
 *
 * 2. **There is no adapter any more.** `baskfyAdapter` existed to hold users and the OTP
 *    verification tokens. The API owns both facts, and the OTP tokens no longer exist, so the web
 *    app has stopped opening its own Postgres connection entirely. One less place that can read
 *    the account table.
 *
 * 3. **The access token is not the session cookie.** The cookie is Auth.js's own encrypted JWT;
 *    `session.accessToken` is a *separate* HS256 token minted by the API for itself, whose
 *    verifier (`baskfy_api.auth`) refuses anything longer-lived than fifteen minutes. It is
 *    re-minted inside the `jwt` callback whenever it is within a minute of expiring, so a long
 *    session never carries a stale bearer token.
 *
 * 4. **Google proves the identity; the API decides what it means.** This app never tells the API
 *    who signed in. It forwards the ID token and the API checks the signature, the audience and
 *    `email_verified` for itself — see `exchangeGoogleIdToken`. That keeps this app outside the
 *    trust boundary, where a front end belongs.
 */

declare module "next-auth" {
  interface Session {
    /** The HS256 bearer token `services/api` verifies. */
    accessToken?: string | undefined;
    /** Epoch milliseconds; the browser client refreshes shortly before this. */
    accessTokenExpiresAt?: number | undefined;
    /**
     * The generation this session was issued at. `(app)/layout.tsx` compares it against
     * `GET /me`'s `session_epoch` on every gated render; a session older than the server's
     * generation was revoked and must not render. This is the only kill switch a `jwt`-strategy
     * session has — see `NEEDS-MAULIK.md` §22.
     */
    sessionEpoch?: number | undefined;
    user: { publicId?: string | undefined } & DefaultSession["user"];
  }
}

/**
 * The claims we add to Auth.js's own session token. Declared as a local shape rather than through
 * `declare module "next-auth/jwt"`: in v5 the JWT type is re-exported from `@auth/core/jwt`, and
 * augmenting a module path that the installed package does not publish is a compile error that
 * says nothing useful about the actual contract.
 */
interface BaskfyToken {
  publicId?: string | undefined;
  accessToken?: string | undefined;
  accessTokenExpiresAt?: number | undefined;
  sessionEpoch?: number | undefined;
  /** Auth.js's own JWT is an open record; keeping the index signature makes this a *view* of it. */
  [claim: string]: unknown;
}

/** Re-mint once the token is within this margin of expiry (matches the browser client's). */
const REFRESH_MARGIN_MS = 60_000;

function secret(): string | undefined {
  return process.env.BASKFY_JWT_SECRET;
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  session: { strategy: "jwt", maxAge: 30 * 24 * 60 * 60 },
  trustHost: true,
  pages: { signIn: "/login", error: "/login" },
  providers: [
    Google({
      /* `?? ""` rather than `!`: `exactOptionalPropertyTypes` is on, and the honest reading of a
         missing client id is "this deployment has no Google credentials", not "trust me". An
         empty string fails at Google's authorize endpoint with a message that names the problem,
         which is a better failure than a non-null assertion that turns it into a runtime
         `undefined` somewhere deeper. `Settings.require_configured` refuses it outright in
         production, so this state can only exist on a laptop or in staging. */
      clientId: process.env.BASKFY_GOOGLE_CLIENT_ID ?? "",
      clientSecret: process.env.BASKFY_GOOGLE_CLIENT_SECRET ?? "",
      authorization: {
        params: {
          // `select_account` rather than the default. Without it, anyone with exactly one Google
          // session is signed straight back into the account they just signed out of, with no
          // visible step in between — which reads as "sign out is broken" rather than as SSO.
          prompt: "select_account",
          scope: "openid email profile",
        },
      },
    }),
  ],
  callbacks: {
    async jwt({ token, account }) {
      const claims: BaskfyToken = token;

      /* First call of a new sign-in: `account` carries what Google returned. This is the only
         moment the ID token exists, so it is the only moment the exchange can happen.

         A failed exchange THROWS rather than returning the token. Returning it would hand the
         browser a session cookie with no `publicId` — signed in as far as Auth.js is concerned,
         nobody as far as the API is concerned — and every gated page would then bounce the user
         through a redirect loop it could not explain. Throwing sends them to `pages.error`, which
         is `/login`, with nothing issued. */
      if (account?.id_token) {
        const exchanged = await exchangeGoogleIdToken(account.id_token);
        if (!exchanged) {
          throw new Error("The accounts service rejected that Google sign-in.");
        }
        claims.publicId = exchanged.publicId;
        claims.accessToken = exchanged.accessToken;
        claims.accessTokenExpiresAt = exchanged.accessTokenExpiresAt;
        /* Stamped once, at sign-in, and never refreshed. That is the whole point: if this were
           re-read from the API alongside the access token below, the session would silently adopt
           every bump the moment it saw one, and a revoked session would renew itself instead of
           dying. The number has to be frozen at issue for the comparison downstream to mean
           anything. */
        claims.sessionEpoch = exchanged.sessionEpoch;
        return token;
      }

      const publicId = claims.publicId;
      if (!publicId) return token;

      const expiresAt = claims.accessTokenExpiresAt ?? 0;
      if (expiresAt - REFRESH_MARGIN_MS > Date.now()) return token;

      /* AUDIT 0.8 / 2.2: re-minting used to ignore session_epoch, so a revoked cookie kept
         minting fresh 15-min bearers for thirty days. Ask /me with the about-to-expire token;
         a newer epoch (or a 401 from deleted_at) kills the session instead of renewing it. */
      const stamped = claims.sessionEpoch ?? 0;
      const origin = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
      if (origin && claims.accessToken) {
        const meResponse = await fetch(`${origin}/api/v1/me`, {
          headers: { Authorization: `Bearer ${claims.accessToken}` },
          cache: "no-store",
        });
        if (!meResponse.ok) {
          throw new Error("The accounts service refused this session.");
        }
        const me = (await meResponse.json()) as { session_epoch?: number };
        if (typeof me.session_epoch === "number" && me.session_epoch > stamped) {
          throw new Error("This session has been revoked.");
        }
      }

      const minted = await mintAccessToken(
        {
          subject: publicId,
          epoch: stamped,
          issuer: process.env.BASKFY_JWT_ISSUER,
          audience: process.env.BASKFY_JWT_AUDIENCE,
        },
        secret(),
      );
      claims.accessToken = minted.token;
      claims.accessTokenExpiresAt = minted.expiresAt;
      return token;
    },

    session({ session, token }) {
      const claims: BaskfyToken = token;
      session.user.publicId = claims.publicId;
      session.accessToken = claims.accessToken;
      session.accessTokenExpiresAt = claims.accessTokenExpiresAt;
      session.sessionEpoch = claims.sessionEpoch;
      return session;
    },
  },
});

export { ACCESS_TOKEN_TTL_SECONDS };
