"""FO6 — ``assert_overnight_option_is_covered`` in the gateway (``docs/fno/02`` §1, ``04`` §2).

The rule: an NRML option order carrying a ``fo_plan`` reference passes only if, in the book after
it — the broker's positions (net of this plan's own fills) + the plan's filled legs + the order —
every (underlying, expiry, type) has each short covered by at least as many longs at strikes
further from the money. It runs before the risk layer and before any network call, so every
refusal here is asserted against a recording broker that saw **nothing**.

The predicate is FO1's ``baskfy_core.fno.covered.uncovered``, injected into the gateway
(DECISIONS-FO FO6.1); a gateway with no predicate wired refuses every FO option order. Orders
without a ``fo_plan`` reference — every O1-O3 order — meet the old ``assert_not_overnight_option``
unchanged, and that is asserted here too.

Everything runs with ``dry_run=False`` and every switch on, so the only thing standing between a
naked short and the (fake) broker is the guard under test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from baskfy_execution import OrderGateway, ProductGates, RiskManager, TenantIds
from baskfy_execution.guards import (
    FoPlanRef,
    UncoveredOptionError,
    assert_overnight_option_is_covered,
)

from baskfy_core.fno.covered import OptionPosition, net_of_plan, uncovered
from baskfy_core.options.config import OptionType

TENANT = TenantIds(user_id=1, broker_account_id=1)
EXPIRY = dt.date(2026, 11, 24)
LOT = 75
CE, PE = OptionType.CE, OptionType.PE

LONG_PUT = OptionPosition("NIFTY", EXPIRY, PE, Decimal(23500), LOT)
LONG_CALL = OptionPosition("NIFTY", EXPIRY, CE, Decimal(25500), LOT)
SHORT_PUT = OptionPosition("NIFTY", EXPIRY, PE, Decimal(24000), -LOT)
SHORT_CALL = OptionPosition("NIFTY", EXPIRY, CE, Decimal(25000), -LOT)
#: ``04`` §2: longs enter before shorts; shorts exit before longs.
ENTRY = (LONG_PUT, LONG_CALL, SHORT_PUT, SHORT_CALL)
COVER = {SHORT_PUT: LONG_PUT, SHORT_CALL: LONG_CALL}
#: A short call with no long above it anywhere in the condor.
NAKED = OptionPosition("NIFTY", EXPIRY, CE, Decimal(26000), -LOT)


def symbol_of(leg: OptionPosition) -> str:
    return f"{leg.underlying}26NOV{leg.strike}{leg.option_type.value}"


class SpyKC:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def place_order(self, **params: object) -> str:
        self.calls.append(dict(params))
        return f"OID-{len(self.calls)}"


def gateway(
    tmp_path: Path, *, wired: bool = True, dry_run: bool = False
) -> tuple[OrderGateway, SpyKC]:
    kc = SpyKC()
    gw = OrderGateway(
        kc,
        RiskManager(),
        gates=lambda: ProductGates(
            dry_run=dry_run, intraday_enabled=True, options_enabled=True, fno_carry_enabled=True
        ),
        journal_path=str(tmp_path / "journal.jsonl"),
        coverage=uncovered if wired else None,
    )
    return gw, kc


def send(  # noqa: PLR0913
    gw: OrderGateway,
    leg: OptionPosition,
    *,
    broker: tuple[OptionPosition, ...] = (),
    filled: tuple[OptionPosition, ...] = (),
    product: str = "NRML",
    plan_id: str = "FOPLAN-1",
    symbol: str | None = None,
    qty: int | None = None,
    side: str | None = None,
) -> dict[str, object]:
    sym = symbol or symbol_of(leg)
    return asyncio.run(
        gw.place(
            symbol=sym,
            qty=abs(leg.quantity) if qty is None else qty,
            side=side or ("BUY" if leg.quantity > 0 else "SELL"),
            product=product,
            order_type="LIMIT",
            price=10.0,
            exchange="NFO",
            gross_exposure=0.0,
            client_id=f"{plan_id}:{sym}",
            tenant=TENANT,
            plan_tenant=TENANT,
            fo_plan=FoPlanRef(
                plan_id=plan_id,
                sleeve="F1N",
                step=leg,
                broker_positions=broker,
                plan_filled=filled,
            ),
        )
    )


# --- the entry and exit sequences ----------------------------------------------------------------


def test_the_entry_sequence_is_admitted_step_by_step(tmp_path: Path) -> None:
    gw, kc = gateway(tmp_path)
    for k, leg in enumerate(ENTRY):
        out = send(gw, leg, filled=ENTRY[:k])
        assert out["status"] == "PLACED", (leg, out)
    assert [c["product"] for c in kc.calls] == ["NRML"] * 4
    assert [c["tradingsymbol"] for c in kc.calls] == [symbol_of(leg) for leg in ENTRY]


@pytest.mark.parametrize("prefix", range(len(ENTRY) + 1))
def test_a_naked_short_is_refused_at_every_prefix(tmp_path: Path, prefix: int) -> None:
    gw, kc = gateway(tmp_path)
    filled = ENTRY[:prefix]
    out = send(gw, NAKED, filled=filled)
    assert out["status"] == "BLOCKED", out
    assert "uncovered" in str(out["error"])
    # Each short whose own long has not filled yet is naked too, at this prefix.
    for short, long_ in COVER.items():
        if long_ in filled or short in filled:
            continue
        out = send(gw, short, filled=filled, plan_id=f"FOPLAN-{prefix}")
        assert out["status"] == "BLOCKED", (short, out)
    assert kc.calls == []


def test_shorts_first_is_refused_at_the_first_step(tmp_path: Path) -> None:
    gw, kc = gateway(tmp_path)
    for leg in (SHORT_PUT, SHORT_CALL):
        assert send(gw, leg)["status"] == "BLOCKED"
    assert kc.calls == []


@pytest.mark.parametrize("partial", (1, 25, 50, 74))
def test_a_partly_filled_long_covers_only_what_filled(tmp_path: Path, partial: int) -> None:
    gw, kc = gateway(tmp_path)
    filled = (replace(LONG_PUT, quantity=partial),)
    assert send(gw, SHORT_PUT, filled=filled)["status"] == "BLOCKED"
    assert kc.calls == []
    out = send(gw, replace(SHORT_PUT, quantity=-partial), filled=filled, plan_id="FOPLAN-2")
    assert out["status"] == "PLACED", out


def test_the_exit_is_shorts_first_and_a_long_sold_early_is_refused(tmp_path: Path) -> None:
    """The exit plan's broker book is the whole condor (the entry plan's legs, not this plan's)."""
    gw, kc = gateway(tmp_path)
    book = ENTRY
    early = send(gw, replace(LONG_CALL, quantity=-LOT), broker=book, plan_id="EXIT-0")
    assert early["status"] == "BLOCKED"
    assert kc.calls == []
    exits = (
        replace(SHORT_CALL, quantity=LOT),
        replace(SHORT_PUT, quantity=LOT),
        replace(LONG_CALL, quantity=-LOT),
        replace(LONG_PUT, quantity=-LOT),
    )
    for k, leg in enumerate(exits):
        out = send(gw, leg, broker=book, filled=exits[:k], plan_id="EXIT-1")
        assert out["status"] == "PLACED", (leg, out)


