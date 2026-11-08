"""The index service: one master of NSE equity indices, their daily bars, stats and constituents.

Seeds ``index_master`` from ``data/nse_equity_indices.json`` (when the file is reachable) and joins
Kite tokens from ``instrument`` rows of type INDEX by normalised name. The Rust API's refresh
re-runs the join against Kite's live instrument dump.

Revision ID: 0060_index_service
Revises: 0059_f3_directional
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0060_index_service"
down_revision: str | None = "0059_f3_directional"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(precision=14, scale=2)
PCT = sa.Numeric(precision=10, scale=2)

#: Master name -> Kite tradingsymbol where the names do not normalise to the same key.
#: Mirrors ``rust/crates/baskfy-api/src/indices/master.rs`` ALIASES.
ALIASES: dict[str, str] = {
    "Nifty Midcap Select": "NIFTY MID SELECT",
    "Nifty Smallcap 250": "NIFTY SMLCAP 250",
    "Nifty Smallcap 50": "NIFTY SMLCAP 50",
    "Nifty Smallcap 100": "NIFTY SMLCAP 100",
    "Nifty LargeMidcap 250": "NIFTY LARGEMID250",
    "Nifty MidSmallcap 400": "NIFTY MIDSML 400",
    "Nifty500 Multicap 50:25:25": "NIFTY500 MULTICAP",
    "Nifty Total Market": "NIFTY TOTAL MKT",
    "Nifty India FPI 150": "NIFTY FPI 150",
    "Nifty Private Bank": "NIFTY PVT BANK",
    "Nifty Consumer Durables": "NIFTY CONSR DURBL",
    "Nifty Financial Services": "NIFTY FIN SERVICE",
    "Nifty Financial Services 25/50": "NIFTY FINSRV25 50",
    "Nifty Financial Services Ex-Bank": "NIFTY FINSEREXBNK",
    "Nifty MidSmall Healthcare": "NIFTY MIDSML HLTH",
    "Nifty500 Healthcare": "NIFTY500 HEALTH",
    "Nifty India Consumption": "NIFTY CONSUMPTION",
    "Nifty Infrastructure": "NIFTY INFRA",
    "Nifty India Manufacturing": "NIFTY INDIA MFG",
    "Nifty Midcap Liquid 15": "NIFTY MID LIQ 15",
    "Nifty Services Sector": "NIFTY SERV SECTOR",
    "Nifty100 Liquid 15": "NIFTY100 LIQ 15",
    "Nifty India Corporate Group Index - Tata Group 25% Cap": "NIFTY TATA 25 CAP",
    "Nifty100 ESG Sector Leaders": "NIFTY100ESGSECLDR",
    "Nifty India Digital": "NIFTY IND DIGITAL",
    "Nifty500 Multicap India Manufacturing 50:30:20": "NIFTY MULTI MFG",
    "Nifty500 Multicap Infrastructure 50:30:20": "NIFTY MULTI INFRA",
    "Nifty India Select 5 Corporate Groups (MAATR)": "NIFTY CORP MAATR",
    "Nifty India Railways PSU": "NIFTY RAILWAYS PSU",
    "Nifty India Internet": "NIFTY INTERNET",
    "Nifty India Infrastructure & Logistics": "NIFTY INFRALOG",
    "Nifty Conglomerate 50": "NIFTYCONGLOMERATE",
    "Nifty 100 Equal Weight": "NIFTY100 EQL WGT",
    "Nifty100 Low Volatility 30": "NIFTY100 LOWVOL30",
    "Nifty Dividend Opportunities 50": "NIFTY DIV OPPS 50",
    "Nifty Alpha Low-Volatility 30": "NIFTY ALPHALOWVOL",
    "Nifty100 Quality 30": "NIFTY100 QUALTY30",
    "Nifty Growth Sectors 15": "NIFTY GROWSECT 15",
    "Nifty 50 Dividend Points": "NIFTY50 DIV POINT",
    "Nifty50 Equal weight": "NIFTY50 EQL WGT",
    "Nifty50 PR 1x Inverse": "NIFTY50 PR 1X INV",
    "Nifty50 PR 2x Leverage": "NIFTY50 PR 2X LEV",
    "Nifty50 TR 1x Inverse": "NIFTY50 TR 1X INV",
    "Nifty50 TR 2x Leverage": "NIFTY50 TR 2X LEV",
    "Nifty200 Quality 30": "NIFTY200 QUALTY30",
    "Nifty Midcap150 Quality 50": "NIFTY M150 QLTY50",
    "Nifty200 Momentum 30": "NIFTY200MOMENTM30",
    "Nifty Midcap150 Momentum 50": "NIFTYM150MOMNTM50",
    "Nifty500 Quality 50": "NIFTY500 QLTY50",
    "Nifty500 Low Volatility 50": "NIFTY500 LOWVOL50",
    "Nifty500 Multifactor MQVLv 50": "NIFTY500 MQVLV50",
    "Nifty500 Flexicap Quality 30": "NIFTY500 FLEXICAP",
    "Nifty Total Market Momentum Quality 50": "NIFTY TMMQ 50",
}


def _norm(name: str) -> str:
    s = name.upper()
    if s.startswith("NSE INDEX "):
        s = s[len("NSE INDEX ") :]
    return re.sub(r"[^A-Z0-9]", "", s.replace("&", " AND "))


def _csv_slug(name: str) -> str:
    return "ind_" + re.sub(r"[^a-z0-9]", "", name.lower()) + "list"


def _master_file() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data" / "nse_equity_indices.json"
        if candidate.is_file():
            return candidate
    return None


def _seed() -> None:
    path = _master_file()
    if path is None:
        return
    groups: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
    bind = op.get_bind()
    kite = bind.execute(
        sa.text(
            "SELECT symbol, name, kite_token FROM instrument "
            "WHERE instrument_type = 'INDEX' AND kite_token IS NOT NULL"
        )
    ).all()
    by_symbol = {r.symbol: r for r in kite}
    by_key: dict[str, object] = {}
    for r in kite:
        by_key.setdefault(_norm(r.name), r)
        by_key.setdefault(_norm(r.symbol), r)
    order = 0
    for grp, names in groups.items():
        for name in names:
            order += 1
            hit = by_symbol.get(ALIASES[name]) if name in ALIASES else by_key.get(_norm(name))
            bind.execute(
                sa.text(
                    "INSERT INTO index_master (name, grp, kite_token, kite_tradingsymbol, "
                    "nse_csv_slug, live, sort_order) VALUES (:n, :g, :t, :s, :c, :l, :o) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {
                    "n": name,
                    "g": grp,
                    "t": getattr(hit, "kite_token", None),
                    "s": getattr(hit, "symbol", None),
                    "c": _csv_slug(name),
                    "l": hit is not None,
                    "o": order,
                },
            )


def upgrade() -> None:
    op.create_table(
        "index_master",
        sa.Column("id", sa.SmallInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("grp", sa.Text(), nullable=False),
        sa.Column("kite_token", sa.BigInteger(), nullable=True),
        sa.Column("kite_tradingsymbol", sa.Text(), nullable=True),
        sa.Column("nse_csv_slug", sa.Text(), nullable=True),
        sa.Column("live", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_constituents_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_master")),
        sa.UniqueConstraint("name", name=op.f("uq_index_master_name")),
    )
    op.create_table(
        "index_bar_daily",
        sa.Column("index_id", sa.SmallInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.ForeignKeyConstraint(
            ["index_id"], ["index_master.id"], name=op.f("fk_index_bar_daily_index_id_index_master"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("index_id", "date", name=op.f("pk_index_bar_daily")),
    )
    op.create_table(
        "index_stats_daily",
        sa.Column("index_id", sa.SmallInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("chg_pct", PCT, nullable=True),
        sa.Column("dma10", PRICE, nullable=True),
        sa.Column("dma20", PRICE, nullable=True),
        sa.Column("dma50", PRICE, nullable=True),
        sa.Column("dma200", PRICE, nullable=True),
        sa.Column("vs20_pct", PCT, nullable=True),
        sa.Column("cross_10_20", sa.Boolean(), nullable=True),
        sa.Column("ret_5d", PCT, nullable=True),
        sa.Column("ret_30d", PCT, nullable=True),
        sa.Column("ret_1y", PCT, nullable=True),
        sa.Column("hi_52w", PRICE, nullable=True),
        sa.Column("lo_52w", PRICE, nullable=True),
        sa.Column("dist_52w_hi_pct", PCT, nullable=True),
        sa.ForeignKeyConstraint(
            ["index_id"], ["index_master.id"],
            name=op.f("fk_index_stats_daily_index_id_index_master"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("index_id", "date", name=op.f("pk_index_stats_daily")),
    )
    op.create_table(
        "index_constituents",
        sa.Column("index_id", sa.SmallInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("isin", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("weight", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["index_id"], ["index_master.id"],
            name=op.f("fk_index_constituents_index_id_index_master"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("index_id", "symbol", name=op.f("pk_index_constituents")),
    )
    op.create_index("ix_index_constituents_symbol", "index_constituents", ["symbol"])
    _seed()


def downgrade() -> None:
    op.drop_index("ix_index_constituents_symbol", table_name="index_constituents")
    op.drop_table("index_constituents")
    op.drop_table("index_stats_daily")
    op.drop_table("index_bar_daily")
    op.drop_table("index_master")
