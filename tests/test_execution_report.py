"""The execution report on the desk page.

This panel is read once, under time pressure, immediately after real money has moved. The
failure it exists to prevent is a partial execution — some orders live, some refused, a
stop or two unarmed — reading as a success. These tests pin the parts of that which can
drift silently: the status vocabulary it understands, and the two numbers it must not
present as fact.
"""
from __future__ import annotations

import re

import app.main as M

TEMPLATE = "app/templates/index.html"


def template() -> str:
    with open(TEMPLATE) as f:
        return f.read()


def declared_statuses() -> set[str]:
    """The order statuses the report knows how to explain."""
    block = re.search(r"const ORDER_STATUS=\{(.*?)\n\};", template(), re.S)
    assert block, "the report's status map is gone or was renamed"
    return set(re.findall(r"^\s*([A-Z_]+):\s*\{", block.group(1), re.M))


def returned_by(path: str) -> set[str]:
    with open(path) as f:
        return set(re.findall(r'"status":\s*"([A-Z_]+)"', f.read()))


# =====================================================================================
# drift: a status the report has never heard of
# =====================================================================================
def test_every_status_the_gateway_returns_has_an_explanation():
    """The gateway is the sole order path, so its vocabulary IS the report's vocabulary.
    An unnamed status still renders safely as unresolved, but it renders without the one
    thing the operator needs — whether the order reached the exchange, and what to do."""
    gateway = returned_by("app/core/gateway.py")
    assert gateway, "gateway no longer returns a recognisable status"
    missing = gateway - declared_statuses()
    assert not missing, f"{missing} would render with no remedy for the operator"


def test_an_unknown_status_is_never_counted_as_a_success():
    """The dangerous direction. A status added upstream must degrade to 'check the order
    book', never to a silent line in the submitted count."""
    src = template()
    fallback = re.search(r"const read=o=>ORDER_STATUS\[o\.status\]\s*\|\|\{kind:'(\w+)'",
                         src)
    assert fallback, "the unknown-status fallback is gone"
    assert fallback.group(1) == "bad"


def test_only_a_placed_order_counts_as_submitted():
    """PLACED is the only status that means an order reached the exchange. DRY_RUN,
    DUPLICATE, BLOCKED and RISK_BLOCKED all mean nothing was sent."""
    block = re.search(r"const ORDER_STATUS=\{(.*?)\n\};", template(), re.S).group(1)
    ok = set(re.findall(r"^\s*([A-Z_]+):\s*\{kind:'ok'", block, re.M))
    assert ok == {"PLACED"}, f"{ok - {'PLACED'}} would be miscounted as sent"


def test_the_gtt_success_set_matches_the_route_that_arms_stops():
    """Same hand-written-set failure that once reported 16 live triggers as 0 armed —
    here it would report an armed stop as missing and invite a duplicate."""
    declared = re.search(r"const STOP_OK=new Set\(\[(.*?)\]\)", template())
    assert declared, "the report's GTT success set is gone"
    assert set(re.findall(r"'([A-Z_]+)'", declared.group(1))) == set(M.STOP_OK)


def test_every_gtt_status_kite_client_returns_is_classified():
    gtt = {s for s in returned_by("app/kite_client.py") if "GTT" in s}
    assert gtt, "kite_client no longer returns a recognisable GTT status"
    src = template()
    for s in gtt:
        assert s in src or s not in M.STOP_OK, f"{s} is neither a success nor explained"


# =====================================================================================
# the two numbers that would be false
# =====================================================================================
def test_the_report_never_shows_filled_value():
    """record_execution() marks a PLACED order FILLED for its full planned quantity, which
    is not knowable at the instant of submission: these are LIMIT orders and some will not
    trade. Showing that field would state a fill that may never happen."""
    # Comments stripped: the reason the field is avoided is written in one, and matching
    # prose instead of code is how a test passes while the defect is present.
    code = re.sub(r"^\s*//.*$", "", template(), flags=re.M)
    assert "filled_value" not in code


def test_the_report_says_submitted_is_not_filled():
    """Because the operator will ask exactly that question, and the honest answer at this
    moment is 'not yet known'."""
    assert "Submitted is not filled" in template()


def test_an_untouchable_instrument_is_not_reported_as_unprotected():
    """analytics/protection.py:56 — an untouchable instrument is deliberately never traded,
    and place_gtt_stop's guard would refuse it. Listing it under 'arm a stop' would demand
    an action that cannot be performed."""
    src = template()
    assert "const untouchable=new Set(" in src
    assert "filter(sym=>!untouchable.has(sym))" in src


# =====================================================================================
# the raw response stays available, but stops being the interface
# =====================================================================================
def test_the_raw_json_is_still_reachable():
    """The rendered view is an interpretation. The ground truth must remain one click
    away, or a disagreement between them cannot be settled."""
    assert re.search(r'<details id="resultRaw">.*?resultBody.*?</details>',
                     template(), re.S)


def test_the_json_dump_is_no_longer_the_primary_view():
    src = template()
    body = re.search(r'<div class="card" id="result".*?</div>\s*\n\s*<script>', src, re.S)
    assert body, "the result card is gone or was restructured"
    rendered = body.group(0).index('id="repVerdict"')
    raw = body.group(0).index('id="resultRaw"')
    assert rendered < raw, "the raw dump comes before the verdict"


def test_a_rejected_plan_is_not_dressed_as_an_execution():
    """A 4xx from the gates — stale plan, unknown plan_id, no confirmation — means no
    order was placed at all. Rendering it through the same panel would imply otherwise."""
    src = template()
    assert "not executed" in src
    assert "The plan was rejected before any order" in src
