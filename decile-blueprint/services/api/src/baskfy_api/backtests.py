"""Backtest bookkeeping — the service half of ``/backtests`` (Prompt 15 deliverables 3-5).

What lives here rather than in the router: the concurrency caps, the R2 artefact keys and their
signed URLs, the Redis progress channel, and the translation between a
:class:`baskfy_core.backtest.BacktestResult` and the ``backtest`` row docs/04 defines.

Where the artefacts go, and why one column is enough
-----------------------------------------------------
docs/10: "Large artefacts (trades, per-day holdings) go to R2; `backtest.metrics` and a
downsampled equity curve live in Postgres for fast page loads." docs/04 gives the table a single
``trades_key``. Three artefacts are written — the trade log, the per-rebalance holdings and the
full daily equity curve — under one deterministic prefix derived from ``public_id``, and
``trades_key`` records the trade log's key. The other two are ``artefact_key(public_id, …)`` of
the same prefix, so the one column still answers the only question it exists to answer: were the
artefacts written, and where. Recorded in ``docs/DECISIONS.md`` §15.

"Signed URL", locally and in production
----------------------------------------
docs/07: "`GET /backtests/{id}/export` -> CSV/Parquet signed URL". A Cloudflare R2 bucket can
mint a presigned GET; a directory on a laptop cannot. So the URL is signed *by this service* —
an HMAC over ``(public_id, artefact, expiry)`` keyed on the JWT secret, redeemed at a download
route that streams the object. When the deployment does have a bucket the same route streams
from it. The property docs/07 is asking for — a link that expires and that nobody can forge — is
the property this provides, on both. ``docs/DECISIONS.md`` §15.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.backtest import (
    BacktestConfig,
    BacktestResult,
    FragilityReport,
    HoldingSnapshot,
    Trade,
)
from baskfy_core.backtest_metrics import (
    DrawdownPoint,
    Metrics,
    compute_metrics,
    downsample_curve,
    drawdown_series,
    metrics_hash,
    monthly_returns,
)
from baskfy_core.models import Backtest
from baskfy_core.models.base import JsonObject

__all__ = [
    "ACTIVE_STATUSES",
    "ARTEFACTS",
    "BACKTEST_GLOBAL_CONCURRENCY",
    "BACKTEST_USER_CONCURRENCY",
    "ArtefactName",
    "ConcurrencyExceeded",
    "artefact_key",
    "build_payload",
    "capacity_check",
    "events_channel",
    "new_public_id",
    "progress_key",
    "sign_download",
    "verify_download",
]

PUBLIC_ID_BYTES: Final = 12

#: PROMPTS.md Prompt 15 §4: "a per-user concurrency cap of 1, and a global cap." These are the
#: defaults; the live numbers are ``BASKFY_BACKTEST_USER_CONCURRENCY`` and
#: ``BASKFY_BACKTEST_GLOBAL_CONCURRENCY``, which the router passes in. The global cap is **not in
#: the bundle** — docs/03 §"Scaling plan" step 4 only says the queue gets its own worker pool —
#: so eight is a chosen number, and it is configuration rather than a constant precisely because
#: the right value depends on how big that pool is. ``docs/DECISIONS.md`` §15.
BACKTEST_USER_CONCURRENCY: Final = 1
BACKTEST_GLOBAL_CONCURRENCY: Final = 8

#: The statuses that occupy a slot. docs/04: ``queued|running|done|failed``.
ACTIVE_STATUSES: Final[tuple[str, ...]] = ("queued", "running")

#: docs/10 §Artefacts. ``trades`` is the one docs/04 gives a column to.
ARTEFACTS: Final[tuple[str, ...]] = ("trades", "holdings", "equity")
ArtefactName = str

#: How long a signed download link lives. Long enough to click, short enough that a link pasted
#: into a chat window stops working before anyone else finds it.
DOWNLOAD_TTL_SECONDS: Final = 15 * 60

_OBJECT_PREFIX: Final = "backtests"


class ConcurrencyExceeded(Exception):
    """Too many runs already in flight — for this user, or for the whole service."""

    def __init__(self, scope: str, limit: int, retry_after_seconds: int = 60) -> None:
        super().__init__(f"{scope} backtest concurrency limit of {limit} reached")
        self.scope = scope
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


def new_public_id() -> str:
    """24 hex characters, like every other ``public_id`` in this service."""
    return secrets.token_hex(PUBLIC_ID_BYTES)


def artefact_key(public_id: str, artefact: ArtefactName) -> str:
    """``backtests/{public_id}/{artefact}.csv`` — deterministic, so one stored key implies all."""
    if artefact not in ARTEFACTS:
        raise ValueError(f"{artefact!r} is not one of {ARTEFACTS}")
    return f"{_OBJECT_PREFIX}/{public_id}/{artefact}.csv"


def progress_key(public_id: str) -> str:
    """Where the latest progress frame is kept, so a client that connects late still sees one."""
    return f"backtest:progress:{public_id}"


def events_channel(public_id: str) -> str:
    """The pub/sub channel the SSE endpoint subscribes to (Prompt 15 §4)."""
    return f"backtest:events:{public_id}"


# ---------------------------------------------------------------------------
# Concurrency (Prompt 15 §4)
# ---------------------------------------------------------------------------


async def capacity_check(
    session: AsyncSession,
    user_id: int,
    *,
    per_user: int = BACKTEST_USER_CONCURRENCY,
    global_limit: int = BACKTEST_GLOBAL_CONCURRENCY,
) -> None:
    """Refuse a run that would breach either cap.

    Counted in PostgreSQL rather than in Redis: the ``backtest`` row is the record of a run
    existing, and a counter that can drift from it would eventually either wedge a user out of
    their own queue or let the global cap be exceeded. The count is the truth by construction.
    """
    mine = (
        await session.execute(
            select(func.count())
            .select_from(Backtest)
            .where(Backtest.user_id == user_id, Backtest.status.in_(ACTIVE_STATUSES))
        )
    ).scalar_one()
    if int(mine) >= per_user:
        raise ConcurrencyExceeded("per-user", per_user)
    everyone = (
        await session.execute(
            select(func.count()).select_from(Backtest).where(Backtest.status.in_(ACTIVE_STATUSES))
        )
    ).scalar_one()
    if int(everyone) >= global_limit:
        raise ConcurrencyExceeded("global", global_limit, retry_after_seconds=120)


# ---------------------------------------------------------------------------
# Signed download links
# ---------------------------------------------------------------------------


def _signature(settings: Settings, public_id: str, artefact: str, expires: int) -> str:
    message = f"{public_id}:{artefact}:{expires}".encode()
    key = settings.jwt_secret.encode("utf-8") or b"baskfy-unconfigured"
    return (
        base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest())
        .decode("ascii")
        .rstrip("=")
    )


@dataclass(frozen=True, slots=True)
class SignedDownload:
    artefact: str
    expires: int
    token: str

    @property
    def expires_at(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.expires, tz=dt.UTC)


def sign_download(
    settings: Settings,
    public_id: str,
    artefact: ArtefactName,
    *,
    now: dt.datetime | None = None,
    ttl_seconds: int = DOWNLOAD_TTL_SECONDS,
) -> SignedDownload:
    if artefact not in ARTEFACTS:
        raise ValueError(f"{artefact!r} is not one of {ARTEFACTS}")
    moment = now or dt.datetime.now(tz=dt.UTC)
    expires = int((moment + dt.timedelta(seconds=ttl_seconds)).timestamp())
    return SignedDownload(artefact, expires, _signature(settings, public_id, artefact, expires))


def verify_download(  # noqa: PLR0913 - the signature covers four independent inputs
    settings: Settings,
    public_id: str,
    artefact: str,
    expires: int,
    token: str,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Constant-time comparison, and the expiry checked *after* it, so neither leaks the other."""
    moment = now or dt.datetime.now(tz=dt.UTC)
    if artefact not in ARTEFACTS:
        return False
    expected = _signature(settings, public_id, artefact, expires)
    if not hmac.compare_digest(expected, token):
        return False
    return expires >= int(moment.timestamp())


