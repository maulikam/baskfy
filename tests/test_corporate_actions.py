"""Corporate actions: quantity that moves without a trade.

Found on the live book — ANANDRATHI showed 114 held against 70 in lots, and the 44-share
gap was exactly the position on the day the price halved. Reconciliation could see that.
Only this can price it.

Bonus and split are tested separately throughout, because Indian tax treats them very
differently and modelling both as "extra shares" would misprice one of them.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.analytics import corporate_actions as CA
from app.analytics import db
from app.analytics import tradebook as TB

D = dt.date


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def fill(symbol, when, side, qty, price, tid=None):
    return TB.Fill(symbol=symbol, when=dt.datetime.fromisoformat(when), side=side,
                   quantity=qty, price=price, trade_id=tid or f"{symbol}{when}{side}{qty}")


def bonus(symbol="X", ex="2026-06-01", new=1, old=1):
    return CA.parse(symbol, "bonus", ex, new, old)


def split(symbol="X", ex="2026-06-01", new=2, old=1):
    return CA.parse(symbol, "split", ex, new, old)


# =====================================================================================
# validation — a wrong ratio silently misprices a position
# =====================================================================================
def test_an_unknown_kind_is_refused():
    with pytest.raises(CA.CorporateActionError, match="kind must be"):
        CA.parse("X", "merger", "2026-06-01", 1, 1)


def test_a_bad_date_is_refused():
    with pytest.raises(CA.CorporateActionError, match="not a YYYY-MM-DD"):
        CA.parse("X", "bonus", "June 2026", 1, 1)


def test_a_future_ex_date_is_refused():
    ahead = (D.today() + dt.timedelta(days=5)).isoformat()
    with pytest.raises(CA.CorporateActionError, match="in the future"):
        CA.parse("X", "bonus", ahead, 1, 1)


@pytest.mark.parametrize("new,old", [(0, 1), (1, 0), (-1, 1)])
def test_a_non_positive_ratio_is_refused(new, old):
    with pytest.raises(CA.CorporateActionError, match="positive"):
        CA.parse("X", "bonus", "2026-06-01", new, old)


def test_a_split_that_does_not_increase_the_count_is_refused():
    """1:2 written the wrong way round would halve the book."""
    with pytest.raises(CA.CorporateActionError, match="must increase"):
        CA.parse("X", "split", "2026-06-01", 1, 2)


def test_the_symbol_is_normalised():
    assert CA.parse(" anandrathi ", "bonus", "2026-06-01", 1, 1).symbol == "ANANDRATHI"


# =====================================================================================
# bonus — nil cost, new holding period
# =====================================================================================
def test_a_bonus_adds_shares_at_nil_cost():
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)]
    rb = TB.build_lots(fills, [bonus()])
    assert sum(l.quantity for l in rb.open_lots) == 200
    nil = [l for l in rb.open_lots if l.price == 0.0]
    assert len(nil) == 1 and nil[0].quantity == 100


def test_a_bonus_lot_is_dated_at_the_ex_date_not_the_original_purchase():
    """Its holding period starts at allotment — this is what makes it short-term."""
    fills = [fill("X", "2024-01-05T10:00:00", "BUY", 100, 500.0)]
    rb = TB.build_lots(fills, [bonus(ex="2026-06-01")])
    nil = next(l for l in rb.open_lots if l.price == 0.0)
    assert nil.when.date() == D(2026, 6, 1)


def test_a_bonus_leaves_the_original_lot_untouched():
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)]
    rb = TB.build_lots(fills, [bonus()])
    orig = next(l for l in rb.open_lots if l.price == 500.0)
    assert orig.quantity == 100 and orig.when.date() == D(2026, 1, 5)


def test_a_two_for_one_bonus_triples_the_holding():
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 50, 100.0)]
    rb = TB.build_lots(fills, [bonus(new=2, old=1)])
    assert sum(l.quantity for l in rb.open_lots) == 150


# =====================================================================================
# split — cost divides, holding period inherited
# =====================================================================================
def test_a_split_multiplies_quantity_and_divides_cost():
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)]
    rb = TB.build_lots(fills, [split()])
    assert len(rb.open_lots) == 1
    lot = rb.open_lots[0]
    assert lot.quantity == 200 and lot.price == pytest.approx(250.0)


def test_a_split_preserves_the_original_acquisition_date():
    """Nothing was acquired, so a long-term holding stays long-term."""
    fills = [fill("X", "2024-01-05T10:00:00", "BUY", 100, 500.0)]
    rb = TB.build_lots(fills, [split()])
    assert rb.open_lots[0].when.date() == D(2024, 1, 5)


def test_a_split_conserves_the_total_cost_basis():
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)]
    rb = TB.build_lots(fills, [split(new=5, old=1)])
    lot = rb.open_lots[0]
    assert lot.quantity * lot.price == pytest.approx(100 * 500.0)


def test_a_split_creates_no_nil_cost_lot():
    """That is the bonus treatment, and applying it here would fabricate a tax liability."""
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)]
    rb = TB.build_lots(fills, [split()])
    assert not [l for l in rb.open_lots if l.price == 0.0]


# =====================================================================================
# ordering — an action is an event in time, not a post-hoc adjustment
# =====================================================================================
def test_a_sale_before_the_ex_date_does_not_draw_on_bonus_shares():
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0),
             fill("X", "2026-03-01T10:00:00", "SELL", 100, 600.0)]
    rb = TB.build_lots(fills, [bonus(ex="2026-06-01")])
    # position was flat at the ex-date, so no bonus is due
    assert rb.open_lots == []
    assert not rb.unmatched_sells


def test_a_sale_after_the_ex_date_consumes_the_adjusted_book():
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0),
             fill("X", "2026-07-01T10:00:00", "SELL", 150, 300.0)]
    rb = TB.build_lots(fills, [bonus(ex="2026-06-01")])
    assert sum(l.quantity for l in rb.open_lots) == 50
    assert not rb.unmatched_sells, "the bonus shares should have covered the sale"


def test_without_the_action_the_same_sale_is_unmatched():
    """This is the state the live book was in before recording anything."""
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0),
             fill("X", "2026-07-01T10:00:00", "SELL", 150, 300.0)]
    rb = TB.build_lots(fills)
    assert rb.unmatched_sells and rb.unmatched_sells[0]["quantity"] == 50


def test_the_bonus_is_sized_from_the_position_on_the_ex_date():
    """Not from the original purchase, and not from the final holding."""
    fills = [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0),
             fill("X", "2026-02-01T10:00:00", "SELL", 60, 550.0),   # 40 left at ex-date
             fill("X", "2026-07-01T10:00:00", "BUY", 10, 300.0)]
    rb = TB.build_lots(fills, [bonus(ex="2026-06-01")])
    nil = next(l for l in rb.open_lots if l.price == 0.0)
    assert nil.quantity == 40


# =====================================================================================
# persistence and rebuild
# =====================================================================================
def test_a_recorded_action_survives_a_rebuild(conn):
    TB.store_fills(conn, [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)],
                   source="console_csv")
    CA.record(conn, bonus())
    TB.rebuild_symbols(conn, ["X"])
    total = conn.execute("SELECT SUM(qty) q FROM trades WHERE symbol='X' "
                         "AND exit_ts IS NULL").fetchone()["q"]
    assert total == 200


def test_removing_an_action_rebuilds_back(conn):
    """Lots are derived, so both directions self-heal."""
    TB.store_fills(conn, [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)],
                   source="console_csv")
    aid = CA.record(conn, bonus())
    TB.rebuild_symbols(conn, ["X"])
    CA.remove(conn, aid)
    TB.rebuild_symbols(conn, ["X"])
    total = conn.execute("SELECT SUM(qty) q FROM trades WHERE symbol='X' "
                         "AND exit_ts IS NULL").fetchone()["q"]
    assert total == 100


def test_recording_the_same_action_twice_does_not_double_it(conn):
    TB.store_fills(conn, [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0)],
                   source="console_csv")
    CA.record(conn, bonus())
    CA.record(conn, bonus())
    TB.rebuild_symbols(conn, ["X"])
    total = conn.execute("SELECT SUM(qty) q FROM trades WHERE symbol='X' "
                         "AND exit_ts IS NULL").fetchone()["q"]
    assert total == 200


def test_an_action_only_affects_its_own_symbol(conn):
    TB.store_fills(conn, [fill("X", "2026-01-05T10:00:00", "BUY", 100, 500.0),
                          fill("Y", "2026-01-05T10:00:00", "BUY", 100, 500.0)],
                   source="console_csv")
    CA.record(conn, bonus(symbol="X"))
    TB.rebuild_symbols(conn, ["X", "Y"])
    y = conn.execute("SELECT SUM(qty) q FROM trades WHERE symbol='Y' "
                     "AND exit_ts IS NULL").fetchone()["q"]
    assert y == 100


def test_the_description_states_the_tax_treatment():
    assert "nil cost" in bonus().describe()
    assert "inherited" in split().describe()
