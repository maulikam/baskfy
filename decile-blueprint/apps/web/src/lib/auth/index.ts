import NextAuth, { type DefaultSession } from "next-auth";
import Credentials from "next-auth/providers/credentials";

import { baskfyAdapter } from "@/lib/auth/adapter";
import { ACCESS_TOKEN_TTL_SECONDS, mintAccessToken } from "@/lib/auth/jwt";
import { requestOtp, verifyOtp, verifyPassword } from "@/lib/auth/api-auth";

/**
 * Auth.js v5 — docs/02: "Auth.js v5 (credentials + email OTP), sessions in Postgres";
 * docs/11 §Security: "Argon2id password hashing; OTP login as the default path, password
 * optional. JWT: HS256, 15-min access."
 *
 * Three things are worth reading before changing anything here.
 *
 * 1. **The session strategy is `jwt`, and that is forced.** Auth.js v5 cannot use database
 *    sessions with the Credentials provider. docs/02 asks for credentials *and* Postgres
 *    sessions; those two cannot both hold. The Postgres adapter still carries the user records
 *    and the OTP verification tokens, which is the part the email flow needs. `docs/08a` §3.
 *
 * 2. **The access token is not the session cookie.** The cookie is Auth.js's own encrypted JWT;
 *    `session.accessToken` is a *separate* HS256 token minted for `services/api`, whose verifier
 *    (`baskfy_api.auth`) refuses anything longer-lived than fifteen minutes. It is re-minted
 *    inside the `jwt` callback whenever it is within a minute of expiring, so a long session
 *    never carries a stale bearer token.
 *
 * 3. **The credential checks are the API's.** Prompt 12 built `/auth/*`, so `api-auth.ts` posts
 *    to them and Argon2id verification happens where the hash lives (docs/11 §Security). The
 *    access token in the session is the one the API minted, not one this app derived — so the
 *    token the API verifies and the token it issued are the same object.
 */

declare module "next-auth" {
  interface Session {
    /** The HS256 bearer token `services/api` verifies. */
    accessToken?: string | undefined;
    /** Epoch milliseconds; the browser client refreshes shortly before this. */
    accessTokenExpiresAt?: number | undefined;
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
  /** Auth.js's own JWT is an open record; keeping the index signature makes this a *view* of it. */
  [claim: string]: unknown;
}

/** Re-mint once the token is within this margin of expiry (matches the browser client's). */
const REFRESH_MARGIN_MS = 60_000;

function secret(): string | undefined {
  return process.env.BASKFY_JWT_SECRET;
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  adapter: baskfyAdapter(),
  session: { strategy: "jwt", maxAge: 30 * 24 * 60 * 60 },
  trustHost: true,
  pages: { signIn: "/login", error: "/login" },
  providers: [
    Credentials({
      id: "password",
      name: "Email and password",
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      async authorize(raw) {
        const email = typeof raw?.email === "string" ? raw.email : "";
        const password = typeof raw?.password === "string" ? raw.password : "";
        if (!email || !password) return null;
        const account = await verifyPassword(email, password);
        if (!account) return null;
        return {
          id: account.publicId,
          email: account.email,
          name: account.name,
          accessToken: account.accessToken,
          accessTokenExpiresAt: account.accessTokenExpiresAt,
        };
      },
    }),
    /**
     * docs/11: "OTP login as the default path, password optional." Modelled as a second
     * credentials provider rather than Auth.js's Email provider because the code is entered in
     * the app (a six-digit OTP), not clicked in a magic link — and because the send half is
     * `POST /auth/request-otp` (docs/07), which is the server's job, not the client's.
     */
    Credentials({
      id: "otp",
      name: "Email OTP",
      credentials: {
        email: { label: "Email", type: "email" },
        code: { label: "Code", type: "text" },
      },
      async authorize(raw) {
        const email = typeof raw?.email === "string" ? raw.email : "";
        const code = typeof raw?.code === "string" ? raw.code : "";
        if (!email || !code) return null;
        const account = await verifyOtp(email, code);
        if (!account) return null;
        return {
          id: account.publicId,
          email: account.email,
          name: account.name,
          accessToken: account.accessToken,
          accessTokenExpiresAt: account.accessTokenExpiresAt,
        };
      },
    }),
  ],
  callbacks: {
    async jwt({ token, user }) {
      const claims: BaskfyToken = token;
      if (user?.id) {
        claims.publicId = user.id;
        // The API issued this on `/auth/login`; carrying it rather than minting a second one
        // means the token the API verifies is the token the API created.
        const authorised = user as { accessToken?: string; accessTokenExpiresAt?: number };
        if (authorised.accessToken) {
          claims.accessToken = authorised.accessToken;
          claims.accessTokenExpiresAt = authorised.accessTokenExpiresAt;
        }
      }
      const publicId = claims.publicId;
      if (!publicId) return token;

      const expiresAt = claims.accessTokenExpiresAt ?? 0;
      if (expiresAt - REFRESH_MARGIN_MS > Date.now()) return token;

      const minted = await mintAccessToken(
        {
          subject: publicId,
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
      return session;
    },
  },
});

export { requestOtp, ACCESS_TOKEN_TTL_SECONDS };
