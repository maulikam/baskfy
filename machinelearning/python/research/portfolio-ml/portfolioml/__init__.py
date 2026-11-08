"""Swing-ML research pack: triple-barrier labels + LightGBM cross-sectional ranker over Baskfy's
``ohlcv_daily`` export. ``panel`` is the only module that reads disk; the rest are frames in,
frames out (law 1 shape, so they can move into ``baskfy_core`` unchanged)."""
