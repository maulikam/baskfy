"""OP13's drill as a test: a whole paper day per sleeve, and a skip day, with 0 orders to a broker.

`docs/options/06` OP13 AC: "the drill prints `sleeve=O1M confirms=1 fills=8 orders_to_broker=0` (and
the O2/O3 equivalents) and the journal rows". `tools/options/drill.py` runs the plan builder, the
confirm handler, the real gateway, the exit rules, the monitor's sweep and the ledger against
PostgreSQL; this runs it and reads what it printed. Skipped without `BASKFY_TEST_DATABASE_URL`.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

DRILL = Path(__file__).resolve().parents[2] / "tools" / "options" / "drill.py"


@pytest.mark.skipif(not os.environ.get("BASKFY_TEST_DATABASE_URL"),
                    reason="BASKFY_TEST_DATABASE_URL is not set")
def test_the_drill_trades_every_sleeve_on_paper_and_reaches_no_broker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = importlib.util.spec_from_file_location("options_drill", DRILL)
    assert spec is not None and spec.loader is not None
    drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drill)
    assert drill.main([]) == 0
    out = capsys.readouterr().out
    for line in ("sleeve=O1M confirms=1 fills=8 orders_to_broker=0",
                 "sleeve=O1W confirms=1 fills=8 orders_to_broker=0",
                 "sleeve=O2 confirms=1 fills=2 orders_to_broker=0",
                 "sleeve=O3A confirms=1 fills=4 orders_to_broker=0",
                 "sleeve=O3B confirms=1 fills=4 orders_to_broker=0",
                 "sleeve=O1W confirms=0 fills=0 orders_to_broker=0"):
        assert line in out, line
    assert "skipped=NOT_CONTAINED" in out
    assert out.count("  journal ") == 5
    assert "simulated=True" in out and "simulated=False" not in out
    assert "orders_to_broker=1" not in out
