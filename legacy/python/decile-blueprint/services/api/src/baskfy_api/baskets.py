"""Building the current basket — M22's data layer.

Bars → ``compute_factors`` → ``MomentumScan`` → ``baskfy_core.score`` → the basket engine. The
same chain the desk runs, called rather than re-implemented: a second implementation that agreed
today would disagree the first time either side changed, and nobody would know which was right.

The four columns no bar series can produce — ``marketcap``, ``beta``, ``circuits_*`` and
``is_nifty_fno`` — come from the desk's most recent uploaded scan, exactly as
``app/scan_source.py`` takes them. When there is no such scan there is no basket, and the endpoint
says so rather than inventing a beta.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import polars as pl
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core import momentum_scan as ms
from baskfy_core import score as core_score
from baskfy_core.basket import BasketConfig, build_plan
from baskfy_core.score import ScoringConfig


class DeskConfig(ScoringConfig, BasketConfig, Protocol):
    """Both halves at once.

    `score` needs a `ScoringConfig` and `build_plan` needs a `BasketConfig`; the desk's single
    `app.config` module satisfies both, and this says so in the type system rather than casting
    twice at the call sites.
    """

    #: The two protocols disagree about this one: `ScoringConfig` narrows it to `Collection[str]`
    #: and `BasketConfig` widens it to `object`, because its guard is a prefix match rather than a
    #: set membership (`EXCLUDED_SYMBOLS` held "SGBDE31III" while the holding was
    #: "SGBDE31III-GB"). The desk's config really is a collection of strings, so the narrow
    #: declaration wins here and the conflict is resolved in the open rather than cast away.
    EXCLUDED_SYMBOLS: Collection[str]


if TYPE_CHECKING:
    from baskfy_api.routers.baskets import BasketOut

#: The desk lives beside this workspace in the monorepo.
DESK_ROOT = Path(__file__).resolve().parents[5] / "kite-momentum-rebalancer"

#: What the basket is sized against when there is no book to size against. A basket page answers
#: "what does the strategy want", which is a question about the market, not about anyone's money.
NOTIONAL_CAPITAL = 10_000_000.0

#: The six parts the Momentum Quality Score is built from. Named here so the basket page can show
#: why a name is in it, not only that it is.
SCORE_PARTS: tuple[str, ...] = (
    "A_trend",
    "B_momentum",
    "C_sharpe",
    "D_consistency",
    "E_liquidity",
    "F_penalty",
)


def _desk_config() -> DeskConfig:
    """The desk's own configuration object, imported rather than restated.

    Restating the weights here would let the web app show a basket the desk would never trade,
    which is the one failure mode a shared surface must not have. It is returned as
    `ScoringConfig` — the Protocol `baskfy_core.score` already defines — rather than as a proxy
    with a dynamic `__getattr__`, because a proxy would need `Any` and the house rule forbids one.
    """
    import sys  # noqa: PLC0415

    if str(DESK_ROOT) not in sys.path:
        sys.path.insert(0, str(DESK_ROOT))
    from app import config  # noqa: PLC0415

    return cast("DeskConfig", config)


def _carried() -> pl.DataFrame | None:
    """The four borrowed columns, from the desk's newest uploaded scan."""
    import pandas as pd  # noqa: PLC0415

    uploads = sorted(
        (DESK_ROOT / "data" / "uploads").glob("scan_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in uploads:
        frame = pd.read_csv(path, encoding="utf-8-sig")
        wanted = ["symbol", "series", *ms.CARRIED_COLUMNS]
        if all(column in frame.columns for column in wanted):
            return pl.DataFrame(frame[wanted].to_dict(orient="records"))
    return None


async def _bars(session: AsyncSession, as_of: dt.date, symbols: list[str]) -> pl.DataFrame:
    rows = (
        await session.execute(
            text(
                "select i.symbol, b.instrument_id, b.date, b.open, b.high, b.low, b.close, "
                "b.volume, b.close_raw, b.volume_raw "
                "from ohlcv_daily b join instrument i on i.id = b.instrument_id "
                "where i.symbol = any(:syms) and b.date <= :as_of order by i.symbol, b.date"
            ),
            {"syms": symbols, "as_of": as_of},
        )
    ).all()

    def column(index: int) -> list[float]:
        return [float(r[index]) if r[index] is not None else 0.0 for r in rows]

    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "instrument_id": [r[1] for r in rows],
            "date": [r[2] for r in rows],
            "open": column(3),
            "high": column(4),
            "low": column(5),
            "close": column(6),
            "volume": column(7),
            "close_raw": column(8),
            "volume_raw": column(9),
        }
    )


