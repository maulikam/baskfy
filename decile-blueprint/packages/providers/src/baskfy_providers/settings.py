"""Provider configuration. Env prefix ``BASKFY_`` (docs/14 §"Naming inside the codebase")."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
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

    #: docs/09: "Rate limit ~ 3 req/s".
    kite_rate_limit_per_second: float = Field(default=3.0, gt=0)
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
