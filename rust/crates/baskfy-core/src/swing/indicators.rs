//! Port of `baskfy_core.swing.indicators` (`decile-blueprint/packages/core/src/baskfy_core/swing/indicators.py`).
//!
//! Per-bar columns the swing detectors read (docs/swing/04 §1).
//!
//! Goldens: none directly; exercised through `detect_setups` (`L1/swing/detect_setups.*`), whose function under test is `detect_setups(with_swing_indicators(bars, config), as_of, config)`.
