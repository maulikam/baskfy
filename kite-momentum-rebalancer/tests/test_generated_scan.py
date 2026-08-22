"""M13 — the CSV cord, and what happens when it is cut.

Three acceptances, in the order the module states them:

1. the generated scan is a CSV the desk would have accepted;
2. both `/analyze` paths produce the same plan for the same date;
3. every plan carries a `screen_run_id` that resolves back to its inputs.

There is a fourth property, not in the module text but load-bearing: the generated path must not
become the default while `reconciliation/DESK-PARITY.md`'s delta table is non-empty. A generated
scan is a *smaller* tradeable universe than an uploaded one today, because unadjusted corporate
actions trip the `far_from_high` filter, and a desk that quietly stopped seeing sixteen names
would look like it was working.
"""

from __future__ import annotations

import datetime as dt
import inspect
import io
import sys
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app import scan_source
from app.scoring import REQUIRED, load_scan
from baskfy_core import momentum_scan as ms

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "decile-blueprint" / "packages" / "core" / "tests"
sys.path.insert(0, str(FIXTURE_DIR))
import _scan_fixture as fixture  # noqa: E402


@pytest.fixture
def generated() -> ms.MomentumScan:
    days = fixture.trading_days()
    return ms.build(fixture.bars(), days[-1], days, cfg=C, carried=fixture.carried())


# =====================================================================================
# 1. the generated scan is a CSV the desk would have accepted
# =====================================================================================
def test_the_desks_own_loader_accepts_a_generated_scan(generated: ms.MomentumScan) -> None:
    """Not "has the right columns" — actually run `load_scan`, the function that has rejected
    real exports before, over the actual bytes."""
    frame = load_scan(io.BytesIO(generated.to_csv()))
    assert list(frame.columns) == REQUIRED
    assert len(frame) == generated.frame.height


def test_it_survives_the_round_trip_through_a_file(generated: ms.MomentumScan, tmp_path: Path) -> None:
    """A CSV that only works from memory is not a CSV the desk would have accepted."""
    path = tmp_path / "generated.csv"
    path.write_bytes(generated.to_csv())
    assert list(load_scan(str(path)).columns) == REQUIRED


