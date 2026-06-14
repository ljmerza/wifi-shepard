"""Partial-deploy resilience: a daemon DB written before the AP-stats tables (and
before the client_samples `name` column) existed must not 500 the UI — the
overview tiles still compute and the devices page still renders."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

# Pre-upgrade daemon DB: client_samples + kick_events only. No ap_samples /
# ap_radio_samples, and client_samples lacks the `name` column.
PRE_UPGRADE_SCHEMA = """
CREATE TABLE client_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    signal INTEGER,
    tx_rate_kbps INTEGER,
    tx_retries INTEGER,
    wifi_tx_attempts INTEGER,
    radio TEXT,
    ap_id TEXT,
    ap_cu_total INTEGER
);
CREATE TABLE kick_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    mechanism TEXT NOT NULL DEFAULT 'deauth',
    target_bssid TEXT,
    attempt_group TEXT
);
"""


def _make_pre_upgrade_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "old_state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(PRE_UPGRADE_SCHEMA)
    conn.execute(
        "INSERT INTO client_samples "
        "(ts, mac, signal, tx_rate_kbps, tx_retries, wifi_tx_attempts, radio, ap_id, ap_cu_total) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), "aa:bb:cc:dd:ee:01", -70, 6000, 0, 100, "ng", "ap1", 70),
    )
    conn.commit()
    conn.close()
    return db_path


def test_overview_renders_tiles_when_ap_tables_absent(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=_make_pre_upgrade_db(tmp_path))
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200, "a missing ap_samples table must not 500 the overview"
    text = response.text
    tiles = re.findall(r'<div class="value">(\d+)</div>', text)
    assert len(tiles) == 4, f"all four tiles must still render; got {tiles}"
    assert tiles[0] == "1", "tracked-clients tile must still compute from client_samples"
    assert "no ap data" in text.lower(), "empty AP state, not a crash"


def test_devices_renders_when_name_column_absent(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=_make_pre_upgrade_db(tmp_path))
    with TestClient(app) as client:
        response = client.get("/devices")

    assert response.status_code == 200, "a missing name column must not 500 the devices page"
    assert "aa:bb:cc:dd:ee:01" in response.text.lower()
