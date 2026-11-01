from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from baskfy_api import opportunities as service
from baskfy_api import opportunities_agui as agui
from baskfy_api import opportunities_llm as llm
from baskfy_api.opportunities_schemas import OptionsIn, SwingScanIn, ValidateIn
from baskfy_api.routers.opportunities import router
from baskfy_core.market_hours_cb import IST
from baskfy_core.opportunities.config import DataStatus, Direction, QuoteRules, StrategyKind
from baskfy_core.opportunities.options import OptionsRequest

SOURCES = Path(__file__).resolve().parents[1] / "src" / "baskfy_api"
FORBIDDEN = ("place_order", "OrderGateway", "place_gtt", "baskfy_execution", "modify_order")


@pytest.mark.parametrize(
    "name",
    [
        "opportunities.py",
        "opportunities_agui.py",
        "opportunities_views.py",
        "opportunities_schemas.py",
        "routers/opportunities.py",
    ],
)
def test_opportunity_sources_name_no_order_path(name: str) -> None:
    source = (SOURCES / name).read_text(encoding="utf-8")
    for token in FORBIDDEN:
        assert token not in source


def test_router_exposes_no_mutating_broker_route() -> None:
    paths = {str(getattr(route, "path", "")) for route in router.routes}
    assert not any("order" in p or "execute" in p for p in paths)


def test_expected_session_waits_for_the_data_cutoff() -> None:
    days = [dt.date(2026, 10, 28), dt.date(2026, 10, 29), dt.date(2026, 10, 30)]
    before = dt.datetime(2026, 10, 30, 13, 0, tzinfo=IST)
    after = dt.datetime(2026, 10, 30, 18, 30, tzinfo=IST)
    assert service.expected_session(before, days) == dt.date(2026, 10, 29)
    assert service.expected_session(after, days) == dt.date(2026, 10, 30)


def test_freshness_marks_a_missing_session_stale_and_no_data_unavailable() -> None:
    assert service.freshness(None, dt.date(2026, 10, 29)).status is DataStatus.UNAVAILABLE
    stale = service.freshness(dt.date(2026, 10, 28), dt.date(2026, 10, 29))
    assert stale.status is DataStatus.STALE
    assert stale.reason is not None
    assert "2026-10-29" in stale.reason
    fresh = service.freshness(dt.date(2026, 10, 29), dt.date(2026, 10, 29))
    assert fresh.status is DataStatus.FRESH


def test_scan_input_is_validated() -> None:
    with pytest.raises(ValidationError):
        SwingScanIn.model_validate({"horizon_min": 20, "horizon_max": 5})
    with pytest.raises(ValidationError):
        SwingScanIn.model_validate({"limit": 10_000})
    with pytest.raises(ValidationError):
        SwingScanIn.model_validate({"universe": "'; drop table"})
    with pytest.raises(ValidationError):
        SwingScanIn.model_validate({"setups": ["MOONSHOT"]})
    with pytest.raises(ValidationError):
        SwingScanIn.model_validate({"unexpected": 1})
    with pytest.raises(ValidationError):
        ValidateIn.model_validate({"setup": "BREAKOUT", "start": "2018-01-01", "end": "2026-01-01"})
    with pytest.raises(ValidationError):
        OptionsIn.model_validate({"symbol": "INFY", "direction": "SIDEWAYS"})
    with pytest.raises(ValidationError):
        OptionsIn.model_validate({"symbol": "INFY", "direction": "BULLISH", "max_loss_inr": -1})


def test_cache_key_changes_with_filters_and_data_date() -> None:
    a = SwingScanIn().filters()
    b = SwingScanIn(min_rr=2.5).filters()
    day = dt.date(2026, 10, 29)
    assert service.cache_key("nifty-500", a, day) == service.cache_key("nifty-500", a, day)
    assert service.cache_key("nifty-500", a, day) != service.cache_key("nifty-500", b, day)
    assert service.cache_key("nifty-500", a, day) != service.cache_key(
        "nifty-500", a, dt.date(2026, 10, 30)
    )


