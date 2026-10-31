from __future__ import annotations

import json

import httpx
import pytest

from baskfy_api import opportunities_agui as agui
from baskfy_api import opportunities_llm as llm

CONFIG = llm.LlmConfig(url="http://llm.test/v1", model="qwen3:8b", timeout_s=5)


def _reply(name: str, arguments: object) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ]
    }


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def test_config_is_off_without_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(llm.URL_ENV, raising=False)
    assert llm.config_from_env() is None
    monkeypatch.setenv(llm.URL_ENV, "http://localhost:11434/v1/")
    config = llm.config_from_env()
    assert config is not None
    assert config.url == "http://localhost:11434/v1"
    assert config.model == llm.DEFAULT_MODEL


def test_tool_definitions_expose_no_quote_rules_or_order_tool() -> None:
    by_name = {
        str(t["function"]["name"]): t
        for t in llm.tool_definitions()
        if isinstance(t["function"], dict)
    }
    assert set(by_name) == {llm.SWING_TOOL, llm.OPTIONS_TOOL}
    assert "quote_rules" not in json.dumps(by_name[llm.OPTIONS_TOOL])


async def test_a_valid_tool_call_is_accepted_and_sent_once() -> None:
    seen: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json=_reply(llm.SWING_TOOL, {"horizon_min": 5, "horizon_max": 15})
        )

    async with _client(httpx.MockTransport(handle)) as client:
        choice = await llm.choose_tool("liquid setups 5-15 sessions", None, CONFIG, client)
    assert choice == llm.ToolChoice(llm.SWING_TOOL, {"horizon_min": 5, "horizon_max": 15})
    assert len(seen) == 1
    assert seen[0]["model"] == "qwen3:8b"
    assert seen[0]["temperature"] == 0


async def test_options_symbol_is_upper_cased_or_taken_from_state() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_reply(llm.OPTIONS_TOOL, {"direction": "BULLISH", "max_loss_inr": 5000})
        )

    async with _client(httpx.MockTransport(handle)) as client:
        choice = await llm.choose_tool("spreads for this one", {"symbol": "infy"}, CONFIG, client)
    assert choice is not None
    assert choice.arguments["symbol"] == "INFY"


@pytest.mark.parametrize(
    "body",
    [
        _reply(llm.SWING_TOOL, {"limit": 100000}),
        _reply(llm.OPTIONS_TOOL, {"symbol": "INFY", "direction": "SIDEWAYS"}),
        _reply("place_order", {"symbol": "INFY"}),
        {"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
        {"choices": []},
    ],
)
async def test_invalid_or_absent_tool_calls_are_refused(body: dict[str, object]) -> None:
    async with _client(httpx.MockTransport(lambda _r: httpx.Response(200, json=body))) as client:
        assert await llm.choose_tool("anything", None, CONFIG, client) is None


async def test_a_failing_model_server_is_not_an_error() -> None:
    async with _client(httpx.MockTransport(lambda _r: httpx.Response(503))) as client:
        assert await llm.choose_tool("scan", None, CONFIG, client) is None


async def test_resolve_falls_back_to_keywords_when_the_model_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm.URL_ENV, "http://llm.test/v1")

    async def declines(*_args: object) -> llm.ToolChoice | None:
        return None

    monkeypatch.setattr(llm, "choose_tool", declines)
    intent = await agui.resolve_intent("Find liquid swing setups for the next 5-15 sessions")
    assert intent.tool == llm.SWING_TOOL
    assert intent.arguments["horizon_max"] == 15


async def test_resolve_prefers_the_model_when_it_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(llm.URL_ENV, "http://llm.test/v1")

    async def answers(*_args: object) -> llm.ToolChoice | None:
        return llm.ToolChoice(llm.OPTIONS_TOOL, {"symbol": "TCS", "direction": "BEARISH"})

    monkeypatch.setattr(llm, "choose_tool", answers)
    intent = await agui.resolve_intent("what about tcs downside")
    assert intent.tool == llm.OPTIONS_TOOL
    assert "qwen3:8b" in intent.note


async def test_thinking_is_off_and_the_selected_symbol_is_given_to_the_model() -> None:
    seen: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": []})

    async with _client(httpx.MockTransport(handle)) as client:
        await llm.choose_tool("spreads", {"symbol": "infy"}, CONFIG, client)
    assert seen[0]["reasoning_effort"] == "none"
    assert "INFY" in json.dumps(seen[0]["messages"])


@pytest.mark.parametrize(
    ("text", "state", "proposed", "expected"),
    [
        ("spreads for this candidate", None, "THIS", None),
        ("spreads for this candidate", None, "symbol", None),
        ("spreads for this candidate", {"symbol": "INFY"}, "THIS", "INFY"),
        ("bear put spreads on TCS", None, "TCS", "TCS"),
        ("bear put spreads on TCS", {"symbol": "INFY"}, "RELIANCE", "INFY"),
    ],
)
async def test_only_a_typed_or_selected_symbol_is_accepted(
    text: str, state: dict[str, str] | None, proposed: str, expected: str | None
) -> None:
    body = _reply(llm.OPTIONS_TOOL, {"symbol": proposed, "direction": "BULLISH"})
    async with _client(httpx.MockTransport(lambda _r: httpx.Response(200, json=body))) as client:
        choice = await llm.choose_tool(text, state, CONFIG, client)
    if expected is None:
        assert choice is None
    else:
        assert choice is not None
        assert choice.arguments["symbol"] == expected
