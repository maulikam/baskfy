"""NSEProvider.fo_bhavcopy — the F&O end-of-day file (docs/fno/07 §1, docs/fno/03 §1).

Asserts the spec: both NSE layouts land on one schema, the URL follows the layout switch of
8 Jul 2024, the raw file is archived before it is parsed, and a file stamped with another
session's date is refused. Nothing reaches the network.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from collections.abc import Mapping
from decimal import Decimal

import pytest

from baskfy_providers.archive import LocalRawArchive, archive_key
from baskfy_providers.errors import UnexpectedPayload
from baskfy_providers.nse import FO_UDIFF_SINCE, KIND_FO_BHAVCOPY, NSEProvider, NSERuntime
from baskfy_providers.records import FO_BHAVCOPY_SCHEMA
from baskfy_providers.retry import RetryPolicy
from baskfy_providers.settings import ProviderSettings

UDIFF_DAY = dt.date(2026, 8, 18)
LEGACY_DAY = dt.date(2024, 6, 28)

_UDIFF_HEADER = (
    b"TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    b"FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    b"PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    b"TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.status_code = 200
        self.content = content


class FakeHttpClient:
    """Serves canned bodies by URL substring and records every request."""

    def __init__(self, routes: Mapping[str, bytes]) -> None:
        self._routes = dict(routes)
        self.requests: list[str] = []

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> FakeResponse:
        del headers
        self.requests.append(url)
        for fragment, body in self._routes.items():
            if fragment in url:
                return FakeResponse(body)
        return FakeResponse(b"")


class InertLimiter:
    def acquire(self, tokens: float = 1.0) -> float:
        del tokens
        return 0.0


def build(
    settings: ProviderSettings, archive: LocalRawArchive, client: FakeHttpClient
) -> NSEProvider:
    return NSEProvider(
        settings,
        archive,
        NSERuntime(
            client=client,
            rate_limiter=InertLimiter(),
            retry_policy=RetryPolicy(max_attempts=1, base_seconds=0.01),
        ),
    )


def _zip(name: str, csv: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(name, csv)
    return buffer.getvalue()


def _udiff(stamp: str = "2026-08-18") -> bytes:
    rows = (
        f"{stamp},{stamp},FO,NSE,STF,1,,SBIN,,2026-08-25,2026-08-25,,,SBIN25AUGFUT,"
        "801,812,799,810.5,810.5,800,808.2,810.35,60000000,1500000,12000,9700000000,9000,F1,750,"
        ",,,,\n"
        f"{stamp},{stamp},FO,NSE,STO,2,,SBIN,,2026-08-25,2026-08-25,820,CE,SBIN25AUG820CE,"
        "9.5,11,8,10.05,10,9,808.2,10.05,3000000,-75000,4000,30000000,3000,F1,750,,,,,\n"
        f"{stamp},{stamp},FO,NSE,IDO,3,,NIFTY,,2026-08-25,2026-08-25,24000,PE,NIFTY25AUG24000PE,"
        "50,60,45,55.25,55,48,24400,55.25,900000,1000,20000,80000000,10000,F1,75,,,,,\n"
    )
    return _zip("BhavCopy_NSE_FO_0_0_0_20260818_F_0000.csv", _UDIFF_HEADER + rows.encode())


def _legacy() -> bytes:
    csv = (
        b"INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,"
        b"CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,\n"
        b"FUTSTK,SBIN,27-Jun-2024,0,XX,840,845,838,842.3,842.3,9000,56000.5,90000000,-2000,"
        b"28-JUN-2024,\n"
        b"OPTSTK,SBIN,25-Jul-2024,860,CE,20,22,19,21.1,21.1,500,300.25,1200000,1500,"
        b"28-JUN-2024,\n"
    )
    return _zip("fo28JUN2024bhav.csv", csv)


class TestUdiffLayout:
    def test_every_contract_lands_on_the_schema(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        frame = build(settings, archive, FakeHttpClient({"BhavCopy_NSE_FO": _udiff()})).fo_bhavcopy(
            UDIFF_DAY
        )
        assert frame.schema == FO_BHAVCOPY_SCHEMA
        assert frame.get_column("instrument").to_list() == ["FUTSTK", "OPTSTK", "OPTIDX"]

    def test_a_future_carries_no_strike_or_option_type_and_keeps_underlying_and_lot(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        frame = build(settings, archive, FakeHttpClient({"BhavCopy_NSE_FO": _udiff()})).fo_bhavcopy(
            UDIFF_DAY
        )
        future = frame.row(0, named=True)
        assert future["option_type"] is None
        assert future["expiry"] == dt.date(2026, 8, 25)
        assert future["settle"] == Decimal("810.35")
        assert future["underlying"] == Decimal("808.2")
        assert future["lot_size"] == 750
        assert future["open_interest"] == 60_000_000
        option = frame.row(1, named=True)
        assert (option["strike"], option["option_type"]) == (Decimal("820"), "CE")
        assert option["oi_change"] == -75_000

    def test_the_url_is_the_udiff_file_and_it_is_archived_before_parsing(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"BhavCopy_NSE_FO": _udiff()})
        build(settings, archive, client).fo_bhavcopy(UDIFF_DAY)
        assert client.requests[-1].endswith(
            "/content/fo/BhavCopy_NSE_FO_0_0_0_20260818_F_0000.csv.zip"
        )
        assert archive.exists(archive_key(KIND_FO_BHAVCOPY, UDIFF_DAY, "zip"))

    def test_a_file_stamped_with_another_session_is_refused(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        client = FakeHttpClient({"BhavCopy_NSE_FO": _udiff(stamp="2026-08-17")})
        with pytest.raises(UnexpectedPayload, match="wrong file"):
            build(settings, archive, client).fo_bhavcopy(UDIFF_DAY)


class TestLegacyLayout:
    def test_days_before_the_switch_read_the_historical_derivatives_file(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        assert LEGACY_DAY < FO_UDIFF_SINCE
        client = FakeHttpClient({"fo28JUN2024bhav": _legacy()})
        build(settings, archive, client).fo_bhavcopy(LEGACY_DAY)
        assert client.requests[-1].endswith(
            "/content/historical/DERIVATIVES/2024/JUN/fo28JUN2024bhav.csv.zip"
        )

    def test_legacy_rows_land_on_the_same_schema_with_absent_fields_null(
        self, settings: ProviderSettings, archive: LocalRawArchive
    ) -> None:
        frame = build(
            settings, archive, FakeHttpClient({"fo28JUN2024bhav": _legacy()})
        ).fo_bhavcopy(LEGACY_DAY)
        assert frame.schema == FO_BHAVCOPY_SCHEMA
        future, option = frame.row(0, named=True), frame.row(1, named=True)
        assert future["option_type"] is None
        assert future["expiry"] == dt.date(2024, 6, 27)
        assert future["underlying"] is None
        assert future["lot_size"] is None
        # VAL_INLAKH is in lakh; the schema's turnover is rupees.
        assert future["turnover"] == Decimal("5600050000.00")
        assert (option["strike"], option["option_type"]) == (Decimal("860"), "CE")


def test_an_empty_file_is_refused(settings: ProviderSettings, archive: LocalRawArchive) -> None:
    client = FakeHttpClient(
        {"BhavCopy_NSE_FO": _zip("BhavCopy_NSE_FO_0_0_0_20260818_F_0000.csv", _UDIFF_HEADER)}
    )
    with pytest.raises(UnexpectedPayload, match="no readable rows"):
        build(settings, archive, client).fo_bhavcopy(UDIFF_DAY)
