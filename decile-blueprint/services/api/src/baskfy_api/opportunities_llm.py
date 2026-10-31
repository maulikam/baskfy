from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import httpx
from pydantic import BaseModel, ValidationError

from baskfy_api.opportunities_schemas import OptionsIn, SwingScanIn

URL_ENV: Final = "BASKFY_LLM_URL"
MODEL_ENV: Final = "BASKFY_LLM_MODEL"
TIMEOUT_ENV: Final = "BASKFY_LLM_TIMEOUT_S"
DEFAULT_MODEL: Final = "qwen3:8b"
DEFAULT_TIMEOUT_S: Final = 20.0
SWING_TOOL: Final = "scan_swing"
OPTIONS_TOOL: Final = "compare_options"
_DROPPED_OPTION_FIELDS: Final = frozenset({"quote_rules"})

SYSTEM_PROMPT: Final = (
    "You route requests for an Indian equity (NSE) trading-opportunity scanner. "
    "Call exactly one tool when the request asks for swing setups or for option structures "
    "on one underlying; otherwise call no tool. Use only arguments the user stated or clearly "
    "implied and leave everything else unset. Symbols are NSE trading symbols in upper case. "
    "Holding horizons are in trading sessions. Money is in Indian rupees. Never invent prices "
    "or symbols; if no symbol is named or selected, call no tool."
)


@dataclass(frozen=True, slots=True)
class LlmConfig:
    url: str
    model: str
    timeout_s: float


@dataclass(frozen=True, slots=True)
class ToolChoice:
    tool: str
    arguments: dict[str, object]


def config_from_env() -> LlmConfig | None:
    url = os.environ.get(URL_ENV, "").strip().rstrip("/")
    if not url:
        return None
    model = os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
    try:
        timeout = float(os.environ.get(TIMEOUT_ENV, "") or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    return LlmConfig(url=url, model=model, timeout_s=max(1.0, timeout))


def _parameters(model: type[BaseModel], dropped: frozenset[str] = frozenset()) -> dict[str, object]:
    schema = model.model_json_schema()
    properties = schema.get("properties")
    props = (
        {k: v for k, v in properties.items() if k not in dropped}
        if isinstance(properties, dict)
        else {}
    )
    required = schema.get("required")
    return {
        "type": "object",
        "properties": props,
        "required": [r for r in required if r not in dropped] if isinstance(required, list) else [],
        "additionalProperties": False,
    }


def tool_definitions() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": SWING_TOOL,
                "description": (
                    "Scan NSE stocks for swing setups (BREAKOUT, PULLBACK, RELATIVE_STRENGTH) "
                    "on completed daily bars, with liquidity, horizon, volatility and "
                    "reward-to-risk filters. min_turnover_cr is average daily turnover in "
                    "rupee crore; 'liquid' means about 25."
                ),
                "parameters": _parameters(SwingScanIn),
            },
        },
        {
            "type": "function",
            "function": {
                "name": OPTIONS_TOOL,
                "description": (
                    "Compare listed option structures (LONG_CALL, LONG_PUT, BULL_CALL_SPREAD, "
                    "BEAR_PUT_SPREAD) on one NSE underlying for a BULLISH or BEARISH view, "
                    "optionally within a maximum loss in rupees and a days-to-expiry window."
                ),
                "parameters": _parameters(OptionsIn, _DROPPED_OPTION_FIELDS),
            },
        },
    ]


def _state_symbol(state: object) -> str | None:
    if isinstance(state, Mapping):
        symbol = state.get("symbol")
        if isinstance(symbol, str) and symbol.strip():
            return symbol.strip().upper()
    return None


def _grounded_symbol(arguments: dict[str, object], text: str, state: object) -> str | None:
    selected = _state_symbol(state)
    proposed = arguments.get("symbol")
    if isinstance(proposed, str) and proposed.strip():
        candidate = proposed.strip().upper()
        if candidate == selected or re.search(rf"\b{re.escape(candidate)}\b", text):
            return candidate
    return selected


def _normalised(model: type[BaseModel], arguments: dict[str, object]) -> dict[str, object]:
    # Small models add stray keys and write slugs as names ("NIFTY 200"); keep the call.
    known = model.model_fields
    out = {k: v for k, v in arguments.items() if k in known}
    universe = out.get("universe")
    if isinstance(universe, str):
        out["universe"] = re.sub(r"[^a-z0-9]+", "-", universe.strip().lower()).strip("-")
    return out


def _validated(
    name: str, arguments: dict[str, object], text: str, state: object
) -> ToolChoice | None:
    try:
        if name == SWING_TOOL:
            arguments = _normalised(SwingScanIn, arguments)
            SwingScanIn.model_validate(arguments)
            return ToolChoice(name, arguments)
        if name == OPTIONS_TOOL:
            arguments = _normalised(OptionsIn, arguments)
            symbol = _grounded_symbol(arguments, text, state)
            if symbol is None:
                return None
            arguments = {**arguments, "symbol": symbol}
            OptionsIn.model_validate(arguments)
            return ToolChoice(name, arguments)
    except ValidationError:
        return None
    return None


def _first(value: object, key: str) -> object:
    if not isinstance(value, Mapping):
        return None
    item = value.get(key)
    if isinstance(item, list):
        return item[0] if item else None
    return item


def parse_tool_call(body: object, text: str = "", state: object = None) -> ToolChoice | None:
    function = _first(_first(_first(_first(body, "choices"), "message"), "tool_calls"), "function")
    if not isinstance(function, Mapping):
        return None
    name, raw = function.get("name"), function.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
    if not isinstance(name, str) or not isinstance(raw, dict):
        return None
    arguments = {str(k): v for k, v in raw.items() if v is not None}
    return _validated(name, arguments, text, state)


async def choose_tool(
    text: str,
    state: object,
    config: LlmConfig,
    client: httpx.AsyncClient | None = None,
) -> ToolChoice | None:
    system = SYSTEM_PROMPT
    selected = _state_symbol(state)
    if selected is not None:
        system += f" The candidate currently selected on screen is {selected}."
    payload = {
        "model": config.model,
        "temperature": 0,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "tools": tool_definitions(),
        "tool_choice": "auto",
    }
    owned = client is None
    http = client or httpx.AsyncClient(timeout=config.timeout_s)
    try:
        response = await http.post(f"{config.url}/chat/completions", json=payload)
        response.raise_for_status()
        return parse_tool_call(response.json(), text, state)
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if owned:
            await http.aclose()
