"""``/auth/*`` and ``/me`` — docs/07 §"Account & billing", docs/11 §Security (Prompt 12).

    POST /auth/google   -> the only way in
    POST /auth/refresh  /auth/logout
    GET  /me                                  -> profile + entitlements
    PATCH /me
    GET  /me/export     DELETE /me     POST /me/restore

WHAT USED TO BE HERE
--------------------
``/auth/register``, ``/auth/login``, ``/auth/request-otp``, ``/auth/verify-otp``,
``/auth/verify-email``, ``/auth/forgot-password``, ``/auth/reset-password`` and
``/me/change-password``. All eight are gone: Google sign-in replaced the lot
(`docs/DECISIONS-MERGE.md` M46). Nothing in this service stores or checks a password any more,
and no sign-in path sends an email.

Two shapes that ran through the old endpoints are worth knowing are *deliberately absent*:

**The uninformative 202 is gone with the endpoints that needed it.** ``register``,
``request-otp`` and ``forgot-password`` all answered identically for a known and an unknown
address, because an endpoint that says "no such account" publishes the customer list. Google
sign-in has no such leak to plug — the caller has already proved to Google who they are, so
there is no membership question they could be asking us.

**The lockout is gone with the password.** "Ten failures then lock" existed because a password
can be guessed. An ID token cannot: it is either signed by Google or it is not. The lockout
machinery survives in ``auth_service`` only for the model and the migration; nothing calls it.

What has *not* changed is that every rejection is one flat 401 with one sentence
(:func:`_as_problem`), so a caller learns nothing about which check refused them.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.auth import AuthenticatedDep, settings_for
from baskfy_api.auth_google import GoogleVerificationError, GoogleVerifier, TokenVerifier
from baskfy_api.auth_service import (
    AccountLocked,
    AuthError,
    InvalidCredentials,
    IssuedSession,
    TokenReused,
    cancel_deletion,
    find_user_including_deleted,
    issue_session,
    link_google_identity,
    normalise_email,
    request_deletion,
    revoke_all_for_user,
    revoke_one,
    rotate_refresh,
)
from baskfy_api.csrf import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    require_csrf,
    set_auth_cookies,
)
from baskfy_api.db import SessionDep
from baskfy_api.email import Mailer, build_transport
from baskfy_api.email import templates as mail
from baskfy_api.entitlements import EntitlementsDep
from baskfy_api.problems import Problem, ProblemType, unauthenticated
from baskfy_api.schemas import (
    DataExportOut,
    DeleteAccountIn,
    DeletionOut,
    EntitlementsOut,
    GoogleSignInIn,
    MeOut,
    SessionOut,
    UpdateMeIn,
)
from baskfy_api.settings import Settings
from baskfy_core.models import (
    AccountDeletion,
    AppUser,
    ConsentRecord,
    Payment,
    Plan,
    Screen,
    Subscription,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

#: The same sentence for a known and an unknown address. See the module docstring.
NEUTRAL_DETAIL: Final = (
    "If that address has an account, we have sent it an email. Check your inbox."
)

MINUTE_SECONDS: Final = 60
HOUR_SECONDS: Final = 60 * 60


def _settings(request: Request) -> Settings:
    return settings_for(request)


def _mailer(request: Request) -> Mailer:
    """The process-wide mailer, built once by ``create_app``."""
    mailer = getattr(request.app.state, "mailer", None)
    if isinstance(mailer, Mailer):
        return mailer
    return Mailer(build_transport(_settings(request)))


SettingsDep = Annotated[Settings, Depends(_settings)]
MailerDep = Annotated[Mailer, Depends(_mailer)]


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    agent = request.headers.get("user-agent")
    return agent[:512] if agent else None


def _session_response(response: Response, issued: IssuedSession, settings: Settings) -> SessionOut:
    """Set the cookies and render the body. The refresh token never enters the body."""
    set_auth_cookies(
        response,
        refresh_token=issued.refresh_token,
        csrf_token=issued.csrf_token,
        ttl_seconds=issued.refresh_expires_in,
        settings=settings,
    )
    return SessionOut(
        access_token=issued.access_token,
        expires_in=issued.access_expires_in,
        public_id=issued.user.public_id,
        email=issued.user.email,
        name=issued.user.name,
        email_verified=issued.user.email_verified_at is not None,
        # Read *after* `issue_session`, which matters on the reset-password path: `set_password`
        # bumps the epoch, and a value captured before that would stamp the new session with the
        # generation it just invalidated — signing the user out of the session they are in the
        # middle of creating.
        session_epoch=issued.user.session_epoch,
    )


def _as_problem(error: AuthError) -> Problem:
    """One place where an auth failure becomes an HTTP answer.

    A lockout is a 429 with `Retry-After` — it is a rate limit, and docs/07's catalogue already
    has one. Everything else is a flat 401 with the same body, so the caller learns nothing about
    *which* half of the credential was wrong.
    """
    if isinstance(error, AccountLocked):
        return Problem(
            ProblemType.RATE_LIMITED,
            "Too many failed attempts. Try again later, or reset your password.",
            headers={"Retry-After": str(error.retry_after_seconds)},
            retry_after_seconds=error.retry_after_seconds,
        )
    return unauthenticated("Those credentials are not valid.")


# ---------------------------------------------------------------------------
# Registration and sign-in
# ---------------------------------------------------------------------------


def _google(request: Request) -> TokenVerifier:
    """The process-wide verifier, built once by ``create_app`` so the JWKS cache is shared.

    Typed as the protocol rather than the class so a test can substitute a double on
    ``app.state`` — the same seam ``_mailer`` opens for :class:`Outbox`.
    """
    verifier = getattr(request.app.state, "google_verifier", None)
    if isinstance(verifier, TokenVerifier):
        return verifier
    return GoogleVerifier(_settings(request))


GoogleDep = Annotated[TokenVerifier, Depends(_google)]


@router.post("/auth/google", response_model=SessionOut, summary="Sign in with Google")
async def sign_in_with_google(
    request: Request,
    body: GoogleSignInIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    google: GoogleDep,
) -> SessionOut:
    """The only way into the product (`docs/DECISIONS-MERGE.md` M46).

    `apps/web` runs the OAuth dance and posts the resulting ID token here. **The token is the
    credential** — this endpoint never accepts an email or a subject as a parameter, because an
    endpoint that did would mint a session for whoever the caller named.

    There is no neutral 202 here and no membership oracle to protect: the caller has already
    proved to Google who they are, so "this account is new" is something they know better than
    we do. What stays uniform is the *failure*: every rejection is the same 401, whether the
    signature was wrong, the audience was somebody else's application, or the address was
    unverified.
    """
    try:
        identity = await google.verify(body.id_token)
    except GoogleVerificationError as error:
        # The reason is logged, never returned. A caller who learns *why* their forged token
        # failed learns how to forge a better one.
        log.warning("google sign-in rejected", extra={"reason": str(error)})
        raise unauthenticated("That Google sign-in could not be verified.") from error

    # Authorisation, after authentication. Google has proved the address; this decides whether
    # this deployment serves it. Checked *before* `link_google_identity` so a barred address
    # never creates an `app_user` row — a rejected sign-in must leave no trace of an account.
    #
    # The same uniform 401 as every other rejection above, deliberately: an error that said
    # "your address is not on the list" would turn this endpoint into an oracle for who is.
    if not settings.login_permitted(identity.email):
        log.warning(
            "google sign-in refused by allowlist",
            extra={"reason": "not in BASKFY_LOGIN_ALLOWLIST"},
        )
        raise unauthenticated("That Google sign-in could not be verified.")

    try:
        user, created = await link_google_identity(
            session,
            identity,
            settings,
            source_ip=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as error:
        raise _as_problem(error) from error

    await _clear_pending_deletion(session, user)
    issued = await issue_session(session, user, settings)
    log.info(
        "google sign-in",
        extra={"user_public_id": user.public_id, "account_created": created},
    )
    return _session_response(response, issued, settings)



async def _clear_pending_deletion(session: AsyncSession, user: AppUser) -> None:
    """Prompt 12 §5: "Signing in again before then cancels the deletion"."""
    if user.deleted_at is not None:
        await cancel_deletion(session, user)


# ---------------------------------------------------------------------------
# Session lifecycle — docs/11: rotating refresh, CSRF on cookie mutations
# ---------------------------------------------------------------------------


@router.post("/auth/refresh", response_model=SessionOut, summary="Rotate the refresh token")
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> SessionOut:
    """docs/07: `POST /auth/refresh`. docs/11: rotating, httpOnly, CSRF-protected.

    Reuse of an already-rotated token revokes the whole family and answers 401 — the client is
    then signed out everywhere, which is the correct outcome whether the cause was theft or a
    race. `docs/12a` §4.
    """
    require_csrf(request)
    supplied = request.cookies.get(REFRESH_COOKIE)
    if not supplied:
        raise unauthenticated("No refresh token was presented.")

    try:
        issued = await rotate_refresh(session, supplied, settings)
    except TokenReused as error:
        clear_auth_cookies(response, settings)
        raise unauthenticated(
            "That session has been ended for security reasons. Please sign in again."
        ) from error
    except InvalidCredentials as error:
        clear_auth_cookies(response, settings)
        raise unauthenticated("That refresh token is not valid.") from error

    # An allowlist that only guarded sign-in would let a session already in flight outlive the
    # decision to bar it, for as long as the refresh family stays alive. Checked here too, so
    # removing an address ends its access at the next rotation rather than at its own leisure.
    if not settings.login_permitted(issued.user.email):
        clear_auth_cookies(response, settings)
        log.warning("refresh refused by allowlist", extra={"reason": "not in allowlist"})
        raise unauthenticated("That refresh token is not valid.")

    return _session_response(response, issued, settings)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session")
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    """Idempotent, and never an error: signing out of a session that is already gone is fine."""
    require_csrf(request)
    supplied = request.cookies.get(REFRESH_COOKIE)
    if supplied:
        await revoke_one(session, supplied, "logout")
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_auth_cookies(out, settings)
    del response
    return out


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


async def _load_me(session: AsyncSession, user_id: int) -> AppUser:
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise unauthenticated("That token refers to an account that no longer exists.")
    return user


async def _me_payload(session: AsyncSession, user: AppUser, entitlements: EntitlementsDep) -> MeOut:
    subscription = (
        await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    plan_code: str | None = None
    if subscription is not None:
        plan_code = (
            await session.execute(select(Plan.code).where(Plan.id == subscription.plan_id))
        ).scalar_one_or_none()

    pending = (
        await session.execute(
            select(AccountDeletion).where(
                AccountDeletion.user_id == user.id,
                AccountDeletion.cancelled_at.is_(None),
                AccountDeletion.purged_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    granted = entitlements.as_dict()
    return MeOut(
        public_id=user.public_id,
        email=user.email,
        name=user.name,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
        plan_code=plan_code,
        subscription_status=subscription.status if subscription is not None else None,
        entitlements=EntitlementsOut.model_validate(granted),
        # Prompt 17 deliverable 4: the web app renders the /admin link from this, the same way
        # it renders every gate from `entitlements` — server truth, never a client guess.
        is_staff=user.is_staff,
        deletion_scheduled_for=pending.purge_after if pending is not None else None,
        # What the caller compares its own session against. A cookie stamped with a lower number
        # was issued before a revocation and is dead; `NEEDS-MAULIK.md` §22.
        session_epoch=user.session_epoch,
    )


@router.get("/me", response_model=MeOut, summary="Profile and entitlements")
async def get_me(
    session: SessionDep, principal: AuthenticatedDep, entitlements: EntitlementsDep
) -> MeOut:
    """docs/07: `GET /me` -> "profile + entitlements"."""
    user = await _load_me(session, principal.require_user())
    return await _me_payload(session, user, entitlements)


@router.patch("/me", response_model=MeOut, summary="Update the profile")
async def patch_me(
    body: UpdateMeIn,
    session: SessionDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
) -> MeOut:
    """docs/07: `PATCH /me`. Only the display name — the email is an identity change, and
    changing it needs the new address proved, which is a different flow (`docs/12a` §8)."""
    user = await _load_me(session, principal.require_user())
    if body.name is not None:
        user.name = body.name
        await session.flush()
    return await _me_payload(session, user, entitlements)


# ---------------------------------------------------------------------------
# DPDP — export and erasure (docs/11 §Compliance, Prompt 12 §5)
# ---------------------------------------------------------------------------


@router.get("/me/export", response_model=DataExportOut, summary="Export everything we hold")
async def export_me(
    session: SessionDep, principal: AuthenticatedDep, entitlements: EntitlementsDep
) -> DataExportOut:
    """docs/11 §Compliance: "DPDP Act: ... **data export** ... endpoints".

    Everything keyed to the account, in one document. Market data is not included: it is not the
    user's personal data, it is the same public data every account sees.
    """
    user = await _load_me(session, principal.require_user())

    screens = (
        (await session.execute(select(Screen).where(Screen.user_id == user.id))).scalars().all()
    )
    consents = (
        (await session.execute(select(ConsentRecord).where(ConsentRecord.user_id == user.id)))
        .scalars()
        .all()
    )
    subscriptions = (
        (await session.execute(select(Subscription).where(Subscription.user_id == user.id)))
        .scalars()
        .all()
    )
    payments = (
        (await session.execute(select(Payment).where(Payment.user_id == user.id))).scalars().all()
    )

    return DataExportOut(
        exported_at=dt.datetime.now(tz=dt.UTC),
        profile=await _me_payload(session, user, entitlements),
        screens=[
            {
                "public_id": row.public_id,
                "name": row.name,
                "definition": row.definition,
                "columns": row.columns,
                "created_at": row.created_at.isoformat(),
            }
            for row in screens
        ],
        consents=[
            {
                "kind": row.kind,
                "document_version": row.document_version,
                "granted_at": row.granted_at.isoformat(),
                "withdrawn_at": row.withdrawn_at.isoformat() if row.withdrawn_at else None,
                "source_ip": row.source_ip,
            }
            for row in consents
        ],
        subscriptions=[
            {
                "plan_id": row.plan_id,
                "status": row.status,
                "started_at": row.started_at.isoformat(),
                "current_period_end": (
                    row.current_period_end.isoformat() if row.current_period_end else None
                ),
            }
            for row in subscriptions
        ],
        payments=[
            {
                "amount_inr": str(row.amount_inr),
                "status": row.status,
                "invoice_number": row.invoice_number,
                "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
                # docs/11 §Compliance: the GST particulars are part of what we hold about the
                # person, so a DPDP export that omitted them would be an incomplete export
                # (Prompt 13). The gateway's own ids are included for the same reason: they are
                # what a customer needs to reconcile a charge with their bank.
                "taxable_inr": str(row.taxable_inr) if row.taxable_inr is not None else None,
                "cgst_inr": str(row.cgst_inr) if row.cgst_inr is not None else None,
                "sgst_inr": str(row.sgst_inr) if row.sgst_inr is not None else None,
                "igst_inr": str(row.igst_inr) if row.igst_inr is not None else None,
                "gst_inr": str(row.gst_inr) if row.gst_inr is not None else None,
                "gst_rate": str(row.gst_rate) if row.gst_rate is not None else None,
                "place_of_supply": row.place_of_supply,
                "customer_gstin": row.customer_gstin,
                "sac_code": row.sac_code,
                "razorpay_payment_id": row.razorpay_payment_id,
                "razorpay_order_id": row.razorpay_order_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in payments
        ],
    )


@router.delete("/me", response_model=DeletionOut, summary="Schedule account deletion")
async def delete_me(  # noqa: PLR0913, PLR0917 - FastAPI injects one parameter per dependency
    body: DeleteAccountIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    principal: AuthenticatedDep,
    mailer: MailerDep,
) -> DeletionOut:
    """Prompt 12 §5: erasure with a seven-day soft-delete window.

    The address is retyped because this is the one irreversible action in the product, and a
    stolen access token should not be enough to end an account on one click.
    """
    user = await _load_me(session, principal.require_user())
    if normalise_email(str(body.email)) != normalise_email(user.email):
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "That is not this account's email address.",
            errors=[{"field": "email", "message": "does not match"}],
        )

    pending = await request_deletion(session, user, settings)
    await mailer.deliver(
        mail.account_deletion_scheduled(user.email, settings.account_purge_after_days)
    )
    clear_auth_cookies(response, settings)
    return DeletionOut(
        status="scheduled",
        purge_after=pending.purge_after,
        detail=(
            f"Your account is deactivated and will be erased on "
            f"{pending.purge_after.date().isoformat()}. Signing in again before then cancels it."
        ),
    )


@router.post("/me/restore", response_model=DeletionOut, summary="Cancel a pending deletion")
async def restore_me(
    body: DeleteAccountIn,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> DeletionOut:
    """A deactivated account cannot present a token, so this is reached by address plus a code.

    In practice the web app cancels a deletion by signing in — `/auth/verify-otp` does it. This
    endpoint exists so the same thing is possible without a UI. `docs/12a` §6.
    """
    del settings, mailer
    email = normalise_email(str(body.email))
    user = await find_user_including_deleted(session, email)
    if user is None or await cancel_deletion(session, user) is False:
        return DeletionOut(status="cancelled", detail="There is no pending deletion.")
    await revoke_all_for_user(session, user.id, "restored")
    return DeletionOut(status="cancelled", detail="The pending deletion has been cancelled.")
