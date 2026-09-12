"""AF 3.1 — reprocess rescales kite_adjusted across the M29 seam (see test_af_lane_d)."""

from __future__ import annotations

from baskfy_worker.tasks.adjustments import KITE_ADJUSTED_SOURCE, _rescale_deep_segment


def test_seam_rescale_helper_is_exported() -> None:
    assert KITE_ADJUSTED_SOURCE == "kite_adjusted"
    assert callable(_rescale_deep_segment)
