from __future__ import annotations

import datetime as dt
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from baskfy_api import opportunities as service
from baskfy_api import opportunities_llm as llm
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.opportunities_llm import OPTIONS_TOOL, SWING_TOOL
from baskfy_api.opportunities_schemas import OptionsIn, SwingScanIn
from baskfy_api.opportunities_views import options_json, scan_json
from baskfy_api.problems import Problem
from baskfy_core.screener import canonical_json

MAX_THREADS: Final = 10_000
MAX_MESSAGE_CHARS: Final = 2_000
MAX_MESSAGES: Final = 100
_TICKER_STOPWORDS: Final = frozenset(
    {"NSE", "BSE", "INR", "IST", "DTE", "OI", "IV", "ATR", "RR", "CE", "PE", "NIFTY"}
)
_UNIVERSES: Final = {
    "nifty 50": "nifty-50",
    "nifty 100": "nifty-100",
    "nifty 200": "nifty-200",
    "nifty 500": "nifty-500",
    "midcap 150": "nifty-midcap-150",
    "smallcap 250": "nifty-smallcap-250",
}
_LIQUID_CR: Final = 25.0
_HIGHLY_LIQUID_CR: Final = 100.0
_THOUSAND: Final = Decimal(1000)
_LAKH: Final = Decimal(100_000)


class AguiMessage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(min_length=1, max_length=128)
    role: Literal["user", "assistant", "system", "developer", "tool", "activity", "reasoning"]
    content: object = None


