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


async def test_the_list_counts_and_dates_only_the_callers_own_trades(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    """Tenant isolation (DECISIONS-RS R5.28): ``total``, ``first_trade_on`` and
    ``last_trade_on`` describe the caller's trades and nobody else's.

    Until 30 Oct 2026 the bounds aggregate named the table rather than the scoped subquery, so
    SQLAlchemy cross-joined them: the total was (caller's rows) x (every account's rows), and the
    first/last dates spanned every account's trades — another tenant's trading dates, leaked.
    """
    _, mine = await make_user(screener_session, "trades-mine@example.com")
    _, theirs = await make_user(screener_session, "trades-theirs@example.com")
    assert (await _upload(api, bearer(mine), TRADEBOOK)).status_code == 200

    other_book = (
        "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,"
        "trade_id,order_id,order_execution_time\n"
        "CUPID,INE509A01011,2019-01-02,NSE,EQ,EQ,buy,false,5,100.00,OTHER-1,P1,"
        "2019-01-02T10:00:00\n"
        "CUPID,INE509A01011,2026-03-04,NSE,EQ,EQ,sell,false,5,120.00,OTHER-2,P2,"
        "2026-03-04T11:00:00\n"
        "CUPID,INE509A01011,2026-03-05,NSE,EQ,EQ,buy,false,1,121.00,OTHER-3,P3,"
        "2026-03-05T11:00:00\n"
    )
    stored = await _upload(api, bearer(theirs), other_book)
    assert stored.status_code == 200, stored.text
    assert stored.json()["inserted"] == 3

    listed = await api.get(url("/trades"), headers=bearer(mine))
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert (body["total"], body["first_trade_on"], body["last_trade_on"]) == (
        1,
        "2025-02-03",
        "2025-02-03",
    )
    assert [row["trade_id"] for row in body["rows"]] == ["API-T1"]

    # The other account sees its own three, bounded by its own dates — and a symbol filter
    # narrows the bounds with the rows.
    other = (await api.get(url("/trades"), headers=bearer(theirs))).json()
    assert (other["total"], other["first_trade_on"], other["last_trade_on"]) == (
        3,
        "2019-01-02",
        "2026-03-05",
    )
    none = (await api.get(url("/trades"), params={"symbol": "INFY"}, headers=bearer(theirs))).json()
    assert (none["total"], none["first_trade_on"], none["last_trade_on"], none["rows"]) == (
        0,
        None,
        None,
        [],
    )


async def test_an_account_with_no_trades_sees_none_of_anyone_elses(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    _, trader = await make_user(screener_session, "trades-trader@example.com")
    _, empty = await make_user(screener_session, "trades-empty@example.com")
    assert (await _upload(api, bearer(trader), TRADEBOOK)).status_code == 200

    body = (await api.get(url("/trades"), headers=bearer(empty))).json()
    assert body == {"rows": [], "total": 0, "first_trade_on": None, "last_trade_on": None}
