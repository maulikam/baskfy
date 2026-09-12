"""READ-ONLY audit: which filed holdings does the broker still report?

PKTEA was one phantom. This answers whether there are others, for every row in
`portfolio_holding`, without writing anything.

`holdings_for_broker` makes exactly one network call, Kite's read-only GET /portfolio/holdings,
and never writes (`broker_holdings.py:301`). Nothing here calls `sync_holdings_into_portfolio`,
so no sell is attributed and no reconciliation item is raised. The point is to SEE the gap before
deciding what to do about it.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from baskfy_api.broker_holdings import holdings_for_broker
from baskfy_api.settings import get_settings
from baskfy_core.models import Instrument, Portfolio, PortfolioHolding


async def main() -> None:
    result = holdings_for_broker("zerodha")
    source = getattr(result, "source", "?")
    # The field is `rows`, not `holdings`. Reading the wrong name on 12 Sep 2026 produced
    # "broker_symbols=0" and an audit that called all 20 holdings phantoms — including two written
    # from a live sync that morning. `HoldingsResult.__post_init__` makes that reading impossible
    # anyway: a row-bearing source with no rows raises. A wrong attribute name is a silent zero,
    # and a silent zero in THIS script is a script that recommends selling everything.
    rows = list(result.rows)
    broker: dict[str, float] = {
        str(h.symbol): float(h.quantity) + float(h.collateral_quantity) for h in rows
    }
    print(f"broker_source={source} broker_symbols={len(broker)}")

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            q = (
                select(Instrument.symbol, PortfolioHolding.quantity, Portfolio.name,
                       Portfolio.is_broker_pile)
                .join(PortfolioHolding, PortfolioHolding.instrument_id == Instrument.id)
                .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
            )
            filed = [(sym, float(q_), name, pile) for sym, q_, name, pile in (await s.execute(q)).all()]
    finally:
        await engine.dispose()

    print(f"filed_rows={len(filed)}")
    if source != "live":
        print(f"VERDICT=inconclusive reason=broker_read_not_live({source})")
        return

    matched, phantom = [], []
    for sym, qty, name, pile in sorted(filed):
        (matched if sym in broker else phantom).append((sym, qty, name, pile))
    print(f"matched={len(matched)} phantom={len(phantom)}")
    for sym, qty, name, _ in phantom:
        print(f"  PHANTOM {sym} qty={qty:g} in '{name}' — broker does not report it")
    extra = sorted(set(broker) - {s for s, _, _, _ in filed})
    print(f"broker_has_unfiled={len(extra)}" + (f" {extra}" if extra else ""))
    # Print the broker's own list so a "phantom" cannot be a naming mismatch read as a sale.
    print("broker_reports=" + ",".join(sorted(broker)))


asyncio.run(main())
