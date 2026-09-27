"""Worker configuration. Env prefix ``BASKFY_`` (docs/14 §"Naming inside the codebase")."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
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
    #: The benchmark the market gate reads, with ``nifty-500`` as the fallback.
    #:
    #: **`nifty-mid-small-400`, and M87 was WRONG to change it (9 Sep 2026).**
    #:
    #: SW17 (`67df9b4`, 3 Sep 2026) set this deliberately: *"Maulik's call: the book trades mid-
    #: and small-caps, so the tape it asks about is nifty-mid-small-400, with nifty-500 as the
    #: fallback."* The book screens mid- and small-cap breakouts; asking a large-cap-weighted
    #: index whether that tape is healthy is asking about somebody else's market.
    #:
    #: M87 reverted it to `nifty-500` because `docs/swing/04` §8.2 and STANDING-ANSWERS A12 still
    #: said so — treating the disagreement as a bug in the code rather than as documentation that
    #: SW17 had not updated. It even added a test pinning `nifty-500` "against the document",
    #: which locked the wrong value in. The docs are now corrected to match the decision.
    #:
    #: **If this and the docs ever disagree again, the DECISION wins and the doc is the thing to
    #: fix.** Do not "correct" this to nifty-500.
    swing_index_slug: str = "nifty-mid-small-400"
    #: SW11 (STANDING-ANSWERS A4, MD5): the one-shot S2 Kite timing probe at 09:04. Off by
    #: default; on for the one morning Maulik has logged in to Kite before 09:00, and the task
    #: disables itself with a marker file after one good run.
    swing_timing_probe: bool = False
    #: Where the probe writes ``S2-kite-timing.md`` and its ``.done`` marker. The repo's
    #: ``docs/swing/status`` on a checkout; on the box a directory on the state volume.
    swing_timing_probe_dir: str = "docs/swing/status"

    # --- The volume-breakout sleeve (docs/vbt) -------------------------------
    #
    # The worker reads both flags at startup, not per task (``docs/vbt/02``: "A flag is read once
    # per process at startup and never from a form"). ``vbt_execution_enabled`` appears here even
    # though the worker places nothing, because the evening job writes the plan a confirm will
    # execute and has to know whether that confirm would be real: it is what decides the session's
    # ``mode``, whether the first-live halving applies, and whether the DRY_RUN counter moves.
    vbt_execution_enabled: bool = False
    #: Detection moves no money, so this is on. Set it false to silence the nightly step and the
    #: 21:00 retry without removing them.
    vbt_nightly_enabled: bool = True
    vbt_max_open_positions_max: int = Field(default=15, gt=0, le=30)
    vbt_max_position_pct_max: float = Field(default=15.0, gt=0, le=100)
    vbt_stop_pct_max: float = Field(default=15.0, gt=0, le=50)

    # --- The three-weeks-tight sleeve (docs/twt) -----------------------------
    #
    # The worker reads both flags at startup, not per task (``docs/twt/02``: "A flag is read once
    # per process at startup and never from a form"). ``twt_execution_enabled`` appears here even
    # though the worker places nothing, because the evening job writes the plan a confirm will
    # execute and has to know whether that confirm would be real: it is what decides the
    # session's ``mode``, whether the first-live halving applies, and whether the DRY_RUN counter
    # moves.
    twt_execution_enabled: bool = False
    #: Detection moves no money, so this is on. Set it false to silence the nightly step and the
    #: 21:00 retry without removing them.
    twt_nightly_enabled: bool = True
    twt_max_open_positions_max: int = Field(default=15, gt=0, le=30)
    twt_max_position_pct_max: float = Field(default=15.00, gt=0, le=100)
    twt_stop_pct_max: float = Field(default=25.00, gt=0, le=50)
    #: A FLOOR, not a ceiling: ``tw_config.trail_pct`` may not fall below it (DECISIONS-TW
    #: TW0.5). The API mirrors the same four bounds.
    twt_trail_pct_min: float = Field(default=18.00, gt=0, le=100)

    # --- The options run (docs/options/02 Track B, OP2) ------------------------
    #
    # Nine flags and six ceilings, every one system-only and read once at startup. A real options
    # order needs FOUR switches at once for its sleeve — the desk's DRY_RUN off, OPTIONS_ENABLED,
    # INTRADAY_ENABLED and the sleeve's own execution flag — and ``baskfy_core.options.gating.
    # options_gates`` is the one function that ANDs them (``baskfy_worker.options`` calls it). The
    # worker places nothing; it reads the switches so a session's ``mode`` and a scan's label say
    # the same thing the desk would. **Every money flag defaults false and no agent flips one**
    # (``02`` §3). There is no auto-execute flag for any options sleeve (PACK.3).
    options_o1m_execution_enabled: bool = False
    options_o1w_execution_enabled: bool = False
    options_o2_execution_enabled: bool = False
    options_o3_execution_enabled: bool = False
    #: Operational (PACK.11): they move no money. OP3 may default collect/scan true after the
    #: limiter measurement; until then false.
    options_monitor_enabled: bool = False
    options_collect_enabled: bool = False
    options_scan_enabled: bool = False
    #: LV5 (docs/live/PLAN.md): the after-close capture of one-minute equity bars for the swing
    #: book's liquid universe into ``eq_minute_bar`` — read-only market data, ~570 historical
    #: calls at 15:45 on the bulk lane. On by default because the data is the point of LV5 and
    #: nothing else runs at that hour; ``BASKFY_EQ_BARS_ENABLED=false`` turns it off.
    eq_bars_enabled: bool = True
    #: The desk's own product switches and DRY_RUN, read under the desk's unprefixed names and
    #: parsed the desk's way (``app/config.py``): DRY_RUN is on unless exactly ``false``; the two
    #: switches are on only when exactly ``true``. Strings, so pydantic's lenient bool parsing
    #: ("yes", "1", "on") cannot read a value as live that the desk reads as off.
    options_desk_dry_run: str = Field(default="true", validation_alias="DRY_RUN")
    options_desk_options_enabled: str = Field(default="false", validation_alias="OPTIONS_ENABLED")
    options_desk_intraday_enabled: str = Field(default="false", validation_alias="INTRADAY_ENABLED")
    #: ``02`` "Ceilings". A setting may sit below them, never above; mirrored in the API.
    options_risk_per_trade_inr_max: Decimal = Field(default=Decimal("25000"), gt=0)
    options_risk_pct_max: Decimal = Field(default=Decimal("1.0"), gt=0, le=100)
    options_max_lots_max: int = Field(default=10, gt=0, le=100)
    options_book_daily_loss_inr_max: Decimal = Field(default=Decimal("30000"), gt=0)
    options_book_monthly_loss_inr_max: Decimal = Field(default=Decimal("75000"), gt=0)
    #: No hard exit later than this (``02`` Track C §1). Never later than 15:00 in v1.
    options_hard_exit_latest: dt.time = dt.time(15, 0)

    # --- The FO run (docs/fno/02 Track B, FO2) -----------------------------------
    #
    # Operational, moves no money: the nightly F&O bhavcopy ingest (FO2) and, later, the nightly
    # scans (FO4). ``02`` Track B: flipped "after FO2's bhavcopy ingest is green". Read once at
    # startup, never a form field. The FO money flags are the desk's (FO6) and not read here.
    fno_scan_enabled: bool = False
    #: The desk's FO monitor switch, read here only to keep ``FNO_WEEKLY`` dark while there is no
    #: book to summarise (FO10). Operational, moves no money, default false.
    fno_monitor_enabled: bool = False

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
    #: AF 3.11: ``task_reject_on_worker_lost`` requeues without bumping Celery retries; this caps
    #: how many times a single message may come back after a worker death (OOM loop guard).
    task_max_redeliveries: int = Field(default=3, ge=0)
    task_retry_backoff_seconds: int = Field(default=60, gt=0)

    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