# =====================================================================================
# 2. both /analyze paths produce the same plan for the same date
# =====================================================================================
class _FakeKite:
    """Enough of the Kite client for a plan, and nothing that could place an order."""

    def is_authed(self) -> bool:
        return True

    def holdings(self) -> list[dict]:
        return []

    def available_cash(self) -> float:
        return 1_000_000.0

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        return dict.fromkeys(symbols, 100.0)

    def positions(self) -> dict:
        return {"net": [], "day": []}

    def margins(self) -> dict:
        return {"equity": {"available": {"live_balance": 1_000_000.0}}}


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch, generated: ms.MomentumScan, tmp_path: Path):
    monkeypatch.setattr(M, "kite", lambda: _FakeKite())
    monkeypatch.setattr(M, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(scan_source, "generate", lambda *a, **k: generated)
    # The carried columns normally come from the newest uploaded scan; here they come from the
    # fixture, so the two paths are being compared on identical inputs rather than similar ones.
    monkeypatch.setattr(M, "_carried_columns", lambda: fixture.carried())
    return TestClient(M.app)


def _comparable(plan: dict) -> dict:
    """Everything except what is *supposed* to differ between two runs."""
    return {k: v for k, v in plan.items() if k not in {"plan_id", "created_at", "scan"}}


def test_both_paths_produce_the_same_plan(desk, generated: ms.MomentumScan) -> None:
    uploaded = desk.post(
        "/analyze", files={"scan": ("scan.csv", generated.to_csv(), "text/csv")}
    )
    made = desk.post("/analyze", data={"generate_for": str(generated.as_of)})

    assert uploaded.status_code == 200, uploaded.text
    assert made.status_code == 200, made.text
    assert _comparable(uploaded.json()) == _comparable(made.json())

    # Comparing two empty plans proves nothing, and an empty plan is exactly what a fixture with
    # too little turnover produces — every symbol rejected as illiquid, both sides agreeing on
    # nothing at all. The fixture is tuned so DELTA and ECHO survive; this keeps it that way.
    assert uploaded.json()["orders"], "the fixture stopped producing a plan — see _scan_fixture"


def test_the_upload_path_still_works_without_generate_for(desk, generated: ms.MomentumScan) -> None:
    """M13 says upload keeps working. It is the default and it takes no new arguments."""
    response = desk.post("/analyze", files={"scan": ("s.csv", generated.to_csv(), "text/csv")})
    assert response.status_code == 200
    assert response.json()["scan"]["source"] == "upload"


def test_neither_path_is_reachable_with_no_scan_at_all(desk) -> None:
    assert desk.post("/analyze").status_code == 400


def test_a_malformed_date_is_rejected_before_any_database_is_touched(desk) -> None:
    assert desk.post("/analyze", data={"generate_for": "last tuesday"}).status_code == 400


# =====================================================================================
# 3. screen_run_id is on every plan, and it resolves
# =====================================================================================
def test_every_plan_carries_a_screen_run_id(desk, generated: ms.MomentumScan) -> None:
    uploaded = desk.post(
        "/analyze", files={"scan": ("s.csv", generated.to_csv(), "text/csv")}
    ).json()
    made = desk.post("/analyze", data={"generate_for": str(generated.as_of)}).json()

    assert uploaded["scan"]["screen_run_id"].startswith("upload:")
    assert made["scan"]["screen_run_id"] == generated.screen_run_id
    assert made["scan"]["as_of"] == generated.as_of.isoformat()
    assert made["scan"]["data_version"] == generated.data_version


def test_an_uploads_id_resolves_to_the_bytes_that_made_it(desk, generated: ms.MomentumScan) -> None:
    """Same file, same id; a different file, a different id. That is what "resolvable" means for
    an upload, which has no screen definition to name."""
    raw = generated.to_csv()
    first = desk.post("/analyze", files={"scan": ("a.csv", raw, "text/csv")}).json()
    again = desk.post("/analyze", files={"scan": ("b.csv", raw, "text/csv")}).json()
    other = desk.post("/analyze", files={"scan": ("c.csv", raw + b"\n", "text/csv")}).json()

    assert first["scan"]["screen_run_id"] == again["scan"]["screen_run_id"]
    assert other["scan"]["screen_run_id"] != first["scan"]["screen_run_id"]


def test_a_generated_ids_inputs_are_named_not_hashed_away(generated: ms.MomentumScan) -> None:
    """The id is a digest of the *inputs*, so the same inputs reproduce it anywhere."""
    assert generated.screen_run_id == ms.screen_run_id(
        "desk_momentum_v1", generated.as_of, generated.data_version
    )


# =====================================================================================
# 4. the generated path does not become the default while the delta table is not empty
# =====================================================================================
def test_generating_requires_an_explicit_date(desk) -> None:
    """There is no "just generate today's" shortcut, and there should not be one until the
    parity gate is green. Opting in has to be a typed date."""
    assert desk.post("/analyze").status_code == 400


def test_a_scan_with_unadjusted_actions_warns_on_the_plan(desk, generated, monkeypatch) -> None:
    """The contamination must reach the plan, not only the log. A short plan with no explanation
    is indistinguishable from a working desk."""
    dirty = ms.MomentumScan(
        as_of=generated.as_of,
        universe=generated.universe,
        frame=generated.frame,
        screen_run_id=generated.screen_run_id,
        data_version=generated.data_version,
        carried=generated.carried,
        suspect_symbols=("ALPHA", "BRAVO"),
    )
    monkeypatch.setattr(scan_source, "generate", lambda *a, **k: dirty)
    plan = desk.post("/analyze", data={"generate_for": str(generated.as_of)}).json()

    warnings = plan["scan"]["warnings"]
    assert warnings and "unadjusted corporate action" in warnings[0]
    assert "SMALLER universe" in warnings[0]
    assert plan["scan"]["suspect_symbols"] == ["ALPHA", "BRAVO"]


def test_the_scan_source_only_ever_reads(  ) -> None:
    """The screener's database is not the desk's to write. Nothing here may say otherwise."""
    source = inspect.getsource(scan_source)
    for statement in ("insert into", "update ", "delete from", "drop ", "commit()"):
        assert statement not in source.lower(), f"scan_source must not {statement.strip()}"


def test_generate_is_never_called_without_carried_columns() -> None:
    """`carried` is positional-required, so a caller cannot forget it and get a scan of nulls."""
    signature = inspect.signature(scan_source.generate)
    assert signature.parameters["carried"].default is inspect.Parameter.empty


# =====================================================================================
# the empty-universe case M13 made reachable
# =====================================================================================
def test_an_all_rejected_scan_produces_an_empty_plan_not_a_crash(generated: ms.MomentumScan) -> None:
    """`build_plan` divided by zero when nothing survived the filters.

    A market where every candidate sits below both its 50- and 200-day averages should produce no
    buys, not a 500 in the middle of a rebalance. The case became reachable the moment M13 allowed
    a generated scan: unadjusted corporate actions trip `far_from_high`, and a generated scan can
    come back with nothing tradeable left in it at all.
    """
    import pandas as pd

    from app.rebalance import build_plan

    frame = pd.DataFrame(generated.frame.to_dicts())
    frame["reject"] = "far_from_high;"          # every single one
    frame["SCORE"] = 0.0
    frame["rank"] = range(1, len(frame) + 1)

    plan = build_plan(frame, [], 1_000_000.0, live_prices={})
    assert plan["orders"] == []


def test_the_whole_route_survives_a_scan_with_nothing_tradeable(desk, generated, monkeypatch) -> None:
    """The same thing one layer up: a 200 with an empty plan, not a 500."""
    empty = generated.frame.with_columns(pl.lit(1.0).alias("median_volume_one_year"))
    monkeypatch.setattr(
        scan_source,
        "generate",
        lambda *a, **k: ms.MomentumScan(
            as_of=generated.as_of,
            universe=generated.universe,
            frame=empty,
            screen_run_id=generated.screen_run_id,
            data_version=generated.data_version,
            carried=generated.carried,
            suspect_symbols=(),
        ),
    )
    response = desk.post("/analyze", data={"generate_for": str(generated.as_of)})
    assert response.status_code == 200, response.text
    assert response.json()["orders"] == []
