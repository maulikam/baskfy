"""Tradebook importer: parsing, FIFO reconstruction, idempotency, reconciliation."""
from __future__ import annotations

import datetime as dt

import pytest

from app.analytics import db
from app.analytics import tax_lots as TL
from app.analytics import tradebook as TB

D = dt.date

HEADER = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,"
          "quantity,price,trade_id,order_id,order_execution_time")


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


def row(symbol, date, side, qty, price, time_=None, tid="T1"):
    ts = time_ or f"{date}T09:30:00"
    return (f"{symbol},INE000A01001,{date},NSE,EQ,EQ,{side},false,{qty},{price},"
            f"{tid},O{tid},{ts}")


def write(tmp_path, rows, header=HEADER, name="tradebook.csv"):
    p = tmp_path / name
    p.write_text("\n".join([header] + rows) + "\n")
    return str(p)


# =====================================================================================
# parsing
# =====================================================================================
def test_parses_the_console_export_shape(tmp_path):
    f = write(tmp_path, [row("RRKABEL", "2025-07-01", "buy", 100, 1900.0),
                         row("RRKABEL", "2026-01-15", "sell", 40, 2500.0, tid="T2")])
    fills = TB.parse_tradebook(f)
    assert [x.side for x in fills] == ["BUY", "SELL"]
    assert fills[0].symbol == "RRKABEL" and fills[0].quantity == 100
    assert fills[0].when.date() == D(2025, 7, 1)


@pytest.mark.parametrize("word,expected", [
    ("buy", "BUY"), ("BUY", "BUY"), ("B", "BUY"), ("bought", "BUY"),
    ("sell", "SELL"), ("SELL", "SELL"), ("S", "SELL"), ("sold", "SELL"),
])
def test_buy_sell_notation_variants(tmp_path, word, expected):
    f = write(tmp_path, [row("X", "2025-07-01", word, 10, 100.0)])
    assert TB.parse_tradebook(f)[0].side == expected


def test_unrecognised_trade_type_is_refused_not_guessed(tmp_path):
    f = write(tmp_path, [row("X", "2025-07-01", "transfer", 10, 100.0)])
    with pytest.raises(TB.TradebookError, match="refusing to guess"):
        TB.parse_tradebook(f)


def test_a_row_without_a_parseable_date_is_refused(tmp_path):
    f = write(tmp_path, [row("X", "not-a-date", "buy", 10, 100.0, time_="")])
    with pytest.raises(TB.TradebookError, match="acquisition date"):
        TB.parse_tradebook(f)


@pytest.mark.parametrize("date_str", ["2025-07-01", "01-07-2025", "01/07/2025",
                                      "01-Jul-2025", "01 Jul 2025"])
def test_date_formats(tmp_path, date_str):
    f = write(tmp_path, [row("X", date_str, "buy", 10, 100.0, time_="")])
    assert TB.parse_tradebook(f)[0].when.date() == D(2025, 7, 1)


def test_execution_time_is_preferred_over_the_date(tmp_path):
    f = write(tmp_path, [row("X", "2025-07-01", "buy", 10, 100.0,
                             time_="2025-07-01T14:22:05")])
    assert TB.parse_tradebook(f)[0].when.hour == 14


def test_thousands_separators_are_handled(tmp_path):
    f = write(tmp_path, [f'X,INE1,2025-07-01,NSE,EQ,EQ,buy,false,"1,500","2,150.75",T1,O1,'])
    fills = TB.parse_tradebook(f)
    assert fills[0].quantity == 1500 and fills[0].price == 2150.75


def test_unquoted_comma_is_refused_rather_than_misread(tmp_path):
    f = write(tmp_path,
              ["X,INE1,2025-07-01,NSE,EQ,EQ,buy,false,1,500,2150.75,T1,O1,2025-07-01T09:30:00"])
    with pytest.raises(TB.TradebookError, match="more fields than the header"):
        TB.parse_tradebook(f)


def test_missing_required_columns_names_them(tmp_path):
    f = write(tmp_path, ["X,2025-07-01"], header="symbol,trade_date")
    with pytest.raises(TB.TradebookError, match="missing required columns"):
        TB.parse_tradebook(f)