class RunAgentInput(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    thread_id: str = Field(alias="threadId", min_length=1, max_length=128)
    run_id: str = Field(alias="runId", min_length=1, max_length=128)
    messages: list[AguiMessage] = Field(max_length=MAX_MESSAGES)
    state: object = None
    tools: list[object] | None = None
    context: list[object] | None = None
    forwarded_props: object = Field(default=None, alias="forwardedProps")


_owners: OrderedDict[str, int] = OrderedDict()
_owners_lock = threading.Lock()


def claim_thread(thread_id: str, user_id: int) -> bool:
    with _owners_lock:
        owner = _owners.get(thread_id)
        if owner is not None and owner != user_id:
            return False
        _owners[thread_id] = user_id
        _owners.move_to_end(thread_id)
        while len(_owners) > MAX_THREADS:
            _owners.popitem(last=False)
        return True


def reset_threads() -> None:
    with _owners_lock:
        _owners.clear()


@dataclass(frozen=True, slots=True)
class Intent:
    tool: str | None
    arguments: dict[str, object]
    note: str


def _last_user_text(messages: list[AguiMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and isinstance(message.content, str):
            return message.content[:MAX_MESSAGE_CHARS]
    return ""


def _range(text: str, unit: str) -> tuple[int, int] | None:
    match = re.search(rf"(\d+)\s*(?:-|\u2013|\u2014|to)\s*(\d+)\s*(?:trading\s+)?(?:{unit})", text)
    if match is None:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (min(low, high), max(low, high))


def _symbol(original: str, state: object) -> str | None:
    match = re.search(r"\bfor\s+(?:the\s+)?([A-Z][A-Z0-9&-]{1,19})\b", original)
    if match and match.group(1) not in _TICKER_STOPWORDS:
        return str(match.group(1))
    for token in re.findall(r"\b[A-Z][A-Z0-9&-]{1,19}\b", original):
        if token not in _TICKER_STOPWORDS:
            return str(token)
    if isinstance(state, Mapping):
        value = state.get("symbol")
        if isinstance(value, str) and value:
            return value.upper()
    return None


def _max_loss(text: str) -> Decimal | None:
    match = re.search(
        r"(?:max(?:imum)?\s+loss|risk(?:ing)?|lose)\D{0,25}?(?:₹|rs\.?|inr)?\s*"
        r"([\d,]+(?:\.\d+)?)\s*(k|thousand|lakh|lac)?",
        text,
    )
    if match is None:
        return None
    amount = Decimal(match.group(1).replace(",", ""))
    unit = match.group(2)
    if unit in {"k", "thousand"}:
        amount *= _THOUSAND
    elif unit in {"lakh", "lac"}:
        amount *= _LAKH
    return amount if amount > 0 else None


def _options_intent(text: str, original: str, state: object) -> Intent:
    strategies: list[str] = []
    if "bull call" in text:
        strategies.append("BULL_CALL_SPREAD")
    if "bear put" in text:
        strategies.append("BEAR_PUT_SPREAD")
    if re.search(r"\blong calls?\b", text):
        strategies.append("LONG_CALL")
    if re.search(r"\blong puts?\b", text):
        strategies.append("LONG_PUT")
    bearish = bool(re.search(r"\b(bearish|downside|fall|decline|drop)\b", text)) or any(
        s in strategies for s in ("BEAR_PUT_SPREAD", "LONG_PUT")
    )
    direction = "BEARISH" if bearish and "bullish" not in text else "BULLISH"
    if not strategies:
        strategies = (
            ["LONG_PUT", "BEAR_PUT_SPREAD"]
            if direction == "BEARISH"
            else ["LONG_CALL", "BULL_CALL_SPREAD"]
        )
    symbol = _symbol(original, state)
    if symbol is None:
        return Intent(None, {}, "Name the underlying, for example: bull call spreads for INFY.")
    args: dict[str, object] = {
        "symbol": symbol,
        "direction": direction,
        "strategies": strategies,
    }
    loss = _max_loss(text)
    if loss is not None:
        args["max_loss_inr"] = str(loss)
    dte = _range(text, "days|dte")
    if dte is not None:
        args["min_dte"], args["max_dte"] = dte
    return Intent(OPTIONS_TOOL, args, f"Comparing {', '.join(strategies)} on {symbol}.")


def _swing_intent(text: str) -> Intent:
    args: dict[str, object] = {}
    horizon = _range(text, "sessions|days")
    if horizon is not None:
        args["horizon_min"], args["horizon_max"] = horizon
    if "highly liquid" in text or "very liquid" in text:
        args["min_turnover_cr"] = _HIGHLY_LIQUID_CR
    elif "liquid" in text:
        args["min_turnover_cr"] = _LIQUID_CR
    setups = [
        name
        for word, name in (
            ("breakout", "BREAKOUT"),
            ("pullback", "PULLBACK"),
            ("relative strength", "RELATIVE_STRENGTH"),
        )
        if word in text
    ]
    if setups:
        args["setups"] = setups
    for phrase, slug in _UNIVERSES.items():
        if phrase in text:
            args["universe"] = slug
    rr = re.search(r"(?:r:r|r/r|risk.{0,4}reward|reward.{0,4}risk)\D{0,12}(\d+(?:\.\d+)?)", text)
    if rr is not None:
        args["min_rr"] = float(rr.group(1))
    return Intent(SWING_TOOL, args, "Scanning swing setups with those filters.")


def parse_intent(original: str, state: object = None) -> Intent:
    text = original.lower()
    if re.search(r"\b(options?|calls?|puts?|spreads?|strikes?|expiry)\b", text):
        return _options_intent(text, original, state)
    if re.search(r"\b(swing|setups?|breakouts?|pullbacks?|relative strength|scan|liquid)\b", text):
        return _swing_intent(text)
    return Intent(
        None,
        {},
        "I can scan swing setups (breakout, pullback, relative strength) or compare long "
        "calls, long puts, bull call spreads and bear put spreads for one underlying.",
    )


async def resolve_intent(original: str, state: object = None) -> Intent:
    config = llm.config_from_env()
    if config is not None and original.strip():
        choice = await llm.choose_tool(original, state, config)
        if choice is not None:
            arguments = choice.arguments
            if choice.tool == OPTIONS_TOOL:
                # Direction and strategies come from the user's words, never the model's guess.
                grounded = _options_intent(original.lower(), original, state).arguments
                arguments = {
                    **arguments,
                    "direction": grounded.get("direction", arguments.get("direction")),
                    "strategies": grounded.get("strategies", arguments.get("strategies")),
                }
            if choice.tool == SWING_TOOL:
                # "liquid"/"highly liquid" have fixed meanings here (25 / 100 crore).
                liquidity = _swing_intent(original.lower()).arguments.get("min_turnover_cr")
                if liquidity is not None:
                    arguments = {**arguments, "min_turnover_cr": liquidity}
            return Intent(
                choice.tool,
                arguments,
                f"Interpreted by {config.model} as {choice.tool}; arguments validated.",
            )
    intent = parse_intent(original, state)
    source = "matched by keywords"
    if config is not None:
        source = "model gave no usable tool call; " + source
    if intent.tool is None:
        return Intent(None, {}, f"{intent.note} ({source}.)")
    return Intent(intent.tool, intent.arguments, f"{intent.note} ({source}.)")


def _event(kind: str, **fields: object) -> str:
    payload: dict[str, object] = {"type": kind}
    payload.update(fields)
    payload["timestamp"] = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    return f"data: {canonical_json(payload)}\n\n"


def _text(message: str) -> list[str]:
    message_id = uuid.uuid4().hex
    return [
        _event("TEXT_MESSAGE_START", messageId=message_id, role="assistant"),
        _event("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=message),
        _event("TEXT_MESSAGE_END", messageId=message_id),
    ]


def summarize_scan(result: Mapping[str, object]) -> str:
    counts = result.get("counts")
    rows = result.get("opportunities")
    if not isinstance(counts, Mapping) or not isinstance(rows, list):
        return "The scan returned no result."
    head = (
        f"Data as of {result.get('data_as_of')} ({result.get('data_status')}). "
        f"{counts.get('qualified')} setups qualified: {counts.get('triggered')} triggered, "
        f"{counts.get('pending')} pending."
    )
    if result.get("data_status") != "FRESH":
        head += f" Not actionable: {result.get('stale_reason')}"
    lines = [head]
    for row in rows[:5]:
        if not isinstance(row, Mapping):
            continue
        entry = row.get("entry")
        price = entry.get("price") if isinstance(entry, Mapping) else None
        score = row.get("score")
        total = score.get("total") if isinstance(score, Mapping) else None
        lines.append(
            f"- {row.get('symbol')}: {row.get('setup')} {row.get('status')}, entry {price}, "
            f"invalidation {row.get('invalidation')}, R:R {row.get('risk_reward')}, "
            f"score {total}"
        )
    lines.append("Scores are rule-based rankings, not probabilities.")
    return "\n".join(lines)


def summarize_options(result: Mapping[str, object]) -> str:
    provider = result.get("provider")
    if isinstance(provider, Mapping) and not provider.get("available"):
        reason, needs = provider.get("reason"), provider.get("dependency")
        return f"Options data unavailable: {reason} Needs: {needs}"
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        warnings = result.get("warnings")
        extra = f" {'; '.join(map(str, warnings))}" if isinstance(warnings, list) else ""
        return f"No candidate passed the quote and risk checks.{extra}"
    lines = [f"{len(candidates)} candidates ({result.get('data_status')}):"]
    for row in candidates[:5]:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('strategy')} {row.get('expiry')}: max loss/lot "
            f"{row.get('max_loss_after_costs_per_lot')} after costs, max profit/lot "
            f"{row.get('max_profit_per_lot') or 'unlimited'}, breakeven {row.get('breakevens')}"
        )
    lines.append("Candidates to evaluate, not recommendations; Greeks are model-derived.")
    return "\n".join(lines)


async def _execute(
    intent: Intent, session: AsyncSession, user_id: int
) -> tuple[dict[str, object], str]:
    now = dt.datetime.now(dt.UTC)
    if intent.tool == SWING_TOOL:
        body = SwingScanIn.model_validate(intent.arguments)
        scan = await service.run_swing_scan(
            session, universe=body.universe, filters=body.filters(), now=now
        )
        payload = scan_json(scan)
        return payload, summarize_scan(payload)
    options = OptionsIn.model_validate(intent.arguments)
    await scoped_sole_user_id(session, user_id, surface="opportunities")
    analysis = await service.run_options_analysis(
        options.request(), now=now, max_quote_age_s=options.quote_rules.max_quote_age_s
    )
    payload = options_json(options.symbol, analysis)
    return payload, summarize_options(payload)


async def run(
    run_input: RunAgentInput,
    *,
    maker: async_sessionmaker[AsyncSession],
    user_id: int,
    disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    thread_id, run_id = run_input.thread_id, run_input.run_id
    yield _event("RUN_STARTED", threadId=thread_id, runId=run_id)
    yield _event("STEP_STARTED", stepName="interpret")
    intent = await resolve_intent(_last_user_text(run_input.messages), run_input.state)
    yield _event("STEP_FINISHED", stepName="interpret")
    if intent.tool is None:
        for chunk in _text(intent.note):
            yield chunk
        yield _event("RUN_FINISHED", threadId=thread_id, runId=run_id)
        return
    for chunk in _text(intent.note):
        yield chunk
    call_id = uuid.uuid4().hex
    parent = uuid.uuid4().hex
    yield _event(
        "TOOL_CALL_START", toolCallId=call_id, toolCallName=intent.tool, parentMessageId=parent
    )
    yield _event("TOOL_CALL_ARGS", toolCallId=call_id, delta=canonical_json(intent.arguments))
    yield _event("TOOL_CALL_END", toolCallId=call_id)
    yield _event("STEP_STARTED", stepName=intent.tool)
    if await disconnected():
        return
    try:
        async with maker() as session:
            payload, summary = await _execute(intent, session, user_id)
    except ValidationError as exc:
        yield _event(
            "RUN_ERROR", message=f"Invalid tool arguments: {exc.errors()}", code="invalid_arguments"
        )
        return
    except service.UniverseUnavailable as exc:
        yield _event("RUN_ERROR", message=str(exc), code="universe_unavailable")
        return
    except Problem as exc:
        yield _event("RUN_ERROR", message=exc.detail, code=exc.type.value)
        return
    yield _event("STEP_FINISHED", stepName=intent.tool)
    yield _event(
        "TOOL_CALL_RESULT",
        messageId=uuid.uuid4().hex,
        toolCallId=call_id,
        content=canonical_json(payload),
        role="tool",
    )
    yield _event(
        "STATE_SNAPSHOT",
        snapshot={"lastTool": intent.tool, "arguments": intent.arguments},
    )
    for chunk in _text(summary):
        yield chunk
    yield _event("RUN_FINISHED", threadId=thread_id, runId=run_id)
