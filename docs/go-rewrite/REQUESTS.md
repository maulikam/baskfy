# REQUESTS — cross-lane asks

Format in `04-protocol.md`. L0 answers `domain`/`config`/`db`/`testkit` asks within 15 minutes.
Never block on an open request — stub in your own package and swap when it lands.

- **L1 → swing (SW12, B11):** the `swing` lane of `tools/parity/golden.py` (`uv run python ../tools/parity/golden.py swing` from `decile-blueprint`) dumps `detect_setups`, `size_position`, `manage`, `exposure_tier`, `build_entries` and `evaluate_trigger` over a fixed fixture set into `go/testdata/golden/L1/swing/<function>.case_NNN.json` (79 cases, byte-stable, guarded by `packages/core/tests/test_swing_goldens.py`); inputs carry the adjusted bars and the full config, Decimals as strings — port the six against these before anything reads `sw_*` tables.
