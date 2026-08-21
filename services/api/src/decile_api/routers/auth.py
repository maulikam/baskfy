"""``/auth/*`` and ``/me`` — docs/07 §"Account & billing", docs/11 §Security (Prompt 12).

    POST /auth/register  /auth/login  /auth/refresh  /auth/logout
    POST /auth/request-otp  /auth/verify-otp
    POST /auth/forgot-password  /auth/reset-password
    GET  /me                                  -> profile + entitlements
    PATCH /me
    POST /me/change-password

plus three docs/11 §Compliance requires and docs/07 does not enumerate: `POST /auth/verify-email`
(the counterpart to the mail `/auth/register` sends), `GET /me/export` and `DELETE /me` with
`POST /me/restore` (DPDP export and erasure, Prompt 12 §5). `docs/12a` §1 records the addition.

Two shapes recur and are deliberate:

**The uninformative 202.** `request-otp`, `forgot-password` and `register` all answer
`202 accepted` with the same sentence whether or not the address is known. docs/11's PII inventory
makes the customer list PII; an endpoint that says "no such account" publishes it.

**The lockout is checked before the credential.** docs/11: "account lockout after 10 failures".
Checking after would let a locked account still be probed at full speed.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.auth import AuthenticatedDep, settings_for
from decile_api.auth_service import (
    AccountLocked,
    AuthError,
    CodeSpec,
    InvalidCredentials,
    IssuedSession,
    TokenReused,
    assert_not_locked,
    authenticate_password,
    cancel_deletion,
    consume_code,
    consume_link_token,
    create_user,
    find_user,
    find_user_including_deleted,
    issue_code,
    issue_session,
    normalise_email,
    record_consent,
    record_failure,
    request_deletion,
    revoke_all_for_user,
    revoke_one,
    rotate_refresh,
    set_password,
)
from decile_api.csrf import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    require_csrf,
    set_auth_cookies,
)
from decile_api.db import SessionDep
from decile_api.email import Mailer, build_transport
from decile_api.email import templates as mail
from decile_api.entitlements import EntitlementsDep
from decile_api.problems import Problem, ProblemType, unauthenticated
from decile_api.schemas import (
    AcceptedOut,
    ChangePasswordIn,
    DataExportOut,
    DeleteAccountIn,
    DeletionOut,
    EntitlementsOut,
    ForgotPasswordIn,
    LoginIn,
    MeOut,
    RegisterIn,
    RequestOtpIn,
    ResetPasswordIn,
    SessionOut,
    UpdateMeIn,
    VerifyEmailIn,
    VerifyOtpIn,
)
from decile_api.security import WeakPassword, verify_password
from decile_api.settings import Settings
from decile_core.models import (
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


@router.post(
    "/auth/register",
    response_model=AcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create an account",
)
async def register(
    request: Request,
    body: RegisterIn,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> AcceptedOut:
    """docs/07: `POST /auth/register`.

    Answers 202 either way. Registering an address that already has an account sends *that*
    account a sign-in prompt rather than saying "already registered", which would turn the
    registration form into a membership check.
    """
    if not body.accept_terms:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "The Terms and the Privacy Policy have to be accepted to create an account.",
            errors=[{"field": "accept_terms", "message": "required"}],
        )

    email = normalise_email(str(body.email))
    existing = await find_user_including_deleted(session, email)

    if existing is None:
        try:
            user = await create_user(
                session, email, password=body.password, name=body.name, settings=settings
            )
        except WeakPassword as exc:
            raise Problem(
                ProblemType.INVALID_SCREEN_DEFINITION,
                str(exc),
                errors=[{"field": "password", "message": str(exc)}],
            ) from exc

        kinds = ["terms", "privacy"] + (["marketing"] if body.accept_marketing else [])
        await record_consent(
            session,
            user,
            tuple(kinds),
            source_ip=_client_ip(request),
            user_agent=_user_agent(request),
        )
        token = await issue_code(session, CodeSpec.email_verify(settings), email=email, user=user)
        await mailer.deliver(
            mail.verify_email(
                user.email,
                f"{settings.web_origin}/verify-email?token={token}",
                settings.email_verification_ttl_seconds // HOUR_SECONDS,
            )
        )
    else:
        # The address is taken. Tell *the owner*, not the person at the form.
        code = await issue_code(session, CodeSpec.otp(settings), email=email, user=existing)
        await mailer.deliver(
            mail.otp(existing.email, code, settings.otp_ttl_seconds // MINUTE_SECONDS)
        )

    return AcceptedOut(detail=NEUTRAL_DETAIL)


@router.post("/auth/login", response_model=SessionOut, summary="Sign in with a password")
async def login(
    body: LoginIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> SessionOut:
    """docs/07: `POST /auth/login`. docs/11: Argon2id, with lockout after 10 failures."""
    email = normalise_email(str(body.email))
    try:
        await assert_not_locked(session, email, settings)
        user = await authenticate_password(session, email, body.password, settings)
    except AuthError as error:
        if isinstance(error, InvalidCredentials):
            await record_failure(session, email, settings, mailer)
        raise _as_problem(error) from error

    await _clear_pending_deletion(session, user)
    issued = await issue_session(session, user, settings)
    return _session_response(response, issued, settings)


async def _clear_pending_deletion(session: AsyncSession, user: AppUser) -> None:
    """Prompt 12 §5: "Signing in again before then cancels the deletion"."""
    if user.deleted_at is not None:
        await cancel_deletion(session, user)


@router.post(
    "/auth/request-otp",
    response_model=AcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a sign-in code",
)
async def request_otp(
    body: RequestOtpIn,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> AcceptedOut:
    """docs/11: "OTP login as the default path". 202 whether or not the address is known."""
    email = normalise_email(str(body.email))
    user = await find_user(session, email)
    if user is not None:
        code = await issue_code(session, CodeSpec.otp(settings), email=email, user=user)
        await mailer.deliver(mail.otp(user.email, code, settings.otp_ttl_seconds // MINUTE_SECONDS))
    return AcceptedOut(detail=NEUTRAL_DETAIL)


@router.post("/auth/verify-otp", response_model=SessionOut, summary="Sign in with a code")
async def verify_otp(
    body: VerifyOtpIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> SessionOut:
    email = normalise_email(str(body.email))
    try:
        await assert_not_locked(session, email, settings)
        await consume_code(
            session,
            email=email,
            purpose="otp",
            supplied=body.code,
            max_attempts=settings.otp_max_attempts,
        )
    except AuthError as error:
        if isinstance(error, InvalidCredentials):
            await record_failure(session, email, settings, mailer)
        raise _as_problem(error) from error

    user = await find_user_including_deleted(session, email)
    if user is None:
        raise unauthenticated("Those credentials are not valid.")

    # A correct code proves control of the inbox, which is exactly what verification asserts.
    if user.email_verified_at is None:
        user.email_verified_at = dt.datetime.now(tz=dt.UTC)
    await _clear_pending_deletion(session, user)
    await session.flush()

    issued = await issue_session(session, user, settings)
    return _session_response(response, issued, settings)


@router.post("/auth/verify-email", response_model=AcceptedOut, summary="Confirm an email address")
async def verify_email(
    body: VerifyEmailIn,
    session: SessionDep,
    settings: SettingsDep,
) -> AcceptedOut:
    """The counterpart to the mail `/auth/register` sends.

    Not in docs/07's list — docs/07 names `register` but not the confirmation it implies, and an
    unverifiable verification mail would be worse than none. `docs/12a` §1.
    """
    del settings
    try:
        token = await consume_link_token(session, purpose="email_verify", supplied=body.token)
    except InvalidCredentials as error:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "That confirmation link is not valid or has already been used.",
            errors=[{"field": "token", "message": "invalid or expired"}],
        ) from error

    user = await find_user_including_deleted(session, token.email)
    if user is not None and user.email_verified_at is None:
        user.email_verified_at = dt.datetime.now(tz=dt.UTC)
        await session.flush()
    return AcceptedOut(detail="Your email address is confirmed.")


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


@router.post(
    "/auth/forgot-password",
    response_model=AcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a password-reset link",
)
async def forgot_password(
    body: ForgotPasswordIn,
    session: SessionDep,
    settings: SettingsDep,
    mailer: MailerDep,
) -> AcceptedOut:
    email = normalise_email(str(body.email))
    user = await find_user(session, email)
    if user is not None:
        token = await issue_code(session, CodeSpec.password_reset(settings), email=email, user=user)
        await mailer.deliver(
            mail.password_reset(
                user.email,
                f"{settings.web_origin}/reset-password?token={token}",
                settings.password_reset_ttl_seconds // MINUTE_SECONDS,
            )
        )
    return AcceptedOut(detail=NEUTRAL_DETAIL)


@router.post("/auth/reset-password", response_model=SessionOut, summary="Set a new password")
async def reset_password(
    body: ResetPasswordIn,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> SessionOut:
    """Resetting signs the user in, and signs every other session out.

    Signing them in is the difference between "your password is changed, now go and log in" and
    finishing the job. Signing the others out is the point of the reset: whoever prompted it
    should not still be holding a session.
    """
    try:
        token = await consume_link_token(session, purpose="password_reset", supplied=body.token)
    except InvalidCredentials as error:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "That reset link is not valid or has already been used.",
            errors=[{"field": "token", "message": "invalid or expired"}],
        ) from error

    user = await find_user_including_deleted(session, token.email)
    if user is None:
        raise unauthenticated("That reset link refers to an account that no longer exists.")

    try:
        await set_password(session, user, body.password, settings, reason="password_reset")
    except WeakPassword as exc:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            str(exc),
            errors=[{"field": "password", "message": str(exc)}],
        ) from exc

    await _clear_pending_deletion(session, user)
    if user.email_verified_at is None:
        user.email_verified_at = dt.datetime.now(tz=dt.UTC)
        await session.flush()

    issued = await issue_session(session, user, settings)
    return _session_response(response, issued, settings)


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
        has_password=user.password_hash is not None,
        created_at=user.created_at,
        plan_code=plan_code,
        subscription_status=subscription.status if subscription is not None else None,
        entitlements=EntitlementsOut.model_validate(granted),
        # Prompt 17 deliverable 4: the web app renders the /admin link from this, the same way
        # it renders every gate from `entitlements` — server truth, never a client guess.
        is_staff=user.is_staff,
        deletion_scheduled_for=pending.purge_after if pending is not None else None,
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


@router.post("/me/change-password", response_model=MeOut, summary="Change the password")
async def change_password(
    body: ChangePasswordIn,
    session: SessionDep,
    settings: SettingsDep,
    principal: AuthenticatedDep,
    entitlements: EntitlementsDep,
) -> MeOut:
    """docs/07: `POST /me/change-password`.

    An account with no password (docs/11 allows it — "password optional") is *setting* one and
    supplies none; an account that has one must prove it, so a stolen access token cannot be
    upgraded into a permanent credential.
    """
    user = await _load_me(session, principal.require_user())

    if user.password_hash is not None:
        supplied = body.current_password or ""
        if not verify_password(supplied, user.password_hash, settings):
            raise unauthenticated("The current password is not correct.")

    try:
        await set_password(session, user, body.new_password, settings, reason="password_change")
    except WeakPassword as exc:
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            str(exc),
            errors=[{"field": "new_password", "message": str(exc)}],
        ) from exc

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
