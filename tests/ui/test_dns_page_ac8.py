"""ADR-0012 AC-8: /dns is read-only and partial-deploy safe.

The page must render HTTP 200 (empty state) against a pre-ADR-0012 DB that lacks
the new tables and the kick_events.trigger column, and it must expose no write
verb — the ADR-0002 read-only guarantee still holds with /dns added.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Pre-ADR-0012 daemon DB: no dns tables, kick_events has no `trigger`.
PRE_0012_SCHEMA = """
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
    ap_cu_total INTEGER,
    name TEXT
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


def _pre_0012_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(PRE_0012_SCHEMA)
    conn.execute(
        "INSERT INTO client_samples (ts, mac, name) VALUES (?, ?, ?)",
        (time.time(), "aa:bb:cc:dd:ee:01", "wled-kitchen"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_ac_8_dns_page_renders_empty_on_pre_upgrade_db(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=_pre_0012_db(tmp_path))
    with TestClient(app) as client:
        response = client.get("/dns")

    assert response.status_code == 200, "a DB missing the DNS tables/column must not 500 /dns"
    assert "no dns" in response.text.lower() or "not yet" in response.text.lower(), (
        "an empty DNS state must render, not a crash"
    )


def test_ac_8_dns_page_is_read_only(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=_pre_0012_db(tmp_path))
    with TestClient(app) as client:
        # A write verb against /dns must not be served (read-only sidecar, AC-6).
        post = client.post("/dns")
    assert post.status_code == 405, (
        f"/dns must reject write verbs (read-only); got {post.status_code}"
    )
