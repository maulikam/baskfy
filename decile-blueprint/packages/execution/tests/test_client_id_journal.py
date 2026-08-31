"""``client_id = plan_id:symbol`` — minted here, and written on every journal line.

Two things this file pins, both of which were missing before leaf 2.2:

1. **The mint.** Non-negotiable #6 says "``client_id = plan_id:symbol`` so a re-posted plan
   cannot double-send". The gateway has always honoured the id; the only code that *built* one
   was an f-string on the desk (``app/main.py:581``), in the tree being retired. A format that
   lives in each caller's head is a convention, and a caller who formats it differently gets a
   different key for the same order — which is a double-send, not a lint error.

2. **The journal.** Leaf 1.2.3 ABANDONed its G4 here: ``gateway.py:136,152,238`` recorded
   symbol, side, qty and price and never the client id, in live as well as DRY_RUN, so a
   production journal could not be reconciled to the plan that produced it. Every line the
   gateway writes now carries the id it keyed the order on.

Nothing here can place a live order: ``DRY_RUN`` throughout, the broker is a plain Python
double, and the repo-wide ``block_network`` fixture refuses every non-loopback socket.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import pathlib

import pytest
from baskfy_execution import (
    CLIENT_ID_SEPARATOR,
    OrderGateway,
    ProductGates,
    RiskConfig,
    RiskManager,
    TenantIds,
    mint_client_id,
    parse_client_id,
)
from baskfy_execution import gateway as gateway_module
from baskfy_execution.gtt import DRY_RUN_GTT, DRY_RUN_GTT_DELETE

SRC = pathlib.Path(gateway_module.__file__).resolve()
TENANT = TenantIds(user_id=1, broker_account_id=10)
OTHER = TenantIds(user_id=2, broker_account_id=20)


class SpyKC:
    """Records rather than refuses, so a guard that did not run shows up as a broker call."""

    GTT_TYPE_SINGLE = "single"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def place_order(self, **_: object) -> str:
        self.calls.append("place_order")
        return "OID-1"


def _gateway(
    tmp_path: pathlib.Path,
    *,
    risk: RiskManager | None = None,
    dry_run: bool = True,
) -> tuple[OrderGateway, pathlib.Path]:
    journal = tmp_path / "journal.jsonl"
    gw = OrderGateway(
        SpyKC(),
        risk or RiskManager(),
        gates=lambda: ProductGates(dry_run=dry_run),
        journal_path=str(journal),
    )
    return gw, journal


def _lines(journal: pathlib.Path) -> list[dict[str, object]]:
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line]


# =====================================================================================
# The mint
# =====================================================================================
class TestTheMint:
    def test_it_is_the_desk_s_format_character_for_character(self) -> None:
        """``app/main.py:581``: ``client_id=f"{plan_id}:{o['symbol']}"``."""
        assert mint_client_id(plan_id="PLAN1", symbol="RELIANCE") == "PLAN1:RELIANCE"
        assert CLIENT_ID_SEPARATOR == ":"

    def test_the_same_plan_and_symbol_always_mint_the_same_id(self) -> None:
        """Determinism IS the idempotency. Nothing random, nothing time-dependent."""
        first = mint_client_id(plan_id="plan-abc", symbol="INFY")
        second = mint_client_id(plan_id="plan-abc", symbol="INFY")
        assert first == second

    def test_different_symbols_in_one_plan_are_different_keys(self) -> None:
        assert mint_client_id(plan_id="P", symbol="INFY") != mint_client_id(
            plan_id="P", symbol="TCS"
        )

    def test_different_plans_for_one_symbol_are_different_keys(self) -> None:
        """Or a second, legitimate rebalance of the same name would be refused as a duplicate."""
        assert mint_client_id(plan_id="P1", symbol="INFY") != mint_client_id(
            plan_id="P2", symbol="INFY"
        )

    def test_a_symbol_s_case_does_not_make_a_second_key(self) -> None:
        """One NSE instrument, one key. Safe to normalise because the id never reaches the
        broker — it is this process's dictionary key and a journal field."""
        assert mint_client_id(plan_id="P", symbol="infy") == mint_client_id(
            plan_id="P", symbol="INFY"
        )

    @pytest.mark.parametrize(
        ("plan_id", "symbol"),
        [
            pytest.param("", "INFY", id="empty plan_id"),
            pytest.param("P", "", id="empty symbol"),
            pytest.param("P:1", "INFY", id="separator in plan_id"),
            pytest.param("P", "IN:FY", id="separator in symbol"),
            pytest.param("P 1", "INFY", id="whitespace in plan_id"),
            pytest.param("P", "INFY ", id="whitespace in symbol"),
        ],
    )
    def test_it_refuses_anything_that_could_collide_or_split_wrong(
        self, plan_id: str, symbol: str
    ) -> None:
        with pytest.raises(ValueError):
            mint_client_id(plan_id=plan_id, symbol=symbol)

    def test_the_ambiguous_pair_the_separator_rule_exists_for(self) -> None:
        """``("a:b", "c")`` and ``("a", "b:c")`` would mint one string for two orders."""
        with pytest.raises(ValueError):
            mint_client_id(plan_id="a:b", symbol="c")
        with pytest.raises(ValueError):
            mint_client_id(plan_id="a", symbol="b:c")

    def test_a_minted_id_parses_back_to_its_two_halves(self) -> None:
        """The reconciliation direction: given a journal line, which plan ordered it."""
        cid = mint_client_id(plan_id="plan-7", symbol="SONACOMS")
        assert parse_client_id(cid) == ("plan-7", "SONACOMS")

    @pytest.mark.parametrize("bad", ["nosep", "a:b:c", ":RELIANCE", "PLAN1:"])
    def test_parsing_refuses_what_was_not_minted_here(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_client_id(bad)


# =====================================================================================
# The mint drives the gateway's idempotency
# =====================================================================================
class TestAReplayedPlanCannotDoubleSend:
    def test_the_second_presentation_is_a_duplicate_not_a_second_order(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw, journal = _gateway(tmp_path)
        cid = mint_client_id(plan_id="plan-1", symbol="RELIANCE")

        def once() -> dict[str, object]:
            return asyncio.run(
                gw.place(
                    symbol="RELIANCE",
                    qty=5,
                    side="BUY",
                    product="CNC",
                    order_type="LIMIT",
                    price=100.0,
                    exchange="NSE",
                    client_id=cid,
                    tenant=TENANT,
                    plan_tenant=TENANT,
                )
            )

        assert once()["status"] == "DRY_RUN"
        assert once()["status"] == "DUPLICATE"
        placements = [line for line in _lines(journal) if line["event"] == "dry_run"]
        assert len(placements) == 1, "the replay reached the broker path a second time"

    def test_a_multi_leg_plan_sends_each_leg_once_and_replays_none_of_them(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The key is per plan AND per symbol: two legs are two orders, and the whole plan
        re-presented is two duplicates. That pair is what "idempotent, not double-sent" means
        for a batch rather than for a single order."""
        gw, journal = _gateway(tmp_path)
        legs = ("RELIANCE", "INFY")

        def send_all() -> list[str]:
            return [
                str(
                    asyncio.run(
                        gw.place(
                            symbol=symbol,
                            qty=5,
                            side="BUY",
                            product="CNC",
                            order_type="LIMIT",
                            price=100.0,
                            exchange="NSE",
                            client_id=mint_client_id(plan_id="plan-1", symbol=symbol),
                            tenant=TENANT,
                            plan_tenant=TENANT,
                        )
                    )["status"]
                )
                for symbol in legs
            ]

        assert send_all() == ["DRY_RUN", "DRY_RUN"]
        assert send_all() == ["DUPLICATE", "DUPLICATE"]
        assert len([line for line in _lines(journal) if line["event"] == "dry_run"]) == 2


# =====================================================================================
# The journal
# =====================================================================================
class TestEveryJournalLineCarriesTheClientId:
    def test_structurally_every_journal_call_in_the_gateway_passes_one(self) -> None:
        """Over the source, so a journal line added tomorrow and never exercised by a test
        cannot ship without an id. This is the half that keeps the property true."""
        tree = ast.parse(SRC.read_text(encoding="utf-8"), filename=str(SRC))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_journal"
                and not any(kw.arg == "client_id" for kw in node.keywords)
            ):
                offenders.append(f"gateway.py:{node.lineno}")
        assert offenders == [], f"journal lines with no client_id: {offenders}"

    def test_the_journal_helper_has_no_default_so_a_line_cannot_forget(self) -> None:
        signature = inspect.signature(OrderGateway._journal)
        parameter = signature.parameters["client_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    def test_a_dry_run_order_is_journalled_under_its_plan(self, tmp_path: pathlib.Path) -> None:
        gw, journal = _gateway(tmp_path)
        cid = mint_client_id(plan_id="plan-9", symbol="RELIANCE")
        asyncio.run(
            gw.place(
                symbol="RELIANCE",
                qty=1,
                side="BUY",
                product="CNC",
                order_type="LIMIT",
                price=100.0,
                exchange="NSE",
                client_id=cid,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        (line,) = _lines(journal)
        assert line["client_id"] == cid
        assert parse_client_id(str(line["client_id"])) == ("plan-9", "RELIANCE")

    def test_a_live_order_is_journalled_under_its_plan_too(self, tmp_path: pathlib.Path) -> None:
        """1.2.3's finding was "in live as well as DRY_RUN". The live path is the one that
        matters for reconciliation, so it is asserted separately rather than assumed."""
        gw, journal = _gateway(tmp_path, dry_run=False)
        cid = mint_client_id(plan_id="plan-9", symbol="RELIANCE")
        out = asyncio.run(
            gw.place(
                symbol="RELIANCE",
                qty=1,
                side="BUY",
                product="CNC",
                order_type="LIMIT",
                price=100.0,
                exchange="NSE",
                client_id=cid,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        assert out["status"] == "PLACED"
        (line,) = _lines(journal)
        assert line["event"] == "placed"
        assert line["client_id"] == cid

    def test_a_risk_refusal_is_journalled_under_its_plan(self, tmp_path: pathlib.Path) -> None:
        """The line an operator most needs to tie to a plan: nothing was sent, and they have to
        know which plan asked. The id used to be generated three refusals later than this."""
        risk = RiskManager(RiskConfig(max_position_value=1.0))
        gw, journal = _gateway(tmp_path, risk=risk)
        cid = mint_client_id(plan_id="plan-9", symbol="RELIANCE")
        out = asyncio.run(
            gw.place(
                symbol="RELIANCE",
                qty=100,
                side="BUY",
                product="CNC",
                order_type="LIMIT",
                price=100.0,
                exchange="NSE",
                client_id=cid,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        assert out["status"] == "RISK_BLOCKED"
        (line,) = _lines(journal)
        assert line["event"] == "risk_block"
        assert line["client_id"] == cid

    def test_an_armed_stop_is_journalled_under_its_plan(self, tmp_path: pathlib.Path) -> None:
        gw, journal = _gateway(tmp_path)
        cid = mint_client_id(plan_id="plan-9", symbol="RELIANCE")
        out = asyncio.run(
            gw.place_gtt_stop(
                symbol="RELIANCE",
                qty=10,
                trigger=90.0,
                last_price=100.0,
                exchange="NSE",
                client_id=cid,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        assert out["status"] == DRY_RUN_GTT
        assert [line["client_id"] for line in _lines(journal)] == [cid]

    def test_a_refused_stop_is_journalled_under_its_plan(self, tmp_path: pathlib.Path) -> None:
        """A trigger at or above the last price is not a stop; the refusal names the plan."""
        gw, journal = _gateway(tmp_path)
        cid = mint_client_id(plan_id="plan-9", symbol="RELIANCE")
        out = asyncio.run(
            gw.place_gtt_stop(
                symbol="RELIANCE",
                qty=10,
                trigger=110.0,
                last_price=100.0,
                exchange="NSE",
                client_id=cid,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        assert out["status"] == "BLOCKED"
        assert [line["client_id"] for line in _lines(journal)] == [cid]

    def test_a_cancelled_stop_can_name_the_plan_that_ordered_it(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A cancel is addressed by trigger id, so the plan is optional — but when the caller
        has one, the cancellation joins the same reconciliation thread as the order."""
        gw, journal = _gateway(tmp_path)
        cid = mint_client_id(plan_id="plan-9", symbol="RELIANCE")
        out = asyncio.run(
            gw.delete_gtt(
                gtt_id=4242,
                symbol="RELIANCE",
                exchange="NSE",
                client_id=cid,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        assert out["status"] == DRY_RUN_GTT_DELETE
        assert [line["client_id"] for line in _lines(journal)] == [cid]

    def test_a_cancel_with_no_plan_records_none_rather_than_omitting_the_field(
        self, tmp_path: pathlib.Path
    ) -> None:
        """An explicit ``null`` says "this cancel had no plan"; a missing key says nothing at
        all, and a reconciliation script cannot tell the two apart."""
        gw, journal = _gateway(tmp_path)
        asyncio.run(
            gw.delete_gtt(
                gtt_id=4242,
                symbol="RELIANCE",
                exchange="NSE",
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        (line,) = _lines(journal)
        assert "client_id" in line
        assert line["client_id"] is None

    def test_an_order_with_no_client_id_still_journals_the_key_it_used(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The gateway invents a key when the caller supplies none. Journalling *that* key is
        what makes the journal and the idempotency map describe the same thing."""
        gw, journal = _gateway(tmp_path)
        out = asyncio.run(
            gw.place(
                symbol="RELIANCE",
                qty=1,
                side="BUY",
                product="CNC",
                order_type="LIMIT",
                price=100.0,
                exchange="NSE",
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        )
        (line,) = _lines(journal)
        journalled = line["client_id"]
        assert isinstance(journalled, str) and journalled
        assert out["order_id"] == f"DRY-{journalled}"