@pytest.mark.parametrize(
    "cover",
    (
        replace(LONG_CALL, underlying="BANKNIFTY"),
        replace(LONG_CALL, expiry=dt.date(2026, 12, 29)),
        replace(LONG_CALL, option_type=PE, strike=Decimal(23500)),
        replace(LONG_CALL, strike=Decimal(24800)),  # nearer the money, not further
    ),
    ids=("other-underlying", "other-expiry", "other-type", "nearer-strike"),
)
def test_cover_must_be_same_underlying_expiry_and_type_and_further_out(
    tmp_path: Path, cover: OptionPosition
) -> None:
    gw, kc = gateway(tmp_path)
    assert send(gw, SHORT_CALL, broker=(cover,))["status"] == "BLOCKED"
    assert kc.calls == []


# --- FO1.2: the broker book is net of this plan's fills, so nothing counts twice ----------------


def test_a_filled_long_is_counted_once_not_twice(tmp_path: Path) -> None:
    """The broker, read after the long call filled, already holds it. Adding the plan's fills to
    that raw book would count the long twice and admit a short twice its size."""
    filled = (LONG_CALL,)
    raw_after_fill = (LONG_CALL,)
    double = replace(SHORT_CALL, quantity=-2 * LOT)
    # The test bites: the naive composition would have called this covered.
    assert uncovered((*raw_after_fill, *filled, double)) == ()
    gw, kc = gateway(tmp_path)
    out = send(gw, double, broker=net_of_plan(raw_after_fill, filled), filled=filled)
    assert out["status"] == "BLOCKED", out
    assert kc.calls == []
    out = send(gw, SHORT_CALL, broker=net_of_plan(raw_after_fill, filled), filled=filled)
    assert out["status"] == "PLACED", out


def test_a_broker_book_that_lags_a_fill_only_makes_the_guard_stricter(tmp_path: Path) -> None:
    """Netting a fill the broker has not reflected yet leaves a short residue: refuse, never
    admit."""
    filled = (LONG_CALL,)
    net = net_of_plan((), filled)
    assert net == (replace(LONG_CALL, quantity=-LOT),)
    gw, kc = gateway(tmp_path)
    assert send(gw, SHORT_CALL, broker=net, filled=filled)["status"] == "BLOCKED"
    assert kc.calls == []