async def build_current_basket(
    session: AsyncSession, as_of: dt.date | None = None
) -> BasketOut | None:
    """The basket the strategy wants, or None when the inputs are not there."""
    from baskfy_api.routers.baskets import BasketOut, BasketRowOut  # noqa: PLC0415

    carried = _carried()
    if carried is None:
        return None

    resolved = (
        as_of
        or (await session.execute(text("select max(date) from ohlcv_daily"))).scalar_one_or_none()
    )
    if resolved is None:
        return None

    symbols = sorted(carried["symbol"].to_list())
    bars = await _bars(session, resolved, symbols)
    if bars.is_empty():
        return None

    trading_days = [
        row[0]
        for row in (
            await session.execute(
                text("select distinct date from ohlcv_daily where date <= :d order by date"),
                {"d": resolved},
            )
        ).all()
    ]

    cfg = _desk_config()
    scan = ms.build(bars, resolved, trading_days, cfg=cfg, carried=carried)

    import pandas as pd  # noqa: PLC0415

    frame = pd.DataFrame(scan.frame.to_dicts())
    scored = core_score.score(core_score.apply_filters(frame, cfg), cfg)

    # An all-cash book: `tradeable` still guards it, so an untouchable instrument cannot appear
    # in a basket page any more than it can appear in a plan.
    plan = build_plan(
        scored,
        [],
        NOTIONAL_CAPITAL,
        cfg=cfg,
        clusters={},
        tradeable=lambda _s: True,
        # Priced from the scan's own closes. `itertuples()` is untyped, so the guard is written
        # as a float conversion rather than a comparison on an object mypy cannot narrow.
        live_prices={
            str(row["symbol"]): float(row["close"])
            for _, row in frame.iterrows()
            if float(row["close"] or 0.0) > 0
        },
    )

    # Extracted once into a plain dict-of-dicts. A pandas row is untyped, and reaching into one
    # per field would need `Any` at every call site; the house rule forbids that and the rule is
    # right — a float that might be a string is exactly what a score component must not be.
    components: dict[str, dict[str, float]] = {
        str(row["symbol"]): {
            part: float(row[part]) for part in SCORE_PARTS if part in row and row[part] is not None
        }
        for _, row in scored.iterrows()
    }
    rows: list[BasketRowOut] = []
    for order in sorted(plan["orders"], key=lambda o: o.get("rank") or 10**6):
        if not order.get("weight"):
            continue
        symbol = str(order["symbol"])
        part = components.get(symbol, {})
        rows.append(
            BasketRowOut(
                rank=int(order.get("rank") or 0),
                symbol=symbol,
                score=float(order.get("score") or 0.0),
                weight=float(order["weight"]),
                ref_price=float(order["ref_price"]),
                stop=float(order["stop"]),
                value=int(order["value"]),
                a_trend=part.get("A_trend"),
                b_momentum=part.get("B_momentum"),
                c_sharpe=part.get("C_sharpe"),
                d_consistency=part.get("D_consistency"),
                e_liquidity=part.get("E_liquidity"),
                f_penalty=part.get("F_penalty"),
            )
        )

    return BasketOut(
        as_of=resolved.isoformat(),
        screen_run_id=scan.screen_run_id,
        data_version=scan.data_version,
        capital=int(plan["capital"]),
        cash_target_pct=float(plan["cash_target_pct"]),
        breadth_above_20dma=float(plan["breadth_above_20dma"]),
        suspect_symbols=list(scan.suspect_symbols),
        rows=rows,
    )