# ---------------------------------------------------------------------------
# CSV artefacts (docs/10 §Artefacts)
# ---------------------------------------------------------------------------

#: docs/10 §Artefacts names the trade log's columns in this order.
TRADE_COLUMNS: Final[tuple[str, ...]] = (
    "date",
    "symbol",
    "side",
    "quantity",
    "price",
    "notional",
    "cost",
    "reason",
    "realised_pnl",
)
HOLDING_COLUMNS: Final[tuple[str, ...]] = (
    "rebalance_date",
    "executed_on",
    "symbol",
    "name",
    "rank",
    "target_weight",
    "quantity",
    "price",
    "value",
    "actual_weight",
)
EQUITY_COLUMNS: Final[tuple[str, ...]] = (
    "date",
    "equity",
    "cash",
    "invested",
    "drawdown",
    "benchmark",
)


def _csv(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    """Minimal RFC 4180, ``\\n`` line endings — the same shape as ``baskfy_api.csv_export``."""
    lines = [",".join(header)]
    lines.extend(",".join(_cell(value) for value in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if any(character in text for character in ',"\n'):
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text


def trades_csv(trades: Sequence[Trade]) -> bytes:
    return _csv(
        TRADE_COLUMNS,
        [
            (
                trade.date.isoformat(),
                trade.symbol,
                trade.side.value,
                trade.quantity,
                trade.price,
                trade.notional,
                trade.cost,
                trade.reason.value,
                trade.realised_pnl,
            )
            for trade in trades
        ],
    )


def holdings_csv(holdings: Sequence[HoldingSnapshot]) -> bytes:
    return _csv(
        HOLDING_COLUMNS,
        [
            (
                holding.rebalance_date.isoformat(),
                holding.executed_on.isoformat(),
                holding.symbol,
                holding.name,
                holding.rank,
                holding.target_weight,
                holding.quantity,
                holding.price,
                holding.value,
                holding.actual_weight,
            )
            for holding in holdings
        ],
    )


def equity_csv(result: BacktestResult) -> bytes:
    drawdowns = drawdown_series(result.dates, result.equity)
    return _csv(
        EQUITY_COLUMNS,
        [
            (
                day.isoformat(),
                equity,
                cash,
                invested,
                f"{point.drawdown:.10f}",
                level,
            )
            for day, equity, cash, invested, level, point in zip(
                result.dates,
                result.equity,
                result.cash,
                result.invested,
                result.benchmark,
                drawdowns,
                strict=True,
            )
        ],
    )


def artefact_bytes(result: BacktestResult) -> dict[str, bytes]:
    return {
        "trades": trades_csv(result.trades),
        "holdings": holdings_csv(result.holdings),
        "equity": equity_csv(result),
    }


# ---------------------------------------------------------------------------
# The stored payload
# ---------------------------------------------------------------------------


def fragility_payload(report: FragilityReport | None) -> list[JsonObject]:
    """docs/10 §"honesty features" — the spread, as the page renders it.

    Each variant reports the three numbers that decide whether a result survives: what it earned,
    what it gave back, and how the compounded rate moved. A user comparing five CAGRs can see the
    dispersion without doing arithmetic.
    """
    if report is None:
        return []
    rows: list[JsonObject] = []
    for run in (report.base, *report.variants):
        metrics = compute_metrics(run.result)
        rows.append(
            {
                "label": run.label,
                "description": run.description,
                "cagr": metrics.as_dict()["cagr"],
                "total_return": metrics.as_dict()["total_return"],
                "max_drawdown": metrics.as_dict()["max_drawdown"],
                "final_equity": str(run.result.final_equity),
                "trades": len(run.result.trades),
            }
        )
    return rows


def assumptions(config: BacktestConfig, result: BacktestResult) -> list[str]:
    """docs/10 §"What to show the user (honesty features)" — the assumptions panel, in prose.

        "An assumptions panel stating execution timing, costs, dividend policy, and the fact that
         index membership before the first available NSE constituent file is reconstructed."

    Written here rather than in the web app so the API, the CSV export and the page cannot state
    different assumptions about the same run.
    """
    costs = config.costs
    lines = [
        "Every order is decided on the rebalance date's close and filled at the NEXT trading "
        "day's opening price. Nothing is ever filled at the price that triggered it.",
        f"Costs of {costs.total_bps} bps ({costs.brokerage_bps} brokerage + {costs.stt_bps} STT "
        f"+ {costs.slippage_bps} slippage) are charged on the traded notional of every fill, on "
        "both the buy and the sell leg. The impact model is flat, not size-dependent.",
        "Share counts are whole. What rounding leaves behind stays in cash and earns "
        + ("the benchmark's return." if config.cash_policy.value == "benchmark" else "nothing."),
        # M39 corrected this. It used to say cash dividends were "already inside" the series,
        # which stopped being true when M28 applied the 47 share-count actions and deliberately
        # not the 38 dividend-shaped ones. A backtest that quietly omits the dividend yield is
        # one thing; one that tells the reader it included it is another.
        "Prices are adjusted for splits and bonuses. They are NOT adjusted for cash dividends, "
        "so every return here is a PRICE return and is lower than a total return by roughly the "
        f"dividend yield. The dividend policy is {config.dividends.value!r}.",
        "Index membership before the first NSE constituent file NSE publishes is RECONSTRUCTED "
        "from the earliest file available (index_member_daily.source). Any part of this run that "
        "falls in that period rests on membership we inferred, not membership we observed.",
        "A delisted holding is sold at its last available close and the loss is taken. Nothing "
        "is forward-filled and nothing is dropped.",
        "Past backtest results do not predict future results.",
    ]
    if config.risk_overlay.enabled:
        lines.insert(
            3,
            f"The {config.risk_overlay.rule.value} risk overlay is on: the book goes to cash "
            "whenever the benchmark closes below its 200-day moving average.",
        )
    lines.extend(result.notes)
    return lines


@dataclass(frozen=True, slots=True)
class StoredResult:
    """Exactly what goes into the ``backtest`` row."""

    metrics: JsonObject
    equity_curve: JsonObject
    trades_key: str
    metrics_hash: str


def build_payload(
    public_id: str,
    config: BacktestConfig,
    result: BacktestResult,
    fragility: FragilityReport | None,
    *,
    extra_notes: Sequence[str] = (),
) -> StoredResult:
    """docs/10: metrics and a downsampled curve in Postgres; everything large in R2."""
    metrics: Metrics = compute_metrics(result)
    payload: JsonObject = dict(metrics.as_dict())
    payload["metrics_hash"] = metrics_hash(metrics, config, result.data_version)
    payload["data_version"] = result.data_version
    payload["rebalance_count"] = len(result.rebalance_dates)
    payload["fragility"] = fragility_payload(fragility)
    payload["assumptions"] = [*assumptions(config, result), *extra_notes]
    payload["monthly_returns"] = [
        {"month": row.key, "return": round(row.ret, 10)}
        for row in monthly_returns(result.dates, result.equity)
    ]
    curve: JsonObject = {
        "points": downsample_curve(result.dates, result.equity, result.benchmark),
        "drawdown": [
            {"date": point.date.isoformat(), "drawdown": round(point.drawdown, 10)}
            for point in _downsampled_drawdown(result)
        ],
    }
    resolved_hash = payload["metrics_hash"]
    return StoredResult(
        metrics=payload,
        equity_curve=curve,
        trades_key=artefact_key(public_id, "trades"),
        metrics_hash=str(resolved_hash),
    )


def _downsampled_drawdown(result: BacktestResult) -> Sequence[DrawdownPoint]:
    points = drawdown_series(result.dates, result.equity)
    stride = max(1, len(points) // 1500)
    kept: list[DrawdownPoint] = list(points[::stride])
    if points and kept[-1] is not points[-1]:
        kept.append(points[-1])
    return kept


def progress_frame(  # noqa: PLR0913 - one parameter per field of the SSE frame
    public_id: str,
    status: str,
    *,
    stage: str,
    completed: int,
    total: int,
    as_of: dt.date | None = None,
    detail: str | None = None,
) -> str:
    """One SSE payload, as JSON. Shared by the publisher and the endpoint that replays it."""
    body: Mapping[str, object] = {
        "public_id": public_id,
        "status": status,
        "stage": stage,
        "completed": completed,
        "total": total,
        "percent": 0 if total <= 0 else min(100, round(100 * completed / total)),
        "as_of": None if as_of is None else as_of.isoformat(),
        "detail": detail,
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True)
