"""``docs/options/04`` §12 — the journal in R, never pooling sleeves, simulated or sizing modes."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import ClassVar

import pytest

from baskfy_core.options.config import ExpiryKind, SizingMode, Sleeve
from baskfy_core.options.journal import (
    Bucket,
    JournalRow,
    PooledRows,
    PoolKey,
    max_drawdown,
    r_multiple,
    summarize,
    summarize_one,
)

R = Decimal(1000)
ONE_LOT = SizingMode.PAPER_ONE_LOT


def _row(  # noqa: PLR0913 - one argument per journal column
    sleeve: Sleeve,
    day: int,
    r: str | None,
    *,
    simulated: bool = True,
    mode: SizingMode = ONE_LOT,
    closed: str = "PROFIT",
    kind: ExpiryKind | None = None,
    minutes: int = 60,
) -> JournalRow:
    if r is None:
        return JournalRow(
            sleeve, dt.date(2026, 10, day), simulated, mode, traded=False, skip_reason=closed
        )
    return JournalRow(
        sleeve, dt.date(2026, 10, day), simulated, mode, traded=True, expiry_kind=kind,
        net_pnl_inr=Decimal(r) * R, r_inr=R, closed_reason=closed, minutes_held=minutes,
        mae_r=Decimal("-0.5"), mfe_r=Decimal(r).copy_abs(),
    )  # fmt: skip


class TestNeverPooled:
    def test_one_summary_per_sleeve_simulated_and_sizing_mode(self) -> None:
        rows = [
            _row(Sleeve.O1M, 27, "1"),
            _row(Sleeve.O1M, 27, "1", simulated=False),
            _row(Sleeve.O2, 21, "-1"),
            _row(Sleeve.O2, 22, "2", mode=SizingMode.BUDGET),
            _row(Sleeve.O3A, 20, "1"),
            _row(Sleeve.O3B, 20, "1"),
        ]
        keys = [s.key for s in summarize(rows)]
        assert len(keys) == len(set(keys)) == 6
        assert PoolKey(Sleeve.O1M, False, ONE_LOT) in keys
        assert PoolKey(Sleeve.O3A, True, ONE_LOT) in keys
        assert PoolKey(Sleeve.O3B, True, ONE_LOT) in keys

    @pytest.mark.parametrize(
        "other",
        [
            _row(Sleeve.O1W, 20, "1"),
            _row(Sleeve.O1M, 27, "1", simulated=False),
            _row(Sleeve.O1M, 27, "1", mode=SizingMode.BUDGET),
        ],
    )
    def test_summarize_one_refuses_mixed_rows(self, other: JournalRow) -> None:
        with pytest.raises(PooledRows):
            summarize_one([_row(Sleeve.O1M, 27, "1"), other])

    def test_no_rows_is_not_a_summary(self) -> None:
        with pytest.raises(PooledRows):
            summarize_one([])


class TestTheNumbers:
    ROWS: ClassVar[list[JournalRow]] = [
        _row(Sleeve.O2, 19, "1", closed="TARGET", minutes=30),
        _row(Sleeve.O2, 20, "-2", closed="STOP", minutes=10),
        _row(Sleeve.O2, 21, "-1", closed="STOP", minutes=20),
        _row(Sleeve.O2, 22, "3", closed="TARGET", minutes=60),
        _row(Sleeve.O2, 23, None, closed="GAP_TOO_BIG"),
        _row(Sleeve.O2, 26, None, closed="GAP_TOO_BIG"),
        _row(Sleeve.O2, 27, None, closed="VIX_TOO_HIGH"),
    ]

    def test_counts_and_skips_by_reason(self) -> None:
        s = summarize_one(self.ROWS)
        assert (s.count, s.traded) == (7, 4)
        assert s.skipped_by_reason == (("GAP_TOO_BIG", 2), ("VIX_TOO_HIGH", 1))

    def test_r_statistics(self) -> None:
        s = summarize_one(self.ROWS)
        assert s.win_rate == Decimal("0.5")
        assert s.mean_r == s.expectancy_r == Decimal("0.25")
        assert s.expectancy_inr == Decimal(250)
        assert s.worst_r == Decimal(-2)

    def test_drawdown_on_the_running_sum(self) -> None:
        """+1, -2, -1, +3: running 1, -1, -2, 1 → peak 1, trough -2 → 3R."""
        s = summarize_one(self.ROWS)
        assert s.max_drawdown_r == Decimal(3)
        assert s.max_drawdown_inr == Decimal(3000)

    def test_distributions_and_splits(self) -> None:
        s = summarize_one(self.ROWS)
        assert s.mean_minutes_held == Decimal(30)
        assert s.mfe_r == (Decimal(1), Decimal(1), Decimal(2), Decimal(3))
        assert dict(s.by_closed_reason) == {
            "STOP": Bucket(2, Decimal("-1.5")),
            "TARGET": Bucket(2, Decimal(2)),
        }
        assert dict(s.by_weekday) == {
            1: Bucket(1, Decimal(1)),
            2: Bucket(1, Decimal(-2)),
            3: Bucket(1, Decimal(-1)),
            4: Bucket(1, Decimal(3)),
        }
        assert s.by_expiry_kind == ()

    def test_expiry_kind_split_for_o3(self) -> None:
        rows = [
            _row(Sleeve.O3A, 20, "1", kind=ExpiryKind.WEEKLY),
            _row(Sleeve.O3A, 27, "-1", kind=ExpiryKind.MONTHLY),
            _row(Sleeve.O3A, 13, "2", kind=ExpiryKind.WEEKLY),
        ]
        s = summarize_one(rows)
        assert dict(s.by_expiry_kind) == {
            "MONTHLY": Bucket(1, Decimal(-1)),
            "WEEKLY": Bucket(2, Decimal("1.5")),
        }
        assert s.by_weekday == ()

    def test_only_skips(self) -> None:
        s = summarize_one([_row(Sleeve.O1M, 27, None, closed="ER_TOO_HIGH")])
        assert s.traded == 0
        assert s.win_rate is None
        assert s.mean_r is None
        assert s.worst_r is None
        assert s.max_drawdown_r == 0


class TestHelpers:
    def test_r_multiple(self) -> None:
        assert r_multiple(Decimal(-1500), R) == Decimal("-1.5")
        with pytest.raises(ValueError, match="positive"):
            r_multiple(Decimal(1), Decimal(0))

    def test_drawdown_from_the_first_trade(self) -> None:
        assert max_drawdown([Decimal(-1), Decimal(-1)]) == Decimal(2)
        assert max_drawdown([Decimal(1), Decimal(2)]) == 0
