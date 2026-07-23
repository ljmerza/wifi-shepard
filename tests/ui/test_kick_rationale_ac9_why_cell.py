"""ADR-0015 AC-9: /devices/{mac} renders a per-kick "why".

Each kick row shows a one-line plain-English rationale plus an expandable
observed-vs-threshold breakdown, sourced from kick_events.rationale. A row whose
rationale is NULL renders a dash (no crash).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from tests.ui.conftest import DAEMON_SCHEMA

MAC = "AA:BB:CC:DD:EE:FF"

RATIONALE = {
    "v": 1,
    "trigger": "rf",
    "window_samples": 5,
    "quiet_hours": False,
    "override": False,
    "observed": {
        "signal": -78,
        "tx_rate_kbps": 6000,
        "retry_pct": 41.0,
        "radio": "ng",
        "ap_cu_total": 74,
    },
    "thresholds": {
        "signal_dbm_max": -70,
        "tx_rate_kbps_max": 12000,
        "retry_pct_max": 30,
        "ap_cu_total_min": 60,
    },
    "breached": ["signal", "tx_rate_kbps", "retry_pct"],
}


def _seed(tmp_path: Path) -> Path:
    db_path = tmp_path / "rationale.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(DAEMON_SCHEMA)
    conn.execute("ALTER TABLE kick_events ADD COLUMN rationale TEXT")
    now = time.time()
    # A kick WITH rationale, and an older kick with NULL rationale.
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, trigger, rationale) "
        "VALUES (?, ?, 0, 'btm', 'rf', ?)",
        (now - 60, MAC, json.dumps(RATIONALE)),
    )
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, trigger, rationale) "
        "VALUES (?, ?, 0, 'deauth', 'rf', NULL)",
        (now - 600, MAC),
    )
    conn.commit()
    conn.close()
    return db_path


def test_ac_9_device_history_renders_kick_rationale(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    db_path = _seed(tmp_path)
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        response = client.get(f"/devices/{MAC}")

    assert response.status_code == 200, "AC-9: the device page must render"
    text = response.text
    lower = text.lower()

    assert "why" in lower, "AC-9: the timeline must expose a 'why' surface for kicks"

    # Observed values + thresholds come ONLY from the rationale (no matching
    # client_samples are seeded), so their presence proves the rationale rendered.
    for token in ("-78", "-70", "6000", "12000", "41", "30", "74"):
        assert token in text, (
            f"AC-9: rationale value {token!r} must render in the observed-vs-threshold breakdown"
        )

    # The NULL-rationale kick must not borrow the other row's summary: the
    # distinctive observed signal appears exactly once (the row that has it).
    assert text.count("-78") == 1, (
        "AC-9: a NULL-rationale row must render a dash, not another row's rationale"
    )
