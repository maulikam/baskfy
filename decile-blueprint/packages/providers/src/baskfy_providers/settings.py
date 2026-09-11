"""Provider configuration. Env prefix ``BASKFY_`` (docs/14 §"Naming inside the codebase")."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderSettings(BaseSettings):
    """Everything the adapters need, and nothing they do not.

    Every credential defaults to empty so that an unconfigured environment produces
    ``CredentialsMissing`` — a reported condition — rather than an import-time crash. That is what
    lets `providers doctor` run on a laptop with no secrets (Prompt 2 acceptance criterion 4).
    """

    model_config = SettingsConfigDict(
        env_prefix="BASKFY_",
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Kite -----------------------------------------------------------
    kite_api_key: str = ""
    kite_api_secret: str = ""
    #: Fernet key used to encrypt the daily access token at rest (docs/11 §Security:
    #: "Kite access token encrypted at rest"). Generate with `Fernet.generate_key()`.
    kite_token_encryption_key: str = ""
    #: Where the encrypted token blob lives. A file by default; Prompt 17 may move it to a
    #: secret store without touching any call site.
    kite_token_path: str = ".secrets/kite-token.enc"

    #: docs/09: "Rate limit ~ 3 req/s". Kite states this as a combined ceiling across every
    #: endpoint, so it is the clock EVERY Kite read waits on, whatever it is asking for.
    kite_rate_limit_per_second: float = Field(default=3.0, gt=0)
    #: What a *bulk* caller — a backfill, a nightly bar pass, an instrument dump — may spend of
    #: that ceiling (M85). Strictly below `kite_rate_limit_per_second`, and the difference is
    #: the headroom an interactive caller finds waiting for it.
    #:
    #: The reason is the thing Maulik reported: he logs in at 1pm, the login starts a
    #: missed-session catch-up, and the catch-up is an hour of `historical_data` at the full
    #: ceiling. Sharing one departure clock with no lanes, the login's own holdings read and
    #: today's live swing scan queue behind that hour and fail their wait budget — the product
    #: would be *more* stale after logging in, not less. A bulk lane below the ceiling keeps the
    #: shared clock from ever running ahead of now, so an interactive call finds a free slot.
    kite_bulk_rate_limit_per_second: float = Field(default=2.0, gt=0)
    #: docs/09: "chunk backfills into <= 2000-day slices per instrument".
    kite_max_days_per_request: int = Field(default=2000, gt=0)
    #: docs/09: "run with bounded concurrency (<= 3)".
    kite_max_concurrency: int = Field(default=3, gt=0)

    # --- Kite session bridge --------------------------------------------
    # A Kite Connect app has one redirect URL, and the RENIL app's belongs to the momentum desk.
    # Baskfy therefore cannot log in; it borrows the session the desk already holds. These three
    # settings are the whole of that bridge (`baskfy_worker.kite_session_cli pull`).
    #: `user@host` of the desk that performs the daily Kite login. Empty disables the pull.
    kite_desk_ssh_target: str = ""
    #: Private key authorised on the desk against a forced command that emits only the token.
    #: The key is a capability, not an account: it cannot open a shell (`docs/DECISIONS-MERGE.md`
    #: M58).
    kite_desk_ssh_key_path: str = "/var/lib/baskfy/.ssh/kite-session"
    #: The desk's host key, pinned. Not trust-on-first-use: the pull runs unattended on a
    #: schedule, and "first use" for an unattended job is whatever host answered — which is a
    #: decision no human is present to make. A missing file fails the pull, which costs history
    #: and nothing else.
    kite_desk_known_hosts_path: str = "/var/lib/baskfy/.ssh/known_hosts"
    #: Seconds to wait for the desk. Short: the pipeline must not hang on an unreachable box, and
    #: the bhavcopy path covers the day either way.
    kite_desk_ssh_timeout_seconds: float = Field(default=20.0, gt=0)

    # --- Retry ----------------------------------------------------------
    provider_max_attempts: int = Field(default=5, ge=1)
    provider_backoff_base_seconds: float = Field(default=0.5, gt=0)
    provider_backoff_max_seconds: float = Field(default=30.0, gt=0)

    # --- Circuit breaker ------------------------------------------------
    provider_circuit_failure_threshold: int = Field(default=5, ge=1)
    provider_circuit_reset_seconds: float = Field(default=60.0, gt=0)

    # --- Redis ----------------------------------------------------------
    redis_url: str = "redis://localhost:6380/0"

    # --- NSE ------------------------------------------------------------
    nse_base_url: str = "https://www.nseindia.com"
    nse_archive_url: str = "https://nsearchives.nseindia.com"
    nse_request_timeout_seconds: float = Field(default=30.0, gt=0)
    #: NSE's public files are rate-sensitive (docs/09 §"NSE specifics").
    nse_rate_limit_per_second: float = Field(default=1.0, gt=0)
    #: How far past ``since`` the corporate-action window reaches, in days.
    #:
    #: NSE answers ``corporates-corporateActions`` **without** a date range with a default first
    #: page of 20 rows, and for eleven months nothing asked it for a range — so the nightly stored
    #: 20 actions a night out of the 87 NSE published for 2026-09-11 alone. See
    #: `gates/ca-truncation.md`. The window must therefore always be explicit.
    #:
    #: Forward-looking because NSE announces an ex-date days to weeks ahead, and an action is
    #: worth storing before it is worth applying: `apply_adjustments` bounds itself to
    #: ``ex_date <= as_of`` (docs/DECISIONS.md §21.9), so a future-dated row sits inert until its
    #: ex-date arrives. In practice NSE publishes about a fortnight ahead, so this is generous.
    nse_corporate_action_window_days: int = Field(default=120, gt=0)

    # --- Object storage (Cloudflare R2 / any S3-compatible) -------------
    s3_endpoint_url: str = ""
    s3_region: str = "auto"
    s3_bucket: str = ""
    #: Where the local raw archive is written when no S3/R2 bucket is configured.
    #:
    #: Relative paths resolve against the working directory, which in the deployed image is
    #: root-owned `/repo` — so the default `.archive` is unwritable there and **every NSE fetch
    #: fails at the archive step before it can parse anything**. docs/09 requires the archive
    #: ("Never re-fetch to re-parse: the archive is the reproducibility record"), so that is not a
    #: step the ingest can skip: it is the whole path being down, silently, on a box where nobody
    #: had run an NSE ingest yet. `docs/DECISIONS-MERGE.md` M56.
    raw_archive_dir: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # --- Fixtures -------------------------------------------------------
    #: Overridable so tests can point at a temporary directory.
    fixture_dir: str = ""

    @model_validator(mode="after")
    def _bulk_leaves_headroom(self) -> ProviderSettings:
        """A bulk lane at or above the ceiling is not a lane — it is the ceiling with extra steps.

        Refused at construction rather than discovered at 1pm, because the symptom of getting
        this wrong is not an error: it is an interactive call quietly waiting behind an hour of
        backfill, which looks exactly like the stale product this lane exists to fix.
        """
        if self.kite_bulk_rate_limit_per_second >= self.kite_rate_limit_per_second:
            raise ValueError(
                "BASKFY_KITE_BULK_RATE_LIMIT_PER_SECOND "
                f"({self.kite_bulk_rate_limit_per_second:g}) must be strictly below "
                f"BASKFY_KITE_RATE_LIMIT_PER_SECOND ({self.kite_rate_limit_per_second:g}); "
                "the difference is the headroom a login-time read depends on"
            )
        return self

    def kite_configured(self) -> bool:
        return bool(self.kite_api_key and self.kite_api_secret)

    def s3_configured(self) -> bool:
        return bool(self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key)

    def token_encryption_configured(self) -> bool:
        return bool(self.kite_token_encryption_key)

    def desk_session_pull_configured(self) -> bool:
        """Can this deployment fetch the desk's token by itself?

        Both halves are required. A target with no key would prompt for a password on a box with
        no terminal — which does not fail, it *hangs*, and a nightly job that hangs is worse than
        one that skips.
        """
        return bool(self.kite_desk_ssh_target and self.kite_desk_ssh_key_path)


@lru_cache(maxsize=1)
def get_provider_settings() -> ProviderSettings:
    return ProviderSettings()
