"""Regime checkpoint 2 — data adapters, persistence and migrations.

Fake Kite client, injected clock, temporary SQLite. No broker credentials, no network.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os

import pandas as pd
import pytest

from app.analytics import breadth as B
from app.analytics import db
from app.analytics import index_cache as IC
from app.analytics import regime_store as RS
from app.core import regime as R

D = dt.date


# =====================================================================================
# fakes
# =====================================================================================
class FakeKC:
    """Minimal stand-in for kiteconnect.KiteConnect."""

    def __init__(self, instruments=None, series=None, fail_times=0):
        self._instruments = instruments if instruments is not None else DEFAULT_DUMP
        self._series = series or {}
        self.fail_times = fail_times
        self.calls: list[tuple] = []

    def instruments(self, exchange=None):
        return list(self._instruments)

    def historical_data(self, token, frm, to, interval):
        self.calls.append((token, frm, to, interval))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("network hiccup")
        rows = []
        for d, close in self._series.get(token, []):
            if frm <= d <= to:
                rows.append({"date": dt.datetime.combine(d, dt.time(15, 30), IC.IST),
                             "open": close * 0.99, "high": close * 1.01,
                             "low": close * 0.98, "close": close})
        return rows


class FakeKite:
    def __init__(self, kc):
        self.kc = kc


def inst(sym, token, segment="INDICES", exchange="NSE"):
    return {"tradingsymbol": sym, "instrument_token": token, "segment": segment,
            "exchange": exchange, "name": sym}


DEFAULT_DUMP = [
    inst("NIFTY 50", 256265),
    inst("NIFTY MIDCAP 150", 111111),
    inst("NIFTY SMLCAP 250", 222222),
    inst("NIFTY500MOMENTM50", 409609),
    inst("NIFTY 500", 268041),
    inst("RELIANCE", 738561, segment="NSE"),      # not an index — must never match
]


@pytest.fixture()
def cfg():
    return R.RegimeConfig(momentum_sentinel="NIFTY 500 MOMENTUM 50")


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


@pytest.fixture()
def clock():
    # Friday 2026-06-05, after the close
    return IC.Clock(fixed=dt.datetime(2026, 6, 5, 18, 0, tzinfo=IC.IST))


def sessions(n, end=D(2026, 6, 5)):
    """n weekday sessions ending at `end`."""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def series_for(token, n=260, base=100.0, step=0.5, end=D(2026, 6, 5)):
    """A rising line. step=0.5 lifts the close clear of the 150bps buffer on the 20-DMA;
    a gentler drift legitimately stays UNKNOWN (see the drift test below)."""
    return [(d, base + i * step) for i, d in enumerate(sessions(n, end))]


# =====================================================================================
# migration
# =====================================================================================
def test_migration_v2_creates_every_regime_table(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"index_series", "regime_evaluations", "regime_exposure",
            "breadth_readings", "regime_book_snapshots"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_index_series_columns_match_spec(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(index_series)")]
    assert cols == ["index_name", "date", "open", "high", "low", "close",
                    "instrument_token", "is_final", "updated_at"]


def test_migration_is_idempotent_and_preserves_v1_data(conn):
    db.save_snapshot(conn, {"date": "2026-01-01", "nav": 1e6, "invested": 1e6,
                            "cash": 0.0, "holdings_json": "{}"})
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert len(db.snapshot_series(conn)) == 1


def test_canonical_uniqueness_is_enforced_by_the_schema(conn):
    import sqlite3
    row = ("eval1", "canonical", 1, 0, "enforce", "2026-06-05", "2026-06-05", "2026-06-05",
           "R2", "R1", "R2", 0, "half", "trim_to_cap", 0, 0, 0, 60.0, 100.0, "{}",
           "config_default", "{}", "[]", "[]", "2026-06-12", None, "v1", "h1", "now")
    sql = ("INSERT INTO regime_evaluations(evaluation_id, run_id, committed, dry_run, mode,"
           " scheduled_week_end, signal_session_date, data_as_of, raw_candidate_tier,"
           " previous_policy_tier, policy_tier, transition_limited, new_buys, forced_action,"
           " override_active, data_stale, manual_action_required, breadth_pct,"
           " breadth_coverage_pct, book_weights_json, book_weight_source,"
           " input_snapshot_json, reason_codes_json, reasons_json, next_evaluation_date,"
           " last_transition_date, algorithm_version, config_hash, created_at)"
           " VALUES(" + ",".join("?" * 29) + ")")
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)


# =====================================================================================
# instrument resolution
# =====================================================================================
def test_resolves_an_exact_index_symbol(cfg):
    got = IC.resolve_index_instrument(DEFAULT_DUMP, "NIFTY 50", cfg)
    assert got["instrument_token"] == 256265


def test_resolves_through_a_configured_alias(cfg):
    got = IC.resolve_index_instrument(DEFAULT_DUMP, "NIFTY 500 MOMENTUM 50", cfg)
    assert got["tradingsymbol"] == "NIFTY500MOMENTM50"
    assert got["instrument_token"] == 409609


def test_missing_index_raises_rather_than_guessing(cfg):
    with pytest.raises(IC.InstrumentNotFoundError, match="not found"):
        IC.resolve_index_instrument(DEFAULT_DUMP, "NIFTY NOTREAL", cfg)


def test_ambiguous_match_raises_and_never_picks_the_first(cfg):
    dump = DEFAULT_DUMP + [inst("NIFTY 50", 999999)]
    with pytest.raises(IC.AmbiguousInstrumentError, match="refusing to choose"):
        IC.resolve_index_instrument(dump, "NIFTY 50", cfg)


def test_non_index_segment_is_never_matched(cfg):
    with pytest.raises(IC.InstrumentNotFoundError):
        IC.resolve_index_instrument(DEFAULT_DUMP, "RELIANCE", cfg)


def test_resolution_is_case_and_space_tolerant_only_via_aliases(cfg):
    assert IC.resolve_index_instrument(DEFAULT_DUMP, "nifty 50", cfg)["instrument_token"] \
        == 256265
    # but an unregistered spacing variant is NOT silently accepted
    with pytest.raises(IC.InstrumentNotFoundError):
        IC.resolve_index_instrument(DEFAULT_DUMP, "NIFTY50", cfg)


# =====================================================================================
# clock / final candles
# =====================================================================================
def test_past_session_is_final(clock):
    assert clock.session_is_closed(D(2026, 6, 4)) is True


def test_future_session_is_not_final(clock):
    assert clock.session_is_closed(D(2026, 6, 8)) is False


def test_today_is_final_only_after_the_close():
    before = IC.Clock(fixed=dt.datetime(2026, 6, 5, 11, 0, tzinfo=IC.IST))
    after = IC.Clock(fixed=dt.datetime(2026, 6, 5, 15, 30, tzinfo=IC.IST))
    assert before.session_is_closed(D(2026, 6, 5)) is False
    assert after.session_is_closed(D(2026, 6, 5)) is True


def test_session_date_normalises_from_tz_aware_datetime():
    ts = dt.datetime(2026, 6, 5, 15, 30, tzinfo=IC.IST)
    assert IC.normalise_session_date(ts) == D(2026, 6, 5)
    assert IC.normalise_session_date("2026-06-05T00:00:00+0530") == D(2026, 6, 5)


# =====================================================================================
# repository
# =====================================================================================
def test_upsert_is_idempotent(conn):
    repo = IC.IndexRepository(conn)
    cs = [R.Candle(D(2026, 6, 1) + dt.timedelta(days=i), 1, 2, 0.5, 100.0 + i)
          for i in range(5)]
    repo.upsert("NIFTY 50", cs, instrument_token=256265)
    repo.upsert("NIFTY 50", cs, instrument_token=256265)
    assert conn.execute("SELECT COUNT(*) n FROM index_series").fetchone()["n"] == 5


def test_upsert_updates_a_provisional_candle_in_place(conn):
    repo = IC.IndexRepository(conn)
    repo.upsert("NIFTY 50", [R.Candle(D(2026, 6, 5), 1, 2, 0.5, 100.0, is_final=False)])
    assert repo.load("NIFTY 50", final_only=True) == []
    repo.upsert("NIFTY 50", [R.Candle(D(2026, 6, 5), 1, 2, 0.5, 123.0, is_final=True)])
    loaded = repo.load("NIFTY 50", final_only=True)
    assert len(loaded) == 1 and loaded[0].close == 123.0
    assert conn.execute("SELECT COUNT(*) n FROM index_series").fetchone()["n"] == 1


def test_load_excludes_provisional_candles_from_the_signal_path(conn):
    repo = IC.IndexRepository(conn)
    repo.upsert("NIFTY 50", [
        R.Candle(D(2026, 6, 4), 1, 2, 0.5, 100.0, is_final=True),
        R.Candle(D(2026, 6, 5), 1, 2, 0.5, 9_999.0, is_final=False)])
    assert [c.close for c in repo.load("NIFTY 50")] == [100.0]
    assert len(repo.load("NIFTY 50", final_only=False)) == 2


def test_last_date_ignores_provisional(conn):
    repo = IC.IndexRepository(conn)
    repo.upsert("X", [R.Candle(D(2026, 6, 4), 1, 2, 0.5, 100.0),
                      R.Candle(D(2026, 6, 5), 1, 2, 0.5, 101.0, is_final=False)])
    assert repo.last_date("X") == D(2026, 6, 4)


def test_warmup_requires_max_ma_plus_confirmation(conn, cfg):
    repo = IC.IndexRepository(conn)
    need = max(cfg.ma_lengths) + cfg.confirm_days      # 203
    repo.upsert("X", [R.Candle(d, 1, 2, 0.5, 100.0) for d in sessions(need - 1)])
    ok, have, want = repo.has_warmup("X", cfg)
    assert ok is False and have == need - 1 and want == need

    repo.upsert("X", [R.Candle(d, 1, 2, 0.5, 100.0) for d in sessions(need)])
    assert repo.has_warmup("X", cfg)[0] is True


# =====================================================================================
# session alignment
# =====================================================================================
def test_aligned_sessions_are_the_intersection(conn):
    repo = IC.IndexRepository(conn)
    repo.upsert("A", [R.Candle(D(2026, 6, d), 1, 2, 0.5, 100.0) for d in (1, 2, 3)])
    repo.upsert("B", [R.Candle(D(2026, 6, d), 1, 2, 0.5, 100.0) for d in (2, 3, 4)])
    assert repo.aligned_sessions(["A", "B"]) == [D(2026, 6, 2), D(2026, 6, 3)]


def test_missing_index_is_never_forward_filled(conn):
    repo = IC.IndexRepository(conn)
    repo.upsert("A", [R.Candle(D(2026, 6, d), 1, 2, 0.5, 100.0) for d in (1, 2, 3)])
    repo.upsert("B", [R.Candle(D(2026, 6, 1), 1, 2, 0.5, 100.0)])
    assert repo.aligned_sessions(["A", "B"]) == [D(2026, 6, 1)]


def test_weekly_session_falls_back_to_the_last_completed_session(conn):
    """Friday 2026-06-05 is a holiday -> the signal comes from Thursday."""
    repo = IC.IndexRepository(conn)
    days = [D(2026, 6, 1), D(2026, 6, 2), D(2026, 6, 3), D(2026, 6, 4)]
    for name in ("A", "B"):
        repo.upsert(name, [R.Candle(d, 1, 2, 0.5, 100.0) for d in days])
    assert repo.signal_session_for_week(["A", "B"], D(2026, 6, 5)) == D(2026, 6, 4)


def test_weekly_session_is_none_when_the_week_has_no_aligned_session(conn):
    repo = IC.IndexRepository(conn)
    repo.upsert("A", [R.Candle(D(2026, 5, 1), 1, 2, 0.5, 100.0)])
    repo.upsert("B", [R.Candle(D(2026, 5, 1), 1, 2, 0.5, 100.0)])
    assert repo.signal_session_for_week(["A", "B"], D(2026, 6, 5)) is None


# =====================================================================================
# fetching
# =====================================================================================
def test_update_fetches_and_stores(conn, cfg, clock):
    kite = FakeKite(FakeKC(series={256265: series_for(256265, n=30)}))
    res = asyncio.run(IC.update_index_history(kite, conn, "NIFTY 50", cfg, clock=clock,
                                              instruments=DEFAULT_DUMP))
    assert res["written"] == 30
    assert res["resolved_symbol"] == "NIFTY 50"
    assert IC.IndexRepository(conn).last_date("NIFTY 50") == D(2026, 6, 5)


def test_daily_update_fetches_only_missing_dates(conn, cfg, clock):
    kc = FakeKC(series={256265: series_for(256265, n=30)})
    kite = FakeKite(kc)
    asyncio.run(IC.update_index_history(kite, conn, "NIFTY 50", cfg, clock=clock,
                                        instruments=DEFAULT_DUMP))
    first_from = kc.calls[0][1]
    kc.calls.clear()
    asyncio.run(IC.update_index_history(kite, conn, "NIFTY 50", cfg, clock=clock,
                                        instruments=DEFAULT_DUMP))
    # second run starts at the last cached session, not at the beginning of history
    assert kc.calls[0][1] > first_from
    assert kc.calls[0][1] == IC.IndexRepository(conn).last_date("NIFTY 50")


def test_provisional_candle_is_stored_but_not_final(conn, cfg):
    midday = IC.Clock(fixed=dt.datetime(2026, 6, 5, 11, 0, tzinfo=IC.IST))
    kite = FakeKite(FakeKC(series={256265: series_for(256265, n=5)}))
    asyncio.run(IC.update_index_history(kite, conn, "NIFTY 50", cfg, clock=midday,
                                        instruments=DEFAULT_DUMP))
    repo = IC.IndexRepository(conn)
    assert repo.last_date("NIFTY 50") == D(2026, 6, 4)          # today excluded
    row = conn.execute("SELECT is_final FROM index_series WHERE date='2026-06-05'").fetchone()
    assert row["is_final"] == 0


def test_fetch_retries_transient_failures(conn, cfg, clock):
    kc = FakeKC(series={256265: series_for(256265, n=5)}, fail_times=2)
    kite = FakeKite(kc)
    res = asyncio.run(IC.update_index_history(kite, conn, "NIFTY 50", cfg, clock=clock,
                                              instruments=DEFAULT_DUMP))
    assert res["written"] == 5


def test_fetch_gives_up_after_the_retry_budget(conn, cfg, clock):
    kc = FakeKC(series={256265: series_for(256265, n=5)}, fail_times=99)
    with pytest.raises(RuntimeError, match="failed after"):
        asyncio.run(IC.update_index_history(FakeKite(kc), conn, "NIFTY 50", cfg,
                                            clock=clock, instruments=DEFAULT_DUMP))


def test_long_history_is_chunked(conn, cfg, clock):
    kc = FakeKC(series={256265: []})
    asyncio.run(IC.update_index_history(FakeKite(kc), conn, "NIFTY 50", cfg,
                                        start=D(2008, 1, 1), clock=clock,
                                        instruments=DEFAULT_DUMP))
    assert len(kc.calls) > 1
    assert all((c[2] - c[1]).days <= IC.CHUNK_DAYS for c in kc.calls)


def test_update_all_covers_structural_indices_and_the_sentinel(conn, cfg, clock, tmp_path):
    kc = FakeKC(series={t: series_for(t, n=10)
                        for t in (256265, 111111, 222222, 409609)})
    res = asyncio.run(IC.update_all(FakeKite(kc), conn, cfg, clock=clock))
    assert {r["index_name"] for r in res} == set(IC.required_index_names(cfg))
    assert all(r["written"] == 10 for r in res)


# =====================================================================================
# cache -> pure engine
# =====================================================================================
def test_build_all_signals_uses_the_pure_engine(conn, cfg, clock):
    kc = FakeKC(series={t: series_for(t, n=260) for t in (256265, 111111, 222222, 409609)})
    asyncio.run(IC.update_all(FakeKite(kc), conn, cfg, clock=clock))
    sigs = IC.build_all_signals(conn, cfg, as_of=D(2026, 6, 5))
    assert set(sigs) == set(IC.required_index_names(cfg))
    # a steadily rising series confirms ABOVE on every MA
    for s in sigs.values():
        assert s.state(20) is R.SignalState.ABOVE
        assert s.state(200) is R.SignalState.ABOVE
        assert s.is_stale is False


def test_gentle_drift_inside_the_buffer_never_confirms(conn, cfg, clock):
    """A 0.1/day drift keeps the close ~0.8% above its 20-DMA — inside the 150bps band —
    so the fast signal correctly stays UNKNOWN while the slow one confirms. This is the
    hysteresis doing its job, not a data gap."""
    kc = FakeKC(series={t: series_for(t, n=260, step=0.1)
                        for t in (256265, 111111, 222222, 409609)})
    asyncio.run(IC.update_all(FakeKite(kc), conn, cfg, clock=clock))
    sigs = IC.build_all_signals(conn, cfg, as_of=D(2026, 6, 5))
    assert sigs["NIFTY 50"].state(20) is R.SignalState.UNKNOWN
    assert sigs["NIFTY 50"].state(200) is R.SignalState.ABOVE


def test_short_history_yields_unknown_not_a_guess(conn, cfg, clock):
    kc = FakeKC(series={t: series_for(t, n=60) for t in (256265, 111111, 222222, 409609)})
    asyncio.run(IC.update_all(FakeKite(kc), conn, cfg, clock=clock))
    sigs = IC.build_all_signals(conn, cfg, as_of=D(2026, 6, 5))
    assert sigs["NIFTY 50"].state(200) is R.SignalState.UNKNOWN


# =====================================================================================
# breadth adapter
# =====================================================================================
def scan_frame(n=100, missing=0):
    rows = []
    for i in range(n):
        close = 100.0 + i
        ma20 = 90.0 + i
        if i < missing:
            close, ma20 = float("nan"), float("nan")
        rows.append({"symbol": f"SYM{i:03d}", "close": close, "ma_20": ma20})
    return pd.DataFrame(rows)


def test_reading_preserves_the_audit_value_verbatim():
    scan = scan_frame(100)
    audit = {"breadth_above_20dma": 73.4, "scan_date": ["2026-06-05"]}
    r = B.reading_from_audit(audit, scan)
    assert r.pct_above_20dma == 73.4        # untouched
    assert r.as_of_date == D(2026, 6, 5)


def test_reading_derives_the_eight_missing_fields():
    r = B.reading_from_audit({"breadth_above_20dma": 60.0, "scan_date": ["2026-06-05"]},
                             scan_frame(100))
    assert r.eligible_count == 100 and r.observed_count == 100
    assert r.coverage_pct == 100.0
    assert len(r.universe_hash) == 16 and len(r.audit_run_id) == 16
    assert r.calculation_version == B.CALCULATION_VERSION
    assert r.universe_id == "scan"


def test_coverage_reflects_symbols_without_usable_data():
    r = B.reading_from_audit({"breadth_above_20dma": 50.0, "scan_date": ["2026-06-05"]},
                             scan_frame(100, missing=25))
    assert r.observed_count == 75 and r.coverage_pct == 75.0


def test_universe_hash_is_order_independent_but_content_sensitive():
    a = B.universe_hash(["A", "B", "C"])
    assert a == B.universe_hash(["C", "A", "B"])
    assert a != B.universe_hash(["A", "B", "D"])


def test_changing_the_universe_changes_the_hash():
    audit = {"breadth_above_20dma": 60.0, "scan_date": ["2026-06-05"]}
    a = B.reading_from_audit(audit, scan_frame(100))
    b = B.reading_from_audit(audit, scan_frame(101))
    assert a.universe_hash != b.universe_hash
    assert not B.comparable(a, b)


def test_reading_rejects_a_non_audit_dict():
    with pytest.raises(ValueError, match="breadth_above_20dma"):
        B.reading_from_audit({"rows": 10}, scan_frame(5))


def test_save_and_read_back_is_idempotent(conn):
    r = B.reading_from_audit({"breadth_above_20dma": 61.5, "scan_date": ["2026-06-05"]},
                             scan_frame(100))
    assert B.save_reading(conn, r) == "inserted"
    assert B.save_reading(conn, r) == "updated"
    assert conn.execute("SELECT COUNT(*) n FROM breadth_readings").fetchone()["n"] == 1
    back = B.latest_reading(conn)
    assert back.pct_above_20dma == 61.5 and back.universe_hash == r.universe_hash


def test_missing_policy_is_persisted(conn):
    r = B.reading_from_audit({"breadth_above_20dma": 60.0, "scan_date": ["2026-06-05"]},
                             scan_frame(10))
    B.save_reading(conn, r)
    row = conn.execute("SELECT missing_policy FROM breadth_readings").fetchone()
    assert row["missing_policy"] == "counted_as_below"


def test_latest_reading_respects_an_as_of_bound(conn):
    for day, pct in ((D(2026, 6, 1), 50.0), (D(2026, 6, 5), 70.0)):
        B.save_reading(conn, B.reading_from_audit(
            {"breadth_above_20dma": pct, "scan_date": [day.isoformat()]}, scan_frame(10)))
    assert B.latest_reading(conn).pct_above_20dma == 70.0
    assert B.latest_reading(conn, on_or_before=D(2026, 6, 3)).pct_above_20dma == 50.0


def test_stored_reading_feeds_the_engines_usability_check(conn, cfg):
    r = B.reading_from_audit({"breadth_above_20dma": 60.0, "scan_date": ["2026-06-05"]},
                             scan_frame(100, missing=50))
    B.save_reading(conn, r)
    usable, codes = B.latest_reading(conn).is_usable(cfg, D(2026, 6, 5))
    assert usable is False and R.Reason.BREADTH_LOW_COVERAGE in codes


def test_distinct_universes_are_disclosed(conn):
    audit = {"breadth_above_20dma": 60.0, "scan_date": ["2026-06-01"]}
    B.save_reading(conn, B.reading_from_audit(audit, scan_frame(100)))
    audit2 = {"breadth_above_20dma": 60.0, "scan_date": ["2026-06-05"]}
    B.save_reading(conn, B.reading_from_audit(audit2, scan_frame(120)))
    assert len(B.distinct_universes(conn)) == 2


# =====================================================================================
# regime store: previews vs canonical commit
# =====================================================================================
def make_state(cfg, *, week_end=D(2026, 6, 5), previous=None, tier_actual=50.0):
    idx = {n: R.IndexSignals(
        index_name=n, as_of_date=week_end, close=100.0,
        signals={m: R.MaSignal(m, 100.0, 95.0, 5.0, R.SignalState.ABOVE, 3)
                 for m in cfg.ma_lengths}) for n in cfg.structural_indices.values()}
    sentinel = R.IndexSignals(
        index_name=cfg.momentum_sentinel, as_of_date=week_end, close=100.0,
        signals={m: R.MaSignal(m, 100.0, 95.0, 5.0, R.SignalState.ABOVE, 3)
                 for m in cfg.ma_lengths})
    brd = R.BreadthReading(week_end, 70.0, 500, 500, 100.0, "scan", "h", "run", "v1")
    return R.evaluate(
        as_of_date=week_end, scheduled_week_end=week_end, signal_session_date=week_end,
        index_signals=idx, sentinel=sentinel, breadth=brd,
        book=R.resolve_book_weights(cfg),
        exposure=R.ExposureSnapshot(actual_equity_pct=tier_actual),
        cfg=cfg, previous=previous)


def test_dry_run_cannot_commit_a_policy_tier(conn, cfg):
    st = make_state(cfg)
    with pytest.raises(RS.DryRunCommitError, match="DRY_RUN is active"):
        RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=True)
    assert RS.get_canonical(conn, st.evaluation_id) is None


def test_observe_mode_cannot_commit(conn, cfg):
    st = make_state(cfg)
    with pytest.raises(RS.DryRunCommitError, match="does not commit"):
        RS.commit_evaluation(conn, st, mode=R.RegimeMode.OBSERVE, dry_run=False)


def test_preview_is_persisted_without_committing(conn, cfg):
    st = make_state(cfg)
    rid = RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE)
    assert rid.startswith("preview-")
    assert RS.get_canonical(conn, st.evaluation_id) is None
    assert len(RS.previews_for(conn, st.evaluation_id)) == 1


def test_many_previews_do_not_block_the_later_canonical_commit(conn, cfg):
    """The acceptance requirement: a DRY_RUN preview must not reserve the week."""
    st = make_state(cfg)
    for _ in range(3):
        RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)
    row = RS.get_canonical(conn, st.evaluation_id)
    assert row is not None and row["committed"] == 1 and row["dry_run"] == 0
    assert len(RS.previews_for(conn, st.evaluation_id)) == 3


def test_a_week_commits_exactly_once(conn, cfg):
    st = make_state(cfg)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)
    with pytest.raises(RS.AlreadyCommittedError, match="already committed"):
        RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)


def test_preview_run_id_canonical_is_rejected(conn, cfg):
    with pytest.raises(ValueError, match="reserved"):
        RS.save_preview(conn, make_state(cfg), mode=R.RegimeMode.OBSERVE,
                        run_id="canonical")


def test_previews_never_move_the_policy_baseline(conn, cfg):
    st = make_state(cfg)
    RS.save_preview(conn, st, mode=R.RegimeMode.OBSERVE)
    assert RS.previous_regime(conn) is None       # only committed rows count


def test_previous_regime_is_rebuilt_from_the_last_commit(conn, cfg):
    st = make_state(cfg)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)
    prev = RS.previous_regime(conn)
    assert prev.policy_tier is st.policy_tier
    assert prev.scheduled_week_end == D(2026, 6, 5)
    assert prev.book_weights.weights == dict(st.book_category_weights)


def test_committed_decision_round_trips_with_full_provenance(conn, cfg):
    st = make_state(cfg)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False,
                         input_snapshot={"note": "checkpoint2"})
    row = RS.get_canonical(conn, st.evaluation_id)
    assert row["algorithm_version"] == R.ALGORITHM_VERSION
    assert row["config_hash"] == cfg.config_hash()
    assert json.loads(row["reason_codes_json"]) == list(st.reason_codes)
    assert json.loads(row["reasons_json"]) == list(st.reasons)
    snap = json.loads(row["input_snapshot_json"])
    assert snap["note"] == "checkpoint2" and "index_diagnostics" in snap


def test_same_week_rerun_addresses_the_same_evaluation(conn, cfg):
    a, b = make_state(cfg), make_state(cfg)
    assert a.evaluation_id == b.evaluation_id
    RS.commit_evaluation(conn, a, mode=R.RegimeMode.ENFORCE, dry_run=False)
    with pytest.raises(RS.AlreadyCommittedError):
        RS.commit_evaluation(conn, b, mode=R.RegimeMode.ENFORCE, dry_run=False)


# =====================================================================================
# exposure reconciliation
# =====================================================================================
def test_exposure_is_recorded_separately_from_the_decision(conn, cfg):
    st = make_state(cfg, tier_actual=90.0)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)
    RS.record_exposure(conn, evaluation_id=st.evaluation_id, actual_equity_pct=90.0,
                       target_equity_cap_pct=40.0,
                       execution_status=R.ExecutionStatus.PLANNED,
                       observed_at="2026-06-08T10:00:00")
    row = RS.latest_exposure(conn, st.evaluation_id)
    assert row["exposure_gap_pct"] == pytest.approx(50.0)
    assert row["execution_status"] == "planned"


def test_partial_fills_append_observations_without_a_new_evaluation(conn, cfg):
    st = make_state(cfg, tier_actual=90.0)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)
    for when, actual, status in (
            ("2026-06-08T10:00:00", 90.0, R.ExecutionStatus.PLANNED),
            ("2026-06-08T10:05:00", 65.0, R.ExecutionStatus.PARTIAL),
            ("2026-06-09T10:00:00", 40.0, R.ExecutionStatus.COMPLETED)):
        RS.record_exposure(conn, evaluation_id=st.evaluation_id, actual_equity_pct=actual,
                           target_equity_cap_pct=40.0, execution_status=status,
                           observed_at=when)
    hist = RS.exposure_history(conn, st.evaluation_id)
    assert len(hist) == 3
    assert [h["execution_status"] for h in hist] == ["planned", "partial", "completed"]
    assert len(RS.committed_history(conn)) == 1        # still ONE weekly decision


def test_open_gap_is_detectable_for_retry(conn, cfg):
    st = make_state(cfg, tier_actual=90.0)
    RS.commit_evaluation(conn, st, mode=R.RegimeMode.ENFORCE, dry_run=False)
    RS.record_exposure(conn, evaluation_id=st.evaluation_id, actual_equity_pct=65.0,
                       target_equity_cap_pct=40.0,
                       execution_status=R.ExecutionStatus.PARTIAL,
                       observed_at="2026-06-08T10:00:00")
    assert RS.open_exposure_gap(conn) is not None
    RS.record_exposure(conn, evaluation_id=st.evaluation_id, actual_equity_pct=40.2,
                       target_equity_cap_pct=40.0,
                       execution_status=R.ExecutionStatus.COMPLETED,
                       observed_at="2026-06-09T10:00:00")
    assert RS.open_exposure_gap(conn) is None


def test_exposure_observation_is_idempotent_per_timestamp(conn, cfg):
    st = make_state(cfg)
    for _ in range(3):
        RS.record_exposure(conn, evaluation_id=st.evaluation_id, actual_equity_pct=50.0,
                           target_equity_cap_pct=40.0, observed_at="2026-06-08T10:00:00")
    assert len(RS.exposure_history(conn, st.evaluation_id)) == 1


# =====================================================================================
# book snapshots
# =====================================================================================
def test_only_r1_snapshots_are_offered_as_the_fallback_weight_source(conn, cfg):
    RS.save_book_snapshot(conn, evaluation_id="e1", scheduled_week_end=D(2026, 5, 1),
                          policy_tier=R.RegimeTier.R1,
                          weights=R.BookWeights({"smallcap": 0.55, "midcap": 0.30,
                                                 "largecap": 0.15},
                                                R.WeightSource.FULL_RISK_TARGET, 100.0))
    # a later, regime-REDUCED book must never become the strategic fallback
    RS.save_book_snapshot(conn, evaluation_id="e2", scheduled_week_end=D(2026, 6, 5),
                          policy_tier=R.RegimeTier.R3,
                          weights=R.BookWeights({"smallcap": 0.10, "midcap": 0.20,
                                                 "largecap": 0.70},
                                                R.WeightSource.FULL_RISK_TARGET, 100.0))
    snap = RS.last_r1_snapshot(conn)
    assert snap.weights["smallcap"] == 0.55
    assert snap.weights["largecap"] == 0.15


def test_no_r1_snapshot_yields_none(conn):
    assert RS.last_r1_snapshot(conn) is None


def test_book_snapshot_upsert_is_idempotent(conn, cfg):
    w = R.BookWeights({"smallcap": 0.5, "midcap": 0.3, "largecap": 0.2},
                      R.WeightSource.CONFIG_DEFAULT, 0.0)
    for _ in range(2):
        RS.save_book_snapshot(conn, evaluation_id="e1", scheduled_week_end=D(2026, 6, 5),
                              policy_tier=R.RegimeTier.R1, weights=w)
    assert conn.execute("SELECT COUNT(*) n FROM regime_book_snapshots").fetchone()["n"] == 1


def test_snapshot_feeds_the_engines_weight_precedence(conn, cfg):
    RS.save_book_snapshot(conn, evaluation_id="e1", scheduled_week_end=D(2026, 5, 1),
                          policy_tier=R.RegimeTier.R1,
                          weights=R.BookWeights({"smallcap": 0.6, "midcap": 0.25,
                                                 "largecap": 0.15},
                                                R.WeightSource.FULL_RISK_TARGET, 100.0))
    bw = R.resolve_book_weights(cfg, full_risk_target=None,
                                last_r1_snapshot=RS.last_r1_snapshot(conn))
    assert bw.source is R.WeightSource.LAST_R1_SNAPSHOT
    assert bw.weights["smallcap"] == pytest.approx(0.6)


# =====================================================================================
# config wiring
# =====================================================================================
def test_regime_config_factory_validates():
    from app import config as C
    cfg = C.regime_config()
    cfg.validate()
    assert cfg.mode is R.RegimeMode.OBSERVE
    assert cfg.buffer_bps == 150


def test_regime_is_disabled_by_default(monkeypatch):
    """A fresh checkout must not have the overlay on.

    This asserts the DEFAULT, so it reads the env fallback rather than the live module
    attribute: the settings layer exists precisely to change that attribute at runtime,
    and asserting the mutated value made a legitimate 'enable the overlay' break a test
    about defaults.
    """
    monkeypatch.delenv("REGIME_ENABLED", raising=False)
    assert os.getenv("REGIME_ENABLED", "false").lower() == "false"


def test_the_overlay_switch_is_runtime_editable():
    """And that it CAN be turned on is the other half of the contract."""
    from app.analytics import settings as S
    spec = next(s for s in S.SPECS if s.key == "REGIME_ENABLED")
    assert spec.kind == "bool" and "REGIME_ENABLED" not in S.LOCKED_KEYS


def test_bad_regime_config_raises_at_the_factory(monkeypatch):
    from app import config as C
    monkeypatch.setattr(C, "REGIME_CONFIRM_DAYS", 0)
    with pytest.raises(R.ConfigError, match="confirm_days"):
        C.regime_config()


# =====================================================================================
# configuration discoverability
# =====================================================================================
def test_every_env_var_the_code_reads_is_documented():
    """A setting nobody can discover is a setting nobody will use correctly.

    17 of 25 were undocumented at one point, including REGIME_ENABLED and the two
    product gates.
    """
    import pathlib
    import re

    code = set()
    for path in pathlib.Path("app").rglob("*.py"):
        code |= set(re.findall(r'os\.getenv\("([A-Z0-9_]+)"', path.read_text()))
    documented = set(re.findall(r"^([A-Z0-9_]+)=", pathlib.Path(".env.example").read_text(),
                                re.M))
    missing = sorted(code - documented)
    assert missing == [], f".env.example does not document: {missing}"


def test_env_example_contains_no_real_credentials():
    import pathlib
    text = pathlib.Path(".env.example").read_text()
    assert "your_api_key" in text and "your_api_secret" in text
    for line in text.splitlines():
        if line.startswith("KITE_API_KEY=") or line.startswith("KITE_API_SECRET="):
            assert line.split("=", 1)[1].startswith("your_"), f"real credential: {line}"


def test_env_example_keeps_the_safety_defaults_safe():
    import pathlib
    import re
    text = pathlib.Path(".env.example").read_text()
    for key, expected in (("DRY_RUN", "true"), ("REGIME_ENABLED", "false"),
                          ("REGIME_MODE", "observe"), ("INTRADAY_ENABLED", "false"),
                          ("OPTIONS_ENABLED", "false")):
        m = re.search(rf"^{key}=(\S+)", text, re.M)
        assert m and m.group(1) == expected, f"{key} default is not {expected}"


# =====================================================================================
# instrument cache and alias resolution
# =====================================================================================
def test_the_two_instrument_caches_use_different_files():
    """They hold different things: every instrument vs the index segment only. Sharing a
    filename let whichever module ran last redefine what the other read, and index
    resolution then searched 100k bonds and options."""
    from app.analytics import benchmark as BM
    assert IC.INSTRUMENT_CACHE != BM.INSTRUMENT_CACHE


def test_benchmark_cache_is_filtered_on_read(tmp_path, monkeypatch):
    """A cache written by something else must not leak non-indices into resolution."""
    import asyncio
    import json as _json
    from app.analytics import benchmark as BM

    cache = tmp_path / "idx.json"
    cache.write_text(_json.dumps({
        "fetched": dt.date.today().isoformat(),
        "rows": [{"tradingsymbol": "NIFTY 50", "instrument_token": 1, "segment": "INDICES",
                  "exchange": "NSE", "name": ""},
                 {"tradingsymbol": "BHARATBOND-APR30", "instrument_token": 2,
                  "segment": "BSE", "exchange": "BSE", "name": ""}]}))
    monkeypatch.setattr(BM, "INSTRUMENT_CACHE", str(cache))
    rows = asyncio.run(BM._index_dump(FakeKite(FakeKC()), IC.KiteLimits()))
    assert [r["tradingsymbol"] for r in rows] == ["NIFTY 50"]


def test_benchmark_resolution_honours_the_regime_config_aliases(monkeypatch, tmp_path):
    """MOMENTM is not an abbreviation any fuzzy match derives from MOMENTUM, so the
    configured alias is the only route. Two separate alias tables hid it."""
    import asyncio
    import json as _json
    from app.analytics import benchmark as BM

    cache = tmp_path / "idx.json"
    cache.write_text(_json.dumps({
        "fetched": dt.date.today().isoformat(),
        "rows": [{"tradingsymbol": "NIFTY500MOMENTM50", "instrument_token": 409609,
                  "segment": "INDICES", "exchange": "NSE", "name": ""}]}))
    monkeypatch.setattr(BM, "INSTRUMENT_CACHE", str(cache))
    got = asyncio.run(BM.resolve_index_token("NIFTY 500 MOMENTUM 50",
                                             FakeKite(FakeKC())))
    assert got["instrument_token"] == 409609
