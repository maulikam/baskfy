import { SignJWT } from "jose";

/**
 * Minting the access token `services/api` verifies — docs/07 §header:
 *
 *     "Auth: `Authorization: Bearer <JWT>` issued by the Next.js app (HS256, shared secret,
 *      15-min access + refresh)"
 *
 * The verifier is `decile_api.auth.decode_token`, and it is deliberately strict: one algorithm,
 * `sub`/`iat`/`exp` all required, and a token whose `exp - iat` exceeds the configured ceiling is
 * refused *even when correctly signed*. So the claims below are not a suggestion — a token that
 * differs from this shape is a 401, and `apps/web/src/lib/auth/__tests__/jwt.test.ts` pins them.
 *
 * `sub` is the `app_user.public_id`, because that is what the API looks the account up by.
 */

/** docs/11 §Security: "JWT: HS256, 15-min access". */
export const ACCESS_TOKEN_TTL_SECONDS = 15 * 60;
export const JWT_ALGORITHM = "HS256";

/** RFC 7518 §3.2 — and `Settings.require_configured()` refuses to start below it. */
export const MIN_SECRET_BYTES = 32;

export interface AccessTokenClaims {
  /** `app_user.public_id`. */
  subject: string;
  issuedAt?: Date;
  issuer?: string | undefined;
  audience?: string | undefined;
}

export interface MintedToken {
  token: string;
  /** Epoch milliseconds, so the browser client knows when to ask for another. */
  expiresAt: number;
}

export function assertSecret(secret: string | undefined): string {
  if (!secret) {
    throw new Error("DECILE_JWT_SECRET is not set; the API would reject every token this app issues");
  }
  if (new TextEncoder().encode(secret).length < MIN_SECRET_BYTES) {
    throw new Error(
      `DECILE_JWT_SECRET is shorter than the ${MIN_SECRET_BYTES} bytes RFC 7518 §3.2 requires for HS256`,
    );
  }
  return secret;
}

export async function mintAccessToken(
  claims: AccessTokenClaims,
  secret: string | undefined,
): Promise<MintedToken> {
  const key = new TextEncoder().encode(assertSecret(secret));
  const issuedAt = claims.issuedAt ?? new Date();
  const issuedSeconds = Math.floor(issuedAt.getTime() / 1000);
  const expiresSeconds = issuedSeconds + ACCESS_TOKEN_TTL_SECONDS;

  let builder = new SignJWT({})
    .setProtectedHeader({ alg: JWT_ALGORITHM })
    .setSubject(claims.subject)
    .setIssuedAt(issuedSeconds)
    .setExpirationTime(expiresSeconds);

  if (claims.issuer) builder = builder.setIssuer(claims.issuer);
  if (claims.audience) builder = builder.setAudience(claims.audience);

  return { token: await builder.sign(key), expiresAt: expiresSeconds * 1000 };
}
