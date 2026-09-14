"""Contract tests for ``/trades`` — docs/07 §"Trade history", 07a §16 (14 Sep 2026)."""

from __future__ import annotations

import httpx
import pytest
from api_helpers import bearer, make_user, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

TRADEBOOK = (
    "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,"
    "trade_id,order_id,order_execution_time\n"
    "CUPID,INE509A01011,2025-02-03,NSE,EQ,EQ,buy,false,10.000000,284.56,API-T1,O1,"
    "2025-02-03T10:01:02\n"
    "NIFTY25FEBFUT,,2025-02-03,NFO,FO,,buy,false,75,23000,API-F1,O2,\n"
)


async def _upload(api: httpx.AsyncClient, headers: dict[str, str], text: str) -> httpx.Response:
    return await api.post(
        url("/trades/import"),
        files={"file": ("tradebook.csv", text.encode("utf-8"), "text/csv")},
        headers=headers,
    )


async def test_import_requires_authentication(api: httpx.AsyncClient) -> None:
    response = await _upload(api, {}, TRADEBOOK)
    assert response.status_code == 401


async def test_import_stores_equity_and_a_second_import_adds_nothing(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    _, public_id = await make_user(screener_session, "trades@example.com")
    headers = bearer(public_id)

    first = await _upload(api, headers, TRADEBOOK)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["received"], body["inserted"], body["skipped_non_equity"]) == (1, 1, 1)

    again = (await _upload(api, headers, TRADEBOOK)).json()
    assert (again["inserted"], again["already_present"]) == (0, 1)

    listed = await api.get(url("/trades"), headers=headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["rows"]
    assert [(row["symbol"], row["side"], row["quantity"], row["value"]) for row in rows] == [
        ("CUPID", "BUY", "10.0000", "2845.60")
    ]


async def test_an_unreadable_file_is_refused_with_its_line(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    _, public_id = await make_user(screener_session, "trades-bad@example.com")
    bad = TRADEBOOK.replace("buy,false,10.000000", "hold,false,10.000000")
    response = await _upload(api, bearer(public_id), bad)
    assert response.status_code == 400
    assert "line 2" in response.json()["detail"]
