"""Runtime settings. Env prefix ``DECILE_`` (docs/14 §"Naming inside the codebase")."""

from __future__ import annotations

from functools import lru_cache
from typing import Final, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: docs/07: "Base: `/api/v1`".
API_PREFIX: Final = "/api/v1"

#: docs/11 §Security: "JWT: HS256". Fixed rather than configurable — an algorithm a caller can
#: influence is how ``alg: none`` and RS256/HS256 confusion attacks get in.
JWT_ALGORITHM: Final = "HS256"

#: RFC 7518 §3.2: "A key of the same size as the hash output ... or larger MUST be used". SHA-256
#: is 32 bytes. A shorter shared secret weakens every token the web app issues.
MIN_JWT_SECRET_BYTES: Final = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DECILE_",
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: SQLAlchemy async URL, e.g. postgresql+asyncpg://decile:decile@localhost:5433/decile
    database_url: str = Field(
        default="postgresql+asyncpg://decile:decile@localhost:5433/decile",
    )
    redis_url: str = Field(default="redis://localhost:6380/0")

    #: Used only by the `db`-marked test suite; unset means those tests skip.
    test_database_url: str | None = None

    environment: Literal["local", "test", "staging", "production"] = "local"

    # --- Auth (docs/07 §header, docs/11 §Security) ---------------------------
    #: Shared with the Next.js app, which mints the access tokens (docs/07). Empty in `local`
    #: means "no token can be verified", so every request is anonymous — which is the honest
    #: behaviour for a developer who has not configured auth, and is refused outright in
    #: production by :meth:`require_configured`.
    jwt_secret: str = ""
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    #: docs/11: "15-min access". Tokens are verified, not issued, here; this is the ceiling we
    #: accept, so a web app that mints a year-long token is still refused.
    jwt_max_lifetime_seconds: int = Field(default=15 * 60, gt=0)
    #: Clock skew allowed on `exp`/`nbf`, in seconds.
    jwt_leeway_seconds: int = Field(default=30, ge=0)

    # --- Auth issuance (Prompt 12; docs/11 §Security) ------------------------
    #: docs/11: "15-min access". The API now *issues* as well as verifies, so this is the
    #: lifetime it mints — kept below `jwt_max_lifetime_seconds`, which is the ceiling it accepts.
    access_token_ttl_seconds: int = Field(default=15 * 60, gt=0)
    #: How long a refresh-token family lives before a fresh login is required.
    refresh_token_ttl_days: int = Field(default=30, gt=0)

    #: docs/11: "OTP login as the default path". Six digits, ten minutes, five guesses.
    otp_length: int = Field(default=6, ge=6, le=10)
    otp_ttl_seconds: int = Field(default=10 * 60, gt=0)
    otp_max_attempts: int = Field(default=5, gt=0)
    password_reset_ttl_seconds: int = Field(default=60 * 60, gt=0)
    email_verification_ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)

    #: docs/11: "account lockout after 10 failures with email notification".
    auth_max_failures: int = Field(default=10, gt=0)
    auth_lockout_minutes: int = Field(default=15, gt=0)
    #: The window failures are counted over. Older failures are forgiven rather than accumulated
    #: forever, so a wrong password last month plus nine today is not a lockout.
    auth_failure_window_minutes: int = Field(default=60, gt=0)

    #: Argon2id parameters. OWASP's 2024 baseline for interactive logins: 19 MiB, 2 passes,
    #: 1 lane. Configurable because the right memory cost depends on the box, not on the code.
    argon2_time_cost: int = Field(default=2, gt=0)
    argon2_memory_kib: int = Field(default=19 * 1024, gt=0)
    argon2_parallelism: int = Field(default=1, gt=0)

    #: docs/11: "httpOnly, `SameSite=Lax`, **Secure** cookie". False only for plain-HTTP local
    #: development, where a Secure cookie is never sent back at all.
    cookie_secure: bool = True
    cookie_domain: str | None = None
    #: Prompt 12 §5: "a 7-day soft-delete window".
    account_purge_after_days: int = Field(default=7, gt=0)

    # --- Email (docs/02 §Email: Resend; Prompt 12 §3: mailpit locally) -------
    #: "resend" in a deployment, "smtp" for mailpit, "console" when neither is configured.
    email_transport: Literal["resend", "smtp", "console"] = "console"
    resend_api_key: str = ""
    email_from: str = "Decile <no-reply@decile.in>"
    email_reply_to: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, gt=0)
    #: The web app's origin, for the links in emails.
    web_origin: str = "http://localhost:3000"

    # --- Rate limits (docs/07 §Conventions) ----------------------------------
    rate_limit_anonymous_per_minute: int = Field(default=10, gt=0)
    rate_limit_authenticated_per_minute: int = Field(default=60, gt=0)
    rate_limit_api_key_per_minute: int = Field(default=600, gt=0)
    #: docs/11: "exponential backoff on auth endpoints". Per IP, far below the anonymous tier,
    #: because an auth endpoint is the one place a stranger's request costs a password guess.
    rate_limit_auth_per_minute: int = Field(default=10, gt=0)
    #: Turned off in tests that are not about rate limiting, and in `local` by choice.
    rate_limit_enabled: bool = True

    # --- Observability (docs/02 §Observability) ------------------------------
    otel_enabled: bool = False
    otel_service_name: str = "decile-api"
    otel_exporter_otlp_endpoint: str | None = None
    log_level: str = "INFO"
    #: JSON lines in every environment but `local`, where a human is reading them.
    log_json: bool = True

    # --- CORS ----------------------------------------------------------------
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)

    def require_configured(self) -> None:
        """Refuse to serve production traffic with development defaults.

        An empty ``jwt_secret`` verifies nothing, so every request would be anonymous and every
        entitlement check would fall to the anonymous branch. That is a safe *failure* mode for a
        laptop and an unacceptable one for a deployment, so it is a startup error rather than a
        surprise in the logs.
        """
        if self.environment != "production":
            return
        if not self.jwt_secret:
            raise RuntimeError(
                "DECILE_JWT_SECRET is empty in production; the API could not verify any token"
            )
        if len(self.jwt_secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
            raise RuntimeError(
                f"DECILE_JWT_SECRET is shorter than the {MIN_JWT_SECRET_BYTES} bytes RFC 7518 "
                "§3.2 requires for HS256"
            )
        if not self.cookie_secure:
            # docs/11 §Security: the refresh cookie is Secure. Serving it over plain HTTP in
            # production would put a 30-day credential on the wire in the clear.
            raise RuntimeError("DECILE_COOKIE_SECURE is false in production")
        if self.email_transport == "console":
            # docs/11 requires a lockout *notification* and Prompt 12 a verification mail. A
            # deployment that prints them to stdout has neither.
            raise RuntimeError(
                "DECILE_EMAIL_TRANSPORT is 'console' in production; no mail would be delivered"
            )
        if self.email_transport == "resend" and not self.resend_api_key:
            raise RuntimeError("DECILE_RESEND_API_KEY is empty but the transport is 'resend'")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
