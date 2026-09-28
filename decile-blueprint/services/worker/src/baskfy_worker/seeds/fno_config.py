"""The FO run's config rows for the sole user (``docs/fno/03`` §6, ``04`` §3, FO2).

Where each number comes from — **none is an agent's choice**:

* **F1 capital ₹25,00,000**, for NIFTY and BANKNIFTY together — Maulik, in session, 25 Sep 2026
  (``docs/fno/DECISIONS-FO.md`` M.2, raising M.1's ₹10 lakh; ``01`` §1 "Size"; ``04`` §3). At
  1.0 % that is ₹25,000 max loss per structure, the per-trade ceiling. Seeding it moves no
  money: every FO execution flag stays false.
* **F2 capital ₹0** — Q1 was asked for F1 only (``01`` §1b "Size"): paper runs one lot and live
  refuses ``NO_SLEEVE_CAPITAL``.
* **Risk 1.0 %** per trade for both (``04`` §3), **max lots 2** (``04`` §3 ``fo_max_lots``).
* **F3 capital ₹0** — DECISIONS-FO M.5 (28 Sep 2026), QUESTIONS Q10 open: paper one lot.
* **Max open positions**: F1 **2** — one per underlying (``04`` §1 ``f1_max_open_per_underlying``)
  times its two underlyings (``f1_underlyings``); F2 **5** (``04`` §10 ``f2_max_open``); F3 **2**
  (``04`` §11, one spread per underlying).
* ``fo_book_config`` is written with its column defaults (``monthly_pause_inr`` 0), exactly as
  the options seed writes ``op_book_config``: the pause amount is a person's number, set on the
  settings form under the ₹75,000 ceiling (FO10 owns what 0 means).

Risk, lots and max-open are read from ``baskfy_core.fno.config.DEFAULT_FNO_CONFIG``, as the
options seed reads ``DEFAULT_OPTIONS_CONFIG``, so the seed and ``04`` cannot drift. The capitals
are Maulik's numbers, not config defaults, and stay written here.

Idempotent (house rule 7): ``ON CONFLICT DO NOTHING`` everywhere, so a re-seed never resets a
number a person chose. Each sleeve row the seed does insert gets a ``fo_config_audit`` row naming
its source ("every write goes through settings_audit", ``03`` §6); a re-seed that inserts nothing
audits nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno.config import DEFAULT_FNO_CONFIG, F1_SEED_CAPITAL_INR
from baskfy_core.models import FoBookConfig, FoConfigAudit, FoSleeveConfig

SEEDED_BY: Final = "seed"


@dataclass(frozen=True, slots=True)
class SleeveSeed:
    sleeve: str
    capital_inr: Decimal
    risk_per_trade_pct: Decimal
    max_lots: int
    max_open_positions: int
    source: str


SLEEVE_SEEDS: Final[tuple[SleeveSeed, ...]] = (
    SleeveSeed(
        sleeve="F1",
        capital_inr=F1_SEED_CAPITAL_INR.quantize(Decimal("0.01")),  # round at write time
        risk_per_trade_pct=DEFAULT_FNO_CONFIG.f1.risk_per_trade_pct,
        max_lots=DEFAULT_FNO_CONFIG.common.max_lots,
        max_open_positions=DEFAULT_FNO_CONFIG.f1.max_open_per_underlying
        * len(DEFAULT_FNO_CONFIG.f1.underlyings),
        source="Maulik, 25 Sep 2026 (DECISIONS-FO M.2): F1 capital Rs 25 lakh, both "
        "underlyings together",
    ),
    SleeveSeed(
        sleeve="F2",
        capital_inr=Decimal("0.00"),
        risk_per_trade_pct=DEFAULT_FNO_CONFIG.f2.risk_per_trade_pct,
        max_lots=DEFAULT_FNO_CONFIG.common.max_lots,
        max_open_positions=DEFAULT_FNO_CONFIG.f2.max_open,
        source="docs/fno/01 §1b: Q1 was asked for F1 only; F2 paper one lot, live refuses "
        "NO_SLEEVE_CAPITAL",
    ),
    SleeveSeed(
        sleeve="F3",
        capital_inr=Decimal("0.00"),
        risk_per_trade_pct=DEFAULT_FNO_CONFIG.f3.risk_per_trade_pct,
        max_lots=DEFAULT_FNO_CONFIG.common.max_lots,
        max_open_positions=DEFAULT_FNO_CONFIG.f3.max_open_per_underlying
        * len(DEFAULT_FNO_CONFIG.f3.underlyings),
        source="docs/fno/DECISIONS-FO M.5 (28 Sep 2026): F3 capital Rs 0 until QUESTIONS Q10 is "
        "answered; paper one lot, live refuses NO_SLEEVE_CAPITAL",
    ),
)


async def seed_fno(session: AsyncSession, user_id: int) -> dict[str, int]:
    """The book row and the three sleeve rows for ``user_id``. Returns rows written per table."""
    book = await session.execute(
        insert(FoBookConfig)
        .values(user_id=user_id, updated_by=SEEDED_BY)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    inserted = (
        (
            await session.execute(
                insert(FoSleeveConfig)
                .values(
                    [
                        {
                            "user_id": user_id,
                            "sleeve": s.sleeve,
                            "capital_inr": s.capital_inr,
                            "risk_per_trade_pct": s.risk_per_trade_pct,
                            "max_lots": s.max_lots,
                            "max_open_positions": s.max_open_positions,
                            "updated_by": SEEDED_BY,
                        }
                        for s in SLEEVE_SEEDS
                    ]
                )
                .on_conflict_do_nothing(index_elements=["user_id", "sleeve"])
                .returning(FoSleeveConfig.sleeve)
            )
        )
        .scalars()
        .all()
    )
    by_sleeve = {s.sleeve: s for s in SLEEVE_SEEDS}
    for sleeve in sorted(inserted):
        seed = by_sleeve[sleeve]
        session.add(
            FoConfigAudit(
                user_id=user_id,
                scope=sleeve,
                key="capital_inr",
                old_value=None,
                new_value=str(seed.capital_inr),
                changed_by=SEEDED_BY,
                note=seed.source,
            )
        )
    await session.flush()
    return {
        "fo_book_config": int(getattr(book, "rowcount", 0) or 0),
        "fo_sleeve_config": len(inserted),
        "fo_config_audit": len(inserted),
    }