# --- the reference must describe the order it rides on -----------------------------------------


@pytest.mark.parametrize(
    ("symbol", "qty", "side"),
    (
        (None, 150, None),
        (None, None, "BUY"),
        ("NIFTY26NOV25100CE", None, None),
        ("NIFTY26NOV25000PE", None, None),
        ("BANKNIFTY26NOV25000CE", None, None),
        ("NIFTYNXT5026NOV25000CE", None, None),
        ("NIFTY26N24125000CE", None, None),  # a strike of 125000, not 25000
    ),
    ids=("qty", "side", "strike", "type", "underlying", "prefix", "strike-suffix"),
)
def test_a_step_that_is_not_the_order_is_refused(
    tmp_path: Path, symbol: str | None, qty: int | None, side: str | None
) -> None:
    gw, kc = gateway(tmp_path)
    out = send(gw, SHORT_CALL, broker=(LONG_CALL,), symbol=symbol, qty=qty, side=side)
    assert out["status"] == "BLOCKED", out
    assert kc.calls == []


def test_an_option_order_whose_reference_names_no_step_is_refused() -> None:
    with pytest.raises(UncoveredOptionError):
        assert_overnight_option_is_covered(
            symbol_of(SHORT_CALL),
            LOT,
            "SELL",
            fo_plan=FoPlanRef(plan_id="P", sleeve="F1N", broker_positions=(LONG_CALL,)),
            coverage=uncovered,
        )


def test_no_predicate_wired_refuses_every_fo_option_order(tmp_path: Path) -> None:
    gw, kc = gateway(tmp_path, wired=False)
    out = send(gw, LONG_PUT)
    assert out["status"] == "BLOCKED"
    assert "no covered-overnight rule" in str(out["error"])
    assert kc.calls == []


def test_an_f1_reference_on_a_future_or_an_unknown_sleeve_is_refused(tmp_path: Path) -> None:
    gw, kc = gateway(tmp_path)
    for sleeve, symbol in (("F1N", "NIFTY26NOVFUT"), ("F2", symbol_of(LONG_PUT)), ("O1M", "X")):
        out = asyncio.run(
            gw.place(
                symbol=symbol,
                qty=LOT,
                side="BUY",
                product="NRML",
                price=10.0,
                exchange="NFO",
                gross_exposure=0.0,
                tenant=TENANT,
                plan_tenant=TENANT,
                fo_plan=FoPlanRef(plan_id="P", sleeve=sleeve, step=LONG_PUT),
            )
        )
        assert out["status"] == "BLOCKED", (sleeve, out)
    assert kc.calls == []


def test_an_fo_option_under_mis_still_meets_the_cover_rule(tmp_path: Path) -> None:
    """Stricter, not looser: a fo_plan reference never skips the covered check."""
    gw, kc = gateway(tmp_path)
    assert send(gw, NAKED, product="MIS")["status"] == "BLOCKED"
    assert kc.calls == []


def test_a_dry_run_fo_leg_is_simulated_and_journalled_with_its_plan(tmp_path: Path) -> None:
    gw, kc = gateway(tmp_path, dry_run=True)
    out = send(gw, LONG_PUT)
    assert out["status"] == "DRY_RUN"
    assert kc.calls == []
    assert '"fo_plan": "FOPLAN-1"' in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")


def test_an_uncovered_refusal_is_journalled(tmp_path: Path) -> None:
    gw, _ = gateway(tmp_path)
    send(gw, NAKED)
    text = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert '"event": "fo_uncovered_block"' in text
    assert f'"client_id": "FOPLAN-1:{symbol_of(NAKED)}"' in text


# --- orders without a reference: the old guard, unchanged -----------------------------------


@pytest.mark.parametrize("product", ("NRML", "CNC"))
def test_an_o_sleeve_option_under_a_carry_product_is_still_refused_by_the_old_guard(
    tmp_path: Path, product: str
) -> None:
    """Every switch on, the carry flag too, a predicate wired: without a fo_plan reference an
    option under a carry product meets ``assert_not_overnight_option`` exactly as before."""
    gw, kc = gateway(tmp_path)
    out = asyncio.run(
        gw.place(
            symbol="NIFTY2692223700CE",
            qty=LOT,
            side="BUY",
            product=product,
            price=10.0,
            exchange="NFO",
            gross_exposure=0.0,
            client_id="O1M-PLAN:NIFTY2692223700CE",
            tenant=TENANT,
            plan_tenant=TENANT,
        )
    )
    assert out["status"] == "BLOCKED"
    assert "past the close" in str(out["error"])
    assert kc.calls == []
    assert '"event": "overnight_option_block"' in (tmp_path / "journal.jsonl").read_text(
        encoding="utf-8"
    )
