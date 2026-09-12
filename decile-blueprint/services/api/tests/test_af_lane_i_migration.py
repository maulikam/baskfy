"""AF I — migration 0045 exists and revises 0044."""

from __future__ import annotations

from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
MIGRATION = API_DIR / "alembic" / "versions" / "0045_instrument_watch_and_prefs.py"


def test_instrument_watch_migration_chains_from_0044() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0045_instrument_watch_and_prefs"' in text
    assert 'down_revision: str | None = "0044_kite_adjusted_and_ohl_raw"' in text
    assert "instrument_watch_item" in text
    assert "user_discover_preferences" in text
