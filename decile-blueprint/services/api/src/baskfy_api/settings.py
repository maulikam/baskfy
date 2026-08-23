"""Runtime settings. Env prefix ``BASKFY_`` (docs/14 §"Naming inside the codebase")."""

from __future__ import annotations

from decimal import Decimal
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
        env_prefix="BASKFY_",
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: SQLAlchemy async URL, e.g. postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy
    database_url: str = Field(
        default="postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy",
    )
    redis_url: str = Field(default="redis://localhost:6380/0")

    #: Where a backtest actually runs (M42).
    #:
    #: ``inline`` runs it in this process, in a bounded pool, and is the default because it is what
    #: the work is worth: a nine-year monthly run measures about two seconds, nearly all of it
    #: waiting on Postgres. It is also the only mode with no way to leave a run queued for ever,
    #: which is the failure this default exists to remove — see `backtest_runner`.
    #:
    #: ``celery`` publishes to the broker instead, for a deployment that wants the simulation off
    #: the web process. Choosing it means running a worker on the ``backtest`` queue.
    backtest_executor: Literal["inline", "celery"] = "inline"

    #: How many backtests one API process runs at once. See `backtest_runner.DEFAULT_CONCURRENCY`.
    backtest_concurrency: int = Field(default=2, ge=1, le=8)

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
    email_from: str = "Baskfy <no-reply@baskfy.com>"
    email_reply_to: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, gt=0)
    #: The web app's origin, for the links in emails.
    web_origin: str = "http://localhost:3000"
    #: Where `POST /support` delivers (Prompt 18 §2). A setting, not a constant, so a deployment
    #: can route it at a real inbox while every test and every local run sends it to the console
    #: transport or to mailpit.
    support_email: str = "support@baskfy.com"

    # --- Payments (docs/02 §Payments: Razorpay; Prompt 13) -------------------
    #: Razorpay's REST base. A setting so a test can point it at a local stub and so nothing in
    #: this repository can accidentally reach the live gateway (`network_guard.py` blocks it
    #: outright in the suite).
    razorpay_api_base: str = "https://api.razorpay.com/v1"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    #: docs/11 §Security: "Razorpay webhooks: verify signature". A *different* secret from the
    #: API key — Razorpay signs webhooks with the value configured on the webhook, not the key.
    razorpay_webhook_secret: str = ""
    #: Razorpay subscriptions bill against a plan created in *their* dashboard, which has its own
    #: id. It is environment-specific (test vs live), so it is configuration, not seed data.
    razorpay_plan_id_monthly: str = ""
    razorpay_plan_id_yearly: str = ""
    #: How many billing cycles a subscription is created for. Razorpay requires a finite count;
    #: ten years of cycles is "until cancelled" in practice.
    razorpay_subscription_cycles_monthly: int = Field(default=120, gt=0)
    razorpay_subscription_cycles_yearly: int = Field(default=10, gt=0)
    #: Seconds a call to the gateway may take before it is abandoned.
    razorpay_timeout_seconds: float = Field(default=10.0, gt=0)

    #: PROMPTS.md Prompt 13 §5: "A ₹0 free tier with a limited universe is optional — implement it
    #: behind a feature flag." docs/11 §Reliability: "Feature flags for anything touching money".
    free_tier_enabled: bool = False

    # --- Track B (docs/smallcase/02) — dark until D3 / SC10 -------------------
    #: Subscription plans, entitlement locks, paywall. Default OFF — every basket is Free Access.
    subscriptions_enabled: bool = False
    #: Fee *collection* (vs ledger computation). Default OFF; no payment call sites while false.
    fee_collection_enabled: bool = False
    #: Public sign-up / onboarding. Default OFF — signup-shaped routes 404 while false.
    public_signup_enabled: bool = False

    # --- GST invoicing (docs/11 §"Compliance & legal (India)") ---------------
    #: 18% for an online information service. A setting because the rate is Parliament's to
    #: change, and an issued invoice keeps the rate it was raised at (`payment.gst_rate`).
    gst_rate_percent: Decimal = Field(default=Decimal("18"), ge=0)
    #: SAC 998439, "Other on-line contents n.e.c." NOT CONFIRMED BY A CHARTERED ACCOUNTANT.
    gst_sac_code: str = "998439"
    #: The supplier's own particulars — whoever operates the service, not the code.
    supplier_legal_name: str = "Baskfy"
    supplier_address_lines: tuple[str, ...] = ()
    supplier_gstin: str = ""
    #: Spelled as the GST return expects it, e.g. "Karnataka (29)". Used as the default place of
    #: supply for a customer who has told us nothing (IGST Act §12(2)(b)).
    supplier_state: str = ""
    #: Prefix of the invoice series: ``DCL/2026-27/000001``.
    invoice_series_prefix: str = "DCL"
    #: Zero-padding of the running number inside a series.
    invoice_number_width: int = Field(default=6, gt=0)
    #: Where invoice PDFs are written. ``payment.invoice_pdf_key`` stores the full key.
    invoice_object_prefix: str = "invoices"
    #: The directory invoice PDFs land in when no S3/R2 bucket is configured. Shares the raw-file
    #: archive's default so a laptop has one place to look.
    invoice_local_dir: str = ".archive"

    # --- Backtests (docs/10, PROMPTS.md Prompt 15 §4) ------------------------
    #: PROMPTS.md Prompt 15 §4: "a per-user concurrency cap of 1, and a global cap." The per-user
    #: number is the document's; the global one is not in the bundle at all. Both are settings so
    #: a deployment with a bigger backtest pool can raise them without a code change.
    backtest_user_concurrency: int = Field(default=1, gt=0)
    backtest_global_concurrency: int = Field(default=8, gt=0)

    # --- Public read API, API keys, alerts and webhooks (Prompt 20) ----------
    #: PROMPTS.md Prompt 20 §2: "Gate the entire feature behind a flag that stays OFF until the
    #: data-redistribution review in docs/11 is signed off." **This default is asserted by
    #: ``services/api/tests/test_public_api.py`` and by a scan over every environment file in the
    #: repository** — Prompt 20's third acceptance criterion. Turning it on is not sufficient on
    #: its own: ``baskfy_core.public_api.DATA_REDISTRIBUTION_REVIEW`` is a source constant that a
    #: human must edit, and the router refuses to mount while it says the review is outstanding.
    public_api_enabled: bool = False
    #: Where a caller of the public API is told to write. Appears in the terms-of-use document.
    public_api_contact_email: str = "api@baskfy.com"
    #: The base URL printed in the copy-paste examples on the interactive reference. A setting so
    #: a staging deployment's examples point at staging rather than at production.
    public_api_base_url: str = "http://localhost:8000"
    #: Redoc is loaded from a CDN — see ``baskfy_api.routers.public``. Pinned by version, and a
    #: setting so an air-gapped deployment can point it at a self-hosted copy.
    redoc_script_url: str = "https://cdn.redoc.ly/redoc/v2.5.0/bundles/redoc.standalone.js"

    #: Prompt 20 §1: "per-key rate limits". The ceiling an owner may set for one of their own
    #: keys, so a key cannot be given a quota larger than the tier docs/07 fixes at 600/min.
    api_key_max_rate_limit_per_minute: int = Field(default=600, gt=0)
    #: How long a key lives when the caller does not say. ``None`` means "until revoked", which
    #: is what an integration credential usually is; a default expiry that silently breaks a
    #: production integration at 3am is worse than a key an operator has to remember to rotate.
    api_key_default_ttl_days: int | None = None

    #: PROMPTS.md Prompt 20 §3. The number of days of screen runs an alert will look back over
    #: for its "previous" side. Beyond this the diff is against nothing and the alert is skipped
    #: rather than reporting every constituent as an entry.
    alert_lookback_days: int = Field(default=14, gt=0)
    #: The smallest rank move an alert reports by default (``baskfy_core.screen_diff``).
    alert_min_move: int = Field(default=1, gt=0)
    #: How many entries/exits/movers one email lists before it says "and N more". An email with
    #: four thousand rows in it is not an alert.
    alert_max_rows: int = Field(default=25, gt=0)

    #: Prompt 20 §4: "HMAC signing". The master secret every endpoint's signing key is derived
    #: from — see ``baskfy_api.webhooks``. Empty falls back to ``jwt_secret``, which production
    #: already requires; a deployment with neither cannot create a webhook at all.
    webhook_signing_secret: str = ""
    #: Seconds one delivery attempt may take.
    webhook_timeout_seconds: float = Field(default=10.0, gt=0)
    #: "retry with backoff". Attempts, and the base of the exponential schedule in seconds:
    #: 30s, 2m, 8m, 32m, 2h8m — five attempts spanning about three hours.
    webhook_max_attempts: int = Field(default=5, gt=0)
    webhook_backoff_base_seconds: int = Field(default=30, gt=0)
    webhook_backoff_factor: int = Field(default=4, gt=1)
    #: Consecutive failed deliveries before an endpoint is switched off and its owner told.
    webhook_failure_threshold: int = Field(default=20, gt=0)

    # --- Rate limits (docs/07 §Conventions) ----------------------------------
    rate_limit_anonymous_per_minute: int = Field(default=10, gt=0)
    rate_limit_authenticated_per_minute: int = Field(default=60, gt=0)
    rate_limit_api_key_per_minute: int = Field(default=600, gt=0)
    #: docs/11: "exponential backoff on auth endpoints". Per IP, far below the anonymous tier,
    #: because an auth endpoint is the one place a stranger's request costs a password guess.
    rate_limit_auth_per_minute: int = Field(default=10, gt=0)
    #: Gateway webhooks are not anonymous browser traffic. Razorpay retries a failed delivery,
    #: and dropping one at ten a minute would lose a payment; the bucket exists so the endpoint is
    #: still metered rather than open. Keyed per IP, like every other anonymous caller.
    rate_limit_webhook_per_minute: int = Field(default=600, gt=0)
    #: Turned off in tests that are not about rate limiting, and in `local` by choice.
    rate_limit_enabled: bool = True

    # --- Connection pooling (Prompt 16 deliverable 6) ------------------------
    #: SQLAlchemy's ``QueuePool`` in front of asyncpg. docs/11 §"Cost envelope" sizes the box at
    #: 8 vCPU / 32 GB and docs/03 §"Scaling plan" step 1 says "vertical: bigger box" before
    #: anything else, so the pool is sized against one API process on that box rather than
    #: against a fleet. See ``baskfy_api.db`` for the arithmetic.
    db_pool_size: int = Field(default=10, gt=0)
    db_max_overflow: int = Field(default=10, ge=0)
    #: Seconds a request waits for a connection before it is refused. Shorter than the screen-run
    #: budget in docs/11 (800 ms cold) times a small factor: a request that has already queued
    #: this long has missed its budget, and holding it open only deepens the queue.
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    #: Recycle a connection after this long. Below any sensible proxy or firewall idle timeout,
    #: so the pool never hands out a socket the other end has already dropped.
    db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    #: asyncpg prepares every statement server-side and caches the handle per connection. Behind
    #: pgbouncer in *transaction* pooling mode that cache is wrong — the next transaction may land
    #: on a different server connection — so a deployment that puts pgbouncer in front must set
    #: this to 0. Direct connections keep it, because the screen query is the same statement every
    #: time and re-planning it per request is pure waste.
    db_statement_cache_size: int = Field(default=100, ge=0)

    # --- HTTP caching (Prompt 16 deliverable 3) ------------------------------
    #: ``stale-while-revalidate`` on the published analytics reads. One minute: long enough that a
    #: burst of RSC renders is served from the Next data cache while one of them revalidates,
    #: short enough that a publish is visible within a minute even if `POST /api/revalidate`
    #: never arrives (``docs/11a`` §6).
    http_stale_while_revalidate_seconds: int = Field(default=60, ge=0)

    # --- Observability (docs/02 §Observability) ------------------------------
    otel_enabled: bool = False
    otel_service_name: str = "baskfy-api"
    otel_exporter_otlp_endpoint: str | None = None
    log_level: str = "INFO"
    #: JSON lines in every environment but `local`, where a human is reading them.
    log_json: bool = True

    #: docs/02 §Observability: "Sentry for errors". Empty means the SDK is never initialised at
    #: all — see `baskfy_api.sentry` for why that is the honest default rather than a no-op init.
    sentry_dsn: str = ""
    #: Fraction of *error* events sent. 1.0 because an exception this service raises is rare and
    #: sampling them away is how a rare bug stays invisible; the knob exists for an incident where
    #: one endpoint is throwing thousands a minute.
    sentry_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    #: The build this process is running, e.g. a git sha. Tags Sentry events and OTel resources so
    #: "when did this start" has an answer. Set by the deploy; empty on a laptop.
    release: str = ""

    #: PROMPTS.md Prompt 17 §2. `/metrics` is unauthenticated by design and must therefore not be
    #: exposed publicly — it is a private-network scrape target (see `docs/runbooks/`). Off in
    #: `local` costs nothing; on everywhere a Prometheus exists.
    metrics_enabled: bool = True
    #: When set, `/metrics` requires `Authorization: Bearer <this>`. A shared secret rather than a
    #: JWT because Prometheus has no account and cannot refresh one. Empty = no check, which is
    #: correct behind a private network and wrong on the public internet.
    metrics_token: str = ""

    # --- Alerting (Prompt 17 deliverable 3) ----------------------------------
    #: Where operational alerts are emailed. Empty means alerts are logged (and sent to Sentry if
    #: it is configured) but nobody is woken up.
    ops_alert_email: str = ""
    #: An optional generic webhook (Alertmanager, Slack's incoming webhook, whatever the operator
    #: has). Posted as JSON. Empty disables it.
    ops_alert_webhook_url: str = ""
    #: docs/11 §Reliability: "data published by 20:15 IST on >=95% of trading days." The deadline
    #: the publish-late alert fires on, as an IST wall-clock time in `HH:MM`.
    publish_deadline_ist: str = "20:15"
    #: A run still `running` this long after it started is treated as abandoned — the shape a
    #: worker killed mid-pipeline leaves behind. Above the 45-minute end-to-end budget in docs/11
    #: with room for a slow night, so a healthy long run is never reaped.
    pipeline_stale_after_minutes: int = Field(default=90, gt=0)
    #: Queue depth that counts as a backlog worth an alert.
    queue_backlog_threshold: int = Field(default=100, gt=0)
    #: How long before a Kite access token expires the warning fires, in hours.
    kite_token_warning_hours: int = Field(default=6, gt=0)

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
                "BASKFY_JWT_SECRET is empty in production; the API could not verify any token"
            )
        if len(self.jwt_secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
            raise RuntimeError(
                f"BASKFY_JWT_SECRET is shorter than the {MIN_JWT_SECRET_BYTES} bytes RFC 7518 "
                "§3.2 requires for HS256"
            )
        if not self.cookie_secure:
            # docs/11 §Security: the refresh cookie is Secure. Serving it over plain HTTP in
            # production would put a 30-day credential on the wire in the clear.
            raise RuntimeError("BASKFY_COOKIE_SECURE is false in production")
        if self.email_transport == "console":
            # docs/11 requires a lockout *notification* and Prompt 12 a verification mail. A
            # deployment that prints them to stdout has neither.
            raise RuntimeError(
                "BASKFY_EMAIL_TRANSPORT is 'console' in production; no mail would be delivered"
            )
        if self.email_transport == "resend" and not self.resend_api_key:
            raise RuntimeError("BASKFY_RESEND_API_KEY is empty but the transport is 'resend'")
        # docs/11 §Security: "Razorpay webhooks: verify signature". An empty secret cannot verify
        # one, and a webhook handler that accepts anything is a way to grant yourself a plan.
        if not self.razorpay_webhook_secret:
            raise RuntimeError(
                "BASKFY_RAZORPAY_WEBHOOK_SECRET is empty in production; no webhook could be "
                "verified"
            )
        if not (self.razorpay_key_id and self.razorpay_key_secret):
            raise RuntimeError("BASKFY_RAZORPAY_KEY_ID/SECRET are empty in production")
        # docs/11 §Compliance: "GST-compliant invoices with GSTIN ...". An invoice without the
        # supplier's GSTIN is not a tax invoice, and it is issued the moment someone pays.
        if not self.supplier_gstin:
            raise RuntimeError("BASKFY_SUPPLIER_GSTIN is empty in production; invoices need it")
        if not self.supplier_state:
            raise RuntimeError(
                "BASKFY_SUPPLIER_STATE is empty in production; place of supply needs it"
            )

    def payments_configured(self) -> bool:
        """Whether a checkout can actually be created. False on a laptop with no keys."""
        return bool(self.razorpay_key_id and self.razorpay_key_secret)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
