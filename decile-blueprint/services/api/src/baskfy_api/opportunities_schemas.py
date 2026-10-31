from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baskfy_api.opportunities import MAX_RESULTS, MAX_VALIDATION_DAYS
from baskfy_core.opportunities.config import (
    Direction,
    QuoteRules,
    SetupType,
    StrategyKind,
    SwingFilters,
)
from baskfy_core.opportunities.options import OptionsRequest

DEFAULT_UNIVERSE: Final = "nifty-500"
_DEFAULT_FILTERS: Final = SwingFilters()
DEFAULT_RULES: Final = QuoteRules()


SetupName = Literal["BREAKOUT", "PULLBACK", "RELATIVE_STRENGTH"]
StrategyName = Literal["LONG_CALL", "LONG_PUT", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"]
Slug = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")]


class SwingScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    universe: Slug = DEFAULT_UNIVERSE
    sectors: list[Slug] = Field(default_factory=list, max_length=20)
    setups: list[SetupName] = Field(
        default_factory=lambda: [s.value for s in SetupType], min_length=1, max_length=3
    )
    min_turnover_cr: float = Field(default=_DEFAULT_FILTERS.min_turnover_cr, ge=0, le=10_000)
    min_price: float = Field(default=_DEFAULT_FILTERS.min_price, ge=0, le=1_000_000)
    horizon_min: int = Field(default=_DEFAULT_FILTERS.horizon_min, ge=1, le=120)
    horizon_max: int = Field(default=_DEFAULT_FILTERS.horizon_max, ge=1, le=120)
    atr_pct_min: float = Field(default=_DEFAULT_FILTERS.atr_pct_min, ge=0, le=100)
    atr_pct_max: float = Field(default=_DEFAULT_FILTERS.atr_pct_max, ge=0, le=100)
    min_rr: float = Field(default=_DEFAULT_FILTERS.min_rr, ge=0, le=20)
    limit: int = Field(default=_DEFAULT_FILTERS.limit, ge=1, le=MAX_RESULTS)

    @model_validator(mode="after")
    def _ranges(self) -> SwingScanIn:
        if self.horizon_min > self.horizon_max:
            raise ValueError("horizon_min must not exceed horizon_max")
        if self.atr_pct_min > self.atr_pct_max:
            raise ValueError("atr_pct_min must not exceed atr_pct_max")
        return self

    def filters(self) -> SwingFilters:
        return SwingFilters(
            setups=frozenset(SetupType(s) for s in self.setups),
            sectors=frozenset(self.sectors),
            min_turnover_cr=self.min_turnover_cr,
            min_price=self.min_price,
            horizon_min=self.horizon_min,
            horizon_max=self.horizon_max,
            atr_pct_min=self.atr_pct_min,
            atr_pct_max=self.atr_pct_max,
            min_rr=self.min_rr,
            limit=self.limit,
        )


class ValidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    setup: SetupName
    universe: Slug = DEFAULT_UNIVERSE
    start: dt.date
    end: dt.date
    horizon_max: int = Field(default=20, ge=1, le=120)
    slippage_bps: float = Field(default=10.0, ge=0, le=500)

    @model_validator(mode="after")
    def _window(self) -> ValidateIn:
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if (self.end - self.start).days > MAX_VALIDATION_DAYS:
            raise ValueError("the test window is limited to three years")
        return self


class QuoteRulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_spread_pct: Decimal = Field(default=DEFAULT_RULES.max_spread_pct, gt=0, le=100)
    max_quote_age_s: int = Field(default=DEFAULT_RULES.max_quote_age_s, ge=1, le=86_400)
    min_oi: int = Field(default=DEFAULT_RULES.min_oi, ge=0)
    min_volume: int = Field(default=DEFAULT_RULES.min_volume, ge=0)

    def rules(self) -> QuoteRules:
        return QuoteRules(self.max_spread_pct, self.max_quote_age_s, self.min_oi, self.min_volume)


class OptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9&-]+$")
    direction: Literal["BULLISH", "BEARISH"]
    strategies: list[StrategyName] = Field(
        default_factory=lambda: [s.value for s in StrategyKind], min_length=1, max_length=4
    )
    max_loss_inr: Decimal | None = Field(default=None, gt=0, le=Decimal("100000000"))
    min_dte: int = Field(default=1, ge=0, le=365)
    max_dte: int = Field(default=60, ge=0, le=365)
    quote_rules: QuoteRulesIn = Field(default_factory=QuoteRulesIn)

    @model_validator(mode="after")
    def _dte(self) -> OptionsIn:
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must not exceed max_dte")
        return self

    def request(self) -> OptionsRequest:
        return OptionsRequest(
            underlying=self.symbol.upper(),
            direction=Direction(self.direction),
            strategies=frozenset(StrategyKind(s) for s in self.strategies),
            max_loss_inr=self.max_loss_inr,
            min_dte=self.min_dte,
            max_dte=self.max_dte,
            rules=self.quote_rules.rules(),
        )
