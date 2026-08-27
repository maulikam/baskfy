/**
 * The one comparison that can end a web session, kept apart from the layout that performs it.
 *
 * The session is an Auth.js JWT cookie: self-contained, thirty days, and `jwt` strategy is forced
 * because Auth.js v5 cannot use database sessions with the Credentials provider (`docs/08a` §3).
 * Nothing this server does can reach inside a token already in somebody's browser, so before
 * `app_user.session_epoch` there was no way to revoke one — sign-out deleted the browser's copy of
 * a credential that stayed good, and `revoke_all_for_user` revoked refresh-token rows the web app
 * has never read. A password change did not evict whoever prompted it (`NEEDS-MAULIK.md` §22).
 *
 * The epoch is a generation number: frozen into the session at sign-in, bumped by the API whenever
 * a password changes or an account is deleted, and compared here. It lives in its own module
 * because a security rule expressed as one line inside an async server component is a rule nobody
 * can test and nobody reviews — and the three edge cases below are exactly the kind that get
 * "simplified" into a `!==` by a later reader who has not thought about replication lag.
 */

/** What the session carries and what the server says it should carry. */
export interface EpochComparison {
  /**
   * The generation stamped into the session at sign-in. `undefined` for a cookie issued before
   * the epoch shipped.
   */
  stamped: number | undefined;
  /** `MeOut.session_epoch` — the generation the API says this account's sessions must carry. */
  current: number;
}

/**
 * The generation an unstamped session is treated as holding.
 *
 * Migration 0025 backfilled every existing account to 0, so reading a missing stamp as generation
 * zero agrees with the database rather than inventing a mismatch. The alternative — treating
 * "no stamp" as revoked, which is the instinctive safe reading — would sign the entire userbase
 * out the moment this deploys, and a security fix that logs everybody out teaches people that
 * being logged out is normal.
 */
const UNSTAMPED = 0;

/**
 * Whether this session has been revoked server-side.
 *
 * **`>` and not `!==`.** A stamp *ahead* of the server can only mean a rolled-back database or a
 * read replica that has not caught up. Evicting a legitimate session over replication lag is the
 * wrong way to be wrong: it is indistinguishable, to the person it happens to, from the bug this
 * whole mechanism exists to fix. Only the server having genuinely moved past this session — a
 * bump it performed — revokes it.
 *
 * The caller is responsible for not asking when the API could not be reached. `MeOut` being
 * absent means "unknown", and unknown is not revoked; see the call site in `(app)/layout.tsx`.
 */
export function isSessionRevoked({ stamped, current }: EpochComparison): boolean {
  return current > (stamped ?? UNSTAMPED);
}
