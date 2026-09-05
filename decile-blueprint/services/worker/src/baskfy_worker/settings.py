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
    #:
    #: **This was ``nifty-mid-small-400`` and that was a bug (5 Sep 2026).** `04` §8.2 and
    #: STANDING-ANSWERS A12 both name NIFTY 500, `baskfy_worker.deps.PipelineDependencies` has
    #: always defaulted to ``nifty-500``, and the backtest whose expectancy the go-live decision
    #: rested on was run with ``index_slug='nifty-500'`` — but
    #: `providers.build_pipeline_dependencies` reads *this* field, so the deployed gate ran on
    #: the mid-small index instead.
    #:
    #: It changed verdicts, not just numbers. On 3 Sep 2026 NIFTY MidSmall 400 read
    #: ``fast 21,586.40 > slow 21,580.77`` and the gate was **GREEN** (new entries allowed);
    #: NIFTY 500 on the same session read ``fast 23,449.05 < slow 23,524.42``, which is **RED**.
    #: The gate decides whether the book may enter at all, so the wrong benchmark is the wrong
    #: answer to the only question it is asked.
    #:
    #: `test_worker_settings_index_slug_matches_deps` holds this equal to `deps`' default so the
    #: two cannot drift apart again.
    swing_index_slug: str = "nifty-500"
    #: SW11 (STANDING-ANSWERS A4, MD5): the one-shot S2 Kite timing probe at 09:04. Off by
    #: default; on for the one morning Maulik has logged in to Kite before 09:00, and the task
    #: disables itself with a marker file after one good run.
    swing_timing_probe: bool = False
    #: Where the probe writes ``S2-kite-timing.md`` and its ``.done`` marker. The repo's
    #: ``docs/swing/status`` on a checkout; on the box a directory on the state volume.
    swing_timing_probe_dir: str = "docs/swing/status"

    # --- SW18: the 08:45 Kite login nudge (docs/swing/DECISIONS-SW SW18.1) ------
    #
    # Kite kills the access token every morning and nothing on this box may hold a Zerodha
    # password or a TOTP seed, so the only automatable part is the reminder: at 08:45 and again
    # at 09:05 the worker checks for a usable token and, finding none, emails one login link.
    # Off by default, and with no recipient it logs and does nothing — a box that has not been
    # told where to send the link must not start guessing.
    kite_login_nudge_enabled: bool = False
    #: One email address. Never logged, not even its domain.
    kite_login_nudge_to: str = ""

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
