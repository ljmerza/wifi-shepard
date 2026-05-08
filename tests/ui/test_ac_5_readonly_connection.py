"""AC-5: UI's SQLite connection uses file:...?mode=ro; write attempts fail."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_ac_5_readonly_connection_rejects_writes(seeded_db: Path) -> None:
    from wifi_shepard_ui.db import open_readonly

    conn = open_readonly(seeded_db)
    try:
        with pytest_raises_readonly():
            conn.execute(
                "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, ?)",
                (1.0, "deadbeef", 0),
            )
    finally:
        conn.close()


def test_ac_5_readonly_connection_can_read(seeded_db: Path) -> None:
    """Sanity: the read-only connection must still succeed at SELECT."""
    from wifi_shepard_ui.db import open_readonly

    conn = open_readonly(seeded_db)
    try:
        rows = list(conn.execute("SELECT mac FROM kick_events"))
    finally:
        conn.close()
    assert len(rows) == 2, f"expected 2 kick_events rows, got {len(rows)}"


def pytest_raises_readonly():
    """Helper that asserts an OperationalError mentioning read-only."""
    import pytest

    return pytest.raises(sqlite3.OperationalError, match="(?i)readonly|read-only|read only")