def test_empty_file_is_refused(tmp_path):
    with pytest.raises(TB.TradebookError):
        TB.parse_tradebook(write(tmp_path, []))


# =====================================================================================
# FIFO reconstruction
# =====================================================================================
def test_partial_sell_consumes_the_oldest_lot_first():
    fills = [
        TB.Fill("X", dt.datetime(2025, 1, 1), "BUY", 100, 50.0),
        TB.Fill("X", dt.datetime(2025, 6, 1), "BUY", 100, 80.0),
        TB.Fill("X", dt.datetime(2026, 1, 1), "SELL", 150, 100.0),
    ]
    r = TB.build_lots(fills)
    assert [c["qty"] for c in r.closed] == [100, 50]
    assert [c["entry_price"] for c in r.closed] == [50.0, 80.0]
    # 50 of the newer lot survives
    assert len(r.open_lots) == 1
    assert r.open_lots[0].quantity == 50 and r.open_lots[0].price == 80.0


def test_realised_pnl_uses_the_consumed_lots():
    fills = [TB.Fill("X", dt.datetime(2025, 1, 1), "BUY", 100, 50.0),
             TB.Fill("X", dt.datetime(2026, 1, 1), "SELL", 100, 90.0)]
    r = TB.build_lots(fills)
    assert r.closed[0]["pnl"] == pytest.approx(4000.0)
    assert r.open_lots == []


def test_a_pure_buy_history_leaves_only_open_lots():
    fills = [TB.Fill("X", dt.datetime(2025, 1, 1), "BUY", 10, 50.0),
             TB.Fill("X", dt.datetime(2025, 3, 1), "BUY", 20, 60.0)]
    r = TB.build_lots(fills)
    assert r.closed == [] and len(r.open_lots) == 2
    assert sum(l.quantity for l in r.open_lots) == 30


def test_sell_without_inventory_is_recorded_not_dropped():
    """A short, an intraday leg, or history that starts mid-position — all mean the
    lots are incomplete, which the tax review has to know."""
    fills = [TB.Fill("X", dt.datetime(2025, 1, 1), "SELL", 50, 90.0)]
    r = TB.build_lots(fills)
    assert r.unmatched_sells and r.unmatched_sells[0]["quantity"] == 50
    assert r.closed == []


def test_symbols_are_kept_independent():
    fills = [TB.Fill("A", dt.datetime(2025, 1, 1), "BUY", 10, 50.0),
             TB.Fill("B", dt.datetime(2025, 2, 1), "BUY", 20, 60.0),
             TB.Fill("A", dt.datetime(2025, 3, 1), "SELL", 10, 70.0)]
    r = TB.build_lots(fills)
    assert [c["symbol"] for c in r.closed] == ["A"]
    assert [l.symbol for l in r.open_lots] == ["B"]


def test_charges_are_apportioned_across_partial_fills():
    fills = [TB.Fill("X", dt.datetime(2025, 1, 1), "BUY", 100, 50.0, charges=100.0),
             TB.Fill("X", dt.datetime(2026, 1, 1), "SELL", 50, 90.0, charges=60.0)]
    r = TB.build_lots(fills)
    assert r.closed[0]["costs"] == pytest.approx(60.0 + 50.0)   # all the sell + half buy
    assert r.open_lots[0].charges == pytest.approx(50.0)


# =====================================================================================
# persistence + idempotency
# =====================================================================================
def test_import_writes_lots_the_tax_module_can_read(conn, tmp_path):
    f = write(tmp_path, [row("RRKABEL", "2025-09-19", "buy", 172, 2121.25)])
    res = TB.import_tradebook(conn, f)
    assert res["open_lots"] == 1 and res["closed_trades"] == 0

    lots = TL.open_lots(conn, "RRKABEL")
    assert len(lots) == 1
    assert lots[0].quantity == 172 and lots[0].price == pytest.approx(2121.25)
    assert lots[0].acquired_on == D(2025, 9, 19)
    assert lots[0].complete is True          # date AND price known


