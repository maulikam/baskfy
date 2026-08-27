"""The connect catalog may not advertise a capability that has no adapter behind it.

House rule 2 in its purest form: this asserts the **spec**, not what the code happens to do.
The spec is one sentence —

    a broker may claim ``holdings_sync="ready"`` only if a holdings adapter is actually wired.

The wiring is not a matter of opinion: :data:`baskfy_api.broker_holdings._HOLDINGS_WIRED` is the
set of brokers :func:`baskfy_api.broker_holdings.holdings_for_broker` will even try to sync. Every
id outside that set returns ``[]`` from the first line of the function. A catalog row that says
"ready" for one of those is telling the investor the sync works when the code cannot produce a
single holding.

When this test failed for the first time (Tree-5 leaf C2, 25 Aug 2026) it named seven brokers:
kotak, icici, upstox, angelone, fyers, fivepaisa, dhan — eight rows claimed ``"ready"`` and only
zerodha had an adapter.

Two directions are asserted, because a catalog can lie either way:

* over-claiming — "ready" with no adapter, the defect this leaf was opened for;
* under-claiming — an adapter exists and the tile still says "Planned", which would send a user
  to a CSV import they do not need.

The blurb is held to the same standard as the label. It is rendered on the same tile, one line
above the capability list (``apps/web/src/components/brokers/broker-grid.tsx``), and prose that
promises "sync holdings" undoes a label that says "Planned".

Importing from ``baskfy_api`` here does not breach law 1. The *catalog module* stays pure — this
is a test reaching across the tree to bind two files that must agree, the same arrangement as
``test_operand_parity.py`` and ``test_holding_profile_parity.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from baskfy_api.broker_holdings import _HOLDINGS_WIRED
from baskfy_core.broker_connections import BROKERS, BrokerDef, Capability, broker_catalog

MONOREPO_ROOT = Path(__file__).resolve().parents[3]
BROKER_GRID_DIR = MONOREPO_ROOT / "apps" / "web" / "src" / "components" / "brokers"

#: The vocabulary the ``Capability`` literal already carries. A new value here is a decision,
#: not a typo, and it must be added to the type before this set changes.
KNOWN_CAPABILITIES: frozenset[str] = frozenset(get_args(Capability))

#: Words that make a sentence a statement of intent rather than a statement of fact. An unwired
#: broker's blurb must contain at least one of them, so the prose cannot promise what the label
#: withholds. The same genre of lint as ``apps/web/src/lib/__tests__/copy-lint.test.ts``.
CONDITIONAL_MARKERS: tuple[str, ...] = (
    "planned",
    "partner",
    "once ",
    "when ",
    "not wired",
    "not yet",
)


def unwired() -> list[BrokerDef]:
    return [b for b in BROKERS if b.id not in _HOLDINGS_WIRED]


@pytest.fixture(scope="module")
def label_source() -> str:
    """The web file that turns a ``Capability`` value into a word on the tile.

    Located by content rather than by a hard-coded filename: the check is that *something*
    under the brokers component folder still maps these values, not that one file is named
    what it is named today.
    """
    candidates = sorted(BROKER_GRID_DIR.glob("*.tsx")) if BROKER_GRID_DIR.is_dir() else []
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        if "capabilityLabel" in text:
            return text
    pytest.fail(
        "no file under apps/web/src/components/brokers defines capabilityLabel; if the web "
        "renderer moved, point this test at its new home rather than deleting it"
    )


class TestTheWiringIsTheAuthority:
    """``_HOLDINGS_WIRED`` decides; the catalog reports. Never the other way round."""

    def test_the_wired_set_is_not_empty(self) -> None:
        """A test against an empty set would pass by vacuum and prove nothing."""
        assert _HOLDINGS_WIRED, "no holdings adapter is wired at all — this test proves nothing"

    def test_every_wired_broker_is_on_the_catalog(self) -> None:
        """An adapter for a broker nobody can see is drift in the other direction."""
        catalog_ids = {b.id for b in BROKERS}
        missing = sorted(_HOLDINGS_WIRED - catalog_ids)
        assert missing == [], (
            f"holdings adapter wired for brokers absent from the catalog: {missing}"
        )


class TestTheCatalogDoesNotOverclaim:
    def test_no_unwired_broker_claims_holdings_sync_is_ready(self) -> None:
        """THE SPEC. A tile may not say the sync works when the sync cannot run.

        ``holdings_for_broker`` returns ``[]`` on its first line for any id outside
        ``_HOLDINGS_WIRED``, so "ready" on such a row is not optimistic — it is false.
        """
        overclaiming = sorted(b.id for b in unwired() if b.capabilities.holdings_sync == "ready")
        assert overclaiming == [], (
            "these brokers advertise a working holdings sync with no adapter behind it: "
            f"{overclaiming}. Either wire an adapter (credentials — see NEEDS-MAULIK.md) or "
            "correct the label."
        )

    #: Brokers with a wired adapter that the catalog still does not advertise, and why.
    #:
    #: This set exists because "the codebase has an adapter" and "a reader can use it" were the
    #: same fact until M55 and are not any more. Zerodha's Kite adapter is real and has been
    #: fetching holdings since Tree-3 — but it is a **Kite Connect** adapter, and Baskfy
    #: integrates with **Kite Publisher**, which links no account and hands back no session to
    #: fetch anything with. Kite Connect is ₹2,000/month and licensed for the app owner's own
    #: account, which is the wrong shape for a product other people sign into.
    #:
    #: An entry here is a deliberate *under*-claim and must stay rare. The over-claim direction —
    #: advertising a sync with no adapter behind it — is still forbidden outright below.
    UNREACHABLE_ADAPTERS = {
        "zerodha": "integrated through Kite Publisher, which cannot read holdings",
    }

    def test_every_wired_broker_either_says_ready_or_says_why_not(self) -> None:
        """The honest case must not become collateral damage of the fix.

        Relabelling everything "planned" would satisfy the anti-over-claim assertion above while
        lying in the other direction. So a wired adapter must either be advertised, or be named
        here with a reason — silence is what this forbids.
        """
        for broker_id in sorted(_HOLDINGS_WIRED):
            broker = next(b for b in BROKERS if b.id == broker_id)
            if broker_id in self.UNREACHABLE_ADAPTERS:
                assert broker.capabilities.holdings_sync == "not_available", (
                    f"{broker_id} is recorded as unreachable "
                    f"({self.UNREACHABLE_ADAPTERS[broker_id]}) but the catalog says "
                    f"{broker.capabilities.holdings_sync!r}"
                )
                continue
            assert broker.capabilities.holdings_sync == "ready", (
                f"{broker_id} has a wired holdings adapter but the catalog says "
                f"{broker.capabilities.holdings_sync!r}"
            )

    def test_nothing_claims_a_sync_it_has_no_adapter_for(self) -> None:
        """The direction that matters, stated on its own.

        Was an equality against the wired set, which no longer holds: Zerodha is wired and
        deliberately silent. A subset is the real guarantee — every broker advertising a holdings
        sync must have something behind it.
        """
        claims_ready = {b.id for b in BROKERS if b.capabilities.holdings_sync == "ready"}
        assert claims_ready <= set(_HOLDINGS_WIRED), (
            f"advertised with no adapter: {sorted(claims_ready - set(_HOLDINGS_WIRED))}"
        )

    def test_the_unreachable_list_does_not_outlive_its_reason(self) -> None:
        """A stale exemption is a lie nobody is watching. Every entry must still be wired."""
        assert set(self.UNREACHABLE_ADAPTERS) <= set(_HOLDINGS_WIRED)

    def test_every_capability_value_is_in_the_declared_vocabulary(self) -> None:
        """A label the UI has never seen renders as whatever the fallback happens to be."""
        for broker in BROKERS:
            for field, value in (
                ("oauth", broker.capabilities.oauth),
                ("holdings_sync", broker.capabilities.holdings_sync),
                ("trading", broker.capabilities.trading),
            ):
                assert value in KNOWN_CAPABILITIES, (
                    f"{broker.id}.{field} is {value!r}, which is not one of "
                    f"{sorted(KNOWN_CAPABILITIES)} — widen Capability deliberately or fix the row"
                )


class TestTheProseMatchesTheLabel:
    def test_an_unwired_brokers_blurb_does_not_promise_a_working_sync(self) -> None:
        """The blurb sits one line above the capability list on the same tile.

        "Breeze API — connect ICICI Direct, sync holdings, place confirmed orders" is the same
        promise the label used to make, in prose, and a corrected label does not retract it.
        """
        promising: list[str] = []
        for broker in unwired():
            blurb = broker.blurb.lower()
            if not any(marker in blurb for marker in CONDITIONAL_MARKERS):
                promising.append(f"{broker.id}: {broker.blurb}")
        assert promising == [], (
            "an unwired broker's blurb reads as a statement of fact; say what it will take "
            f"instead (one of {CONDITIONAL_MARKERS}): {promising}"
        )

    def test_the_wired_brokers_blurb_is_not_hedged_into_meaninglessness(self) -> None:
        """Zerodha works today. Its tile should say so plainly."""
        zerodha = next(b for b in BROKERS if b.id == "zerodha")
        assert "planned" not in zerodha.blurb.lower()


class TestTheWebRendersTheLabel:
    """``capabilityLabel`` in the connect grid must have a sentence for every value we ship.

    Its last line is an unconditional fallback, so an unhandled value does not blank the tile —
    it renders as *the wrong label*, which is worse. This binds the two.
    """

    def test_every_shipped_capability_value_gets_its_own_sentence(self, label_source: str) -> None:
        branched = set(re.findall(r'value === "([a-z_]+)"', label_source))
        fallback = re.findall(r'return "([A-Za-z ]+)";\s*\n\}', label_source)
        fallback_word = fallback[-1].strip().lower() if fallback else ""
        assert branched, 'capabilityLabel was found but no `value === "..."` branch parsed'
        assert fallback_word, "capabilityLabel has no unconditional fallback return to read"

        shipped = {
            value
            for broker in broker_catalog()
            for value in (
                broker.capabilities.oauth,
                broker.capabilities.holdings_sync,
                broker.capabilities.trading,
            )
        }
        unhandled = sorted(v for v in shipped if v not in branched and v != fallback_word)
        assert unhandled == [], (
            f"capabilityLabel has no branch for {unhandled} and its fallback reads "
            f"{fallback_word!r} — those tiles would render the wrong word. Hand this to leaf D1."
        )