async def test_options_without_a_kite_session_report_the_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "quotes_permitted", lambda: False)
    request = OptionsRequest(
        underlying="INFY",
        direction=Direction.BULLISH,
        strategies=frozenset(StrategyKind),
        max_loss_inr=Decimal("5000"),
        min_dte=1,
        max_dte=45,
        rules=QuoteRules(),
    )
    analysis = await service.run_options_analysis(
        request, now=dt.datetime(2026, 10, 30, 6, 0, tzinfo=dt.UTC), max_quote_age_s=60
    )
    assert analysis.status is DataStatus.UNAVAILABLE
    assert not analysis.provider.available
    assert analysis.provider.dependency == service.KITE_DEPENDENCY
    assert analysis.result is None


def test_threads_belong_to_one_user() -> None:
    agui.reset_threads()
    assert agui.claim_thread("t-1", 7)
    assert agui.claim_thread("t-1", 7)
    assert not agui.claim_thread("t-1", 8)
    assert agui.claim_thread("t-2", 8)


def test_example_swing_prompt_becomes_validated_filters() -> None:
    intent = agui.parse_intent("Find liquid swing setups for the next 5\u201315 trading sessions.")
    assert intent.tool == llm.SWING_TOOL
    body = SwingScanIn.model_validate(intent.arguments)
    assert (body.horizon_min, body.horizon_max) == (5, 15)
    assert body.min_turnover_cr >= 25


def test_example_options_prompt_becomes_validated_arguments() -> None:
    intent = agui.parse_intent(
        "Compare bull call spreads for INFY within my maximum loss of ₹5,000."
    )
    assert intent.tool == llm.OPTIONS_TOOL
    body = OptionsIn.model_validate(intent.arguments)
    assert body.symbol == "INFY"
    assert body.direction == "BULLISH"
    assert body.strategies == ["BULL_CALL_SPREAD"]
    assert body.max_loss_inr == Decimal("5000")


def test_options_prompt_falls_back_to_the_selected_candidate() -> None:
    intent = agui.parse_intent(
        "Compare bull call spreads for this candidate within a max loss of 8k",
        {"symbol": "tcs"},
    )
    assert intent.arguments["symbol"] == "TCS"
    assert intent.arguments["max_loss_inr"] == "8000"


def test_unmatched_request_gets_help_and_no_tool() -> None:
    assert agui.parse_intent("what's the weather").tool is None


def _events(chunks: list[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for chunk in chunks:
        assert chunk.startswith("data: ")
        assert chunk.endswith("\n\n")
        out.append(json.loads(chunk[len("data: ") :]))
    return out


async def test_agui_run_streams_a_well_formed_event_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BASKFY_LLM_URL", raising=False)
    run = agui.RunAgentInput.model_validate(
        {
            "threadId": "t-9",
            "runId": "r-1",
            "messages": [{"id": "m1", "role": "user", "content": "hello"}],
        }
    )

    async def never() -> bool:
        return False

    maker: async_sessionmaker[AsyncSession] = async_sessionmaker()
    chunks = [c async for c in agui.run(run, maker=maker, user_id=1, disconnected=never)]
    events = _events(chunks)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "RUN_STARTED"
    assert kinds[-1] == "RUN_FINISHED"
    assert kinds[1:3] == ["STEP_STARTED", "STEP_FINISHED"]
    assert kinds[3:6] == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]
    assert events[0]["threadId"] == "t-9"
    assert events[0]["runId"] == "r-1"


async def test_agui_tool_argument_errors_end_in_run_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BASKFY_LLM_URL", raising=False)
    monkeypatch.setattr(
        agui,
        "parse_intent",
        lambda _text, _state=None: agui.Intent(llm.SWING_TOOL, {"limit": 0}, "scan"),
    )
    run = agui.RunAgentInput.model_validate(
        {
            "threadId": "t-10",
            "runId": "r-2",
            "messages": [{"id": "m", "role": "user", "content": "scan"}],
        }
    )

    async def never() -> bool:
        return False

    maker: async_sessionmaker[AsyncSession] = async_sessionmaker()
    events = _events([c async for c in agui.run(run, maker=maker, user_id=1, disconnected=never)])
    kinds = [e["type"] for e in events]
    assert "TOOL_CALL_START" in kinds
    assert kinds[-1] == "RUN_ERROR"
    assert events[-1]["code"] == "invalid_arguments"


def test_reward_to_risk_phrasing_sets_the_minimum() -> None:
    intent = agui.parse_intent("pullbacks in nifty 200 with reward to risk at least 2")
    assert intent.arguments["min_rr"] == 2.0
    assert intent.arguments["universe"] == "nifty-200"