def test_imported_lots_produce_a_real_tax_review(conn, tmp_path):
    """The whole point: with lots present, review stops returning TAX_DATA_UNKNOWN."""
    f = write(tmp_path, [row("X", "2025-09-19", "buy", 100, 50.0)])
    TB.import_tradebook(conn, f)
    rev = TL.review_sale("X", 100, 100.0, TL.open_lots(conn, "X"), as_of=D(2026, 8, 14))
    assert rev.data_unknown is False
    assert rev.estimated_gain == pytest.approx(5000.0)
    # 329 days held: profitable and 36 days short of long-term, so it flags for review
    assert TL.TAX_LOT_NEAR_LTCG in rev.codes and rev.flagged is True


def test_an_older_lot_is_recognised_as_long_term(conn, tmp_path):
    f = write(tmp_path, [row("X", "2024-01-05", "buy", 100, 50.0)])
    TB.import_tradebook(conn, f)
    rev = TL.review_sale("X", 100, 100.0, TL.open_lots(conn, "X"), as_of=D(2026, 8, 14))
    assert TL.TAX_LOT_LONG_TERM in rev.codes and rev.flagged is False


def test_reimporting_the_same_file_changes_nothing(conn, tmp_path):
    f = write(tmp_path, [row("X", "2025-01-01", "buy", 100, 50.0),
                         row("X", "2026-01-01", "sell", 40, 90.0, tid="T2")])
    first = TB.import_tradebook(conn, f)
    before = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")]
    second = TB.import_tradebook(conn, f)
    after = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")]

    assert first["open_lots"] == second["open_lots"]
    assert len(before) == len(after)
    assert [(r["symbol"], r["qty"], r["entry_ts"]) for r in before] == \
           [(r["symbol"], r["qty"], r["entry_ts"]) for r in after]


def test_strategy_annotations_survive_a_rebuild(conn, tmp_path):
    """entry_score and exit_reason come from the strategy; the broker cannot know them."""
    f = write(tmp_path, [row("X", "2025-01-01", "buy", 100, 50.0)])
    TB.import_tradebook(conn, f)
    conn.execute("UPDATE trades SET entry_score=84.3, exit_reason='rank_drop' "
                 "WHERE symbol='X'")
    TB.import_tradebook(conn, f)
    r = conn.execute("SELECT entry_score, exit_reason FROM trades WHERE symbol='X'"
                     ).fetchone()
    assert r["entry_score"] == 84.3 and r["exit_reason"] == "rank_drop"


def test_import_only_rebuilds_the_symbols_in_the_file(conn, tmp_path):
    conn.execute("INSERT INTO trades(symbol, qty, entry_ts, entry_price) "
                 "VALUES('OTHER', 10, 1000.0, 5.0)")
    TB.import_tradebook(conn, write(tmp_path, [row("X", "2025-01-01", "buy", 100, 50.0)]))
    assert conn.execute("SELECT COUNT(*) c FROM trades WHERE symbol='OTHER'"
                        ).fetchone()["c"] == 1


# =====================================================================================
# reconciliation — corporate actions must not pass silently
# =====================================================================================
def test_quantity_mismatch_is_reported(conn, tmp_path):
    TB.import_tradebook(conn, write(tmp_path, [row("X", "2025-01-01", "buy", 100, 50.0)]))
    # a 1:1 bonus doubled the holding without any trade
    problems = TB.reconcile_with_holdings(conn, [{"symbol": "X", "quantity": 200}])
    assert len(problems) == 1
    assert problems[0]["difference"] == 100
    assert "corporate action" in problems[0]["likely_cause"]


def test_a_holding_with_no_history_is_reported(conn):
    problems = TB.reconcile_with_holdings(conn, [{"symbol": "NEW", "quantity": 50}])
    assert problems[0]["likely_cause"] == "no tradebook history for this symbol"


def test_matching_quantities_reconcile_clean(conn, tmp_path):
    TB.import_tradebook(conn, write(tmp_path, [row("X", "2025-01-01", "buy", 100, 50.0)]))
    assert TB.reconcile_with_holdings(conn, [{"symbol": "X", "quantity": 100}]) == []


def test_coverage_reports_what_tax_can_reason_about(conn, tmp_path):
    TB.import_tradebook(conn, write(tmp_path, [row("A", "2025-01-01", "buy", 10, 50.0)]))
    cov = TB.coverage(conn, [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}])
    assert cov["with_lots"] == 1 and cov["holdings"] == 3
    assert cov["coverage_pct"] == pytest.approx(33.3)
    assert cov["missing"] == ["B", "C"]
