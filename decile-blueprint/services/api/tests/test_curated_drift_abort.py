"""Audit 4.15 — curated drift must not continue on an aborted SQLAlchemy session."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.routers.curated_drift import _desk_holdings


@pytest.mark.asyncio
async def test_desk_holdings_rolls_back_on_sqlalchemy_error() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=SQLAlchemyError("aborted"))
    session.rollback = AsyncMock()

    out = await _desk_holdings(cast(AsyncSession, session))

    assert out == []
    session.rollback.assert_awaited_once()
