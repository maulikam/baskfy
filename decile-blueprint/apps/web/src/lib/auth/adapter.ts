import "server-only";

import type { Adapter, AdapterUser, VerificationToken } from "@auth/core/adapters";
import { Pool } from "pg";

/**
 * A Postgres adapter over Decile's own schema — docs/02 §"The decision in one table":
 * "Auth.js v5 (credentials + email OTP), sessions in Postgres".
 *
 * Why not `@auth/pg-adapter`
 * -------------------------
 * That package owns its own four tables (`users`, `accounts`, `sessions`, `verification_token`).
 * docs/04 already defines the account of record as `app_user`, and `services/api` looks a bearer
 * token's `sub` up in it. Two user tables would mean two answers to "who is this", so this adapter
 * maps Auth.js onto the table docs/04 defines and adds exactly one more —
 * `auth_verification_token`, for the email OTP codes (`docs/04c`).
 *
 * Why the session methods are absent
 * ----------------------------------
 * **Auth.js v5 does not support database sessions with the Credentials provider** — it requires
 * `strategy: "jwt"`. docs/02 asks for both credentials *and* Postgres sessions, and those two
 * cannot both be true; see `docs/08a` §3. The session cookie is therefore a JWT, and the Postgres
 * half of the adapter carries the user records and the OTP tokens, which is the part the email
 * flow actually needs.
 */

let pool: Pool | null = null;

function connection(): Pool {
  const connectionString = process.env.BASKFY_DATABASE_URL_PG ?? process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error(
      "BASKFY_DATABASE_URL_PG is not set; it is the SQLAlchemy URL with the +asyncpg driver removed",
    );
  }
  pool ??= new Pool({ connectionString, max: 4 });
  return pool;
}

interface AppUserRow {
  public_id: string;
  email: string;
  name: string | null;
  email_verified_at: Date | null;
}

function toAdapterUser(row: AppUserRow): AdapterUser {
  return {
    id: row.public_id,
    email: row.email,
    name: row.name,
    emailVerified: row.email_verified_at,
  };
}

async function findUser(where: "public_id" | "email", value: string): Promise<AdapterUser | null> {
  const { rows } = await connection().query<AppUserRow>(
    `SELECT public_id, email, name, email_verified_at FROM app_user WHERE ${where} = $1 LIMIT 1`,
    [value],
  );
  const row = rows[0];
  return row ? toAdapterUser(row) : null;
}

/** 12 hex characters, matching the `public_id` shape `services/api` generates. */
function newPublicId(): string {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function baskfyAdapter(): Adapter {
  return {
    async createUser(user) {
      const { rows } = await connection().query<AppUserRow>(
        `INSERT INTO app_user (public_id, email, name, email_verified_at)
         VALUES ($1, $2, $3, $4)
         ON CONFLICT (email) DO UPDATE SET name = COALESCE(EXCLUDED.name, app_user.name)
         RETURNING public_id, email, name, email_verified_at`,
        [newPublicId(), user.email, user.name ?? null, user.emailVerified ?? null],
      );
      const row = rows[0];
      if (!row) throw new Error("app_user insert returned no row");
      return toAdapterUser(row);
    },

    async getUser(id) {
      return findUser("public_id", id);
    },

    async getUserByEmail(email) {
      return findUser("email", email);
    },

    async updateUser(user) {
      const { rows } = await connection().query<AppUserRow>(
        `UPDATE app_user
            SET name = COALESCE($2, name),
                email_verified_at = COALESCE($3, email_verified_at)
          WHERE public_id = $1
      RETURNING public_id, email, name, email_verified_at`,
        [user.id, user.name ?? null, user.emailVerified ?? null],
      );
      const row = rows[0];
      if (!row) throw new Error(`no app_user with public_id ${user.id}`);
      return toAdapterUser(row);
    },

    async createVerificationToken(token) {
      await connection().query(
        `INSERT INTO auth_verification_token (identifier, token, expires)
         VALUES ($1, $2, $3)
         ON CONFLICT (identifier, token) DO UPDATE SET expires = EXCLUDED.expires`,
        [token.identifier, token.token, token.expires],
      );
      return token;
    },

    /**
     * Single-use by construction: the DELETE is the check. A SELECT-then-DELETE would let two
     * requests with the same code both succeed.
     */
    async useVerificationToken({ identifier, token }) {
      const { rows } = await connection().query<VerificationToken>(
        `DELETE FROM auth_verification_token
           WHERE identifier = $1 AND token = $2
       RETURNING identifier, token, expires`,
        [identifier, token],
      );
      return rows[0] ?? null;
    },
  };
}
