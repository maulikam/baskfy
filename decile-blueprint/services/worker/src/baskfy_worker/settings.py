"""Worker configuration. Env prefix ``BASKFY_`` (docs/14 §"Naming inside the codebase")."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BASKFY_",
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: docs/02: "Cache / queue broker — Redis 7".
    redis_url: str = "redis://localhost:6380/0"
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    #: docs/09 §"Kite specifics": "run with bounded concurrency (<= 3)".
    ingest_concurrency: int = Field(default=3, gt=0)

    #: docs/09 §"Data-quality gate" assertion 1.
    gate_min_bar_ratio: float = Field(default=0.9, gt=0, le=1)
    #: assertion 2 — the return magnitude that demands an explanation.
    gate_max_unexplained_return: float = Field(default=0.5, gt=0)
    #: assertion 5 — membership within this fraction of the universe's nominal size.
    gate_membership_tolerance: float = Field(default=0.05, gt=0, le=1)
    #: assertion 6 — index level agreement.
    gate_index_level_tolerance: float = Field(default=0.001, gt=0)
    #: assertion 8 — sigma multiple for the ret_12m median.
    gate_ret_median_sigma: float = Field(default=3.0, gt=0)

    # --- The swing book (docs/swing) -----------------------------------------
    #
    # The worker reads all three Track-B flags at startup, not per task. ``docs/swing/02``: "A
    # flag is read once per process at startup ... and never from a form." A task that re-read
    # the environment could change behaviour halfway through a night; a process that reads it
    # once either runs the premarket scan or does not, and the log line says which.
    #
    # ``swing_execution_enabled`` appears here even though the worker places nothing, because the
    # EOD job's ladder reads simulated closes while it is false and real ones once it is true
    # (PACK.6). A worker that could not see the flag would summarise the wrong book.
    swing_execution_enabled: bool = False
    swing_monitor_enabled: bool = False
    swing_ep_premarket_enabled: bool = False
    #: The ceilings, mirrored from the API's settings so that a task writing ``sw_config`` (the
    #: first-live-session countdown, the exposure rung) validates against the same numbers the
    #: form does. Mirrored rather than imported: the worker does not depend on ``baskfy_api``.
    swing_risk_per_trade_pct_max: float = Field(default=1.0, gt=0, le=5)
    swing_max_position_pct_max: float = Field(default=30.0, gt=0, le=100)
    swing_max_open_positions_max: int = Field(default=20, gt=0, le=50)
    #: The benchmark the market gate reads (``docs/swing/04`` §8.2), ``nifty-50`` as fallback.
    swing_index_slug: str = "nifty-500"

    #: Task-level retry budget (deliverable 1).
    task_max_retries: int = Field(default=3, ge=0)
    task_retry_backoff_seconds: int = Field(default=60, gt=0)

    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
