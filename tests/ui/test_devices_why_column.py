"""/devices renders a "Why" column with the newest real kick's rationale.

Same cell as the /devices/{mac} timeline (shared _why.html macro, ADR-0015):
one-line summary plus the expandable observed-vs-threshold breakdown. A device
whose newest kick has no rationale renders a dash, and a daemon DB that
predates the rationale column still renders the page.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from tests.ui.conftest import DAEMON_SCHEMA

MAC_KICKED = "aa:bb:cc:dd:ee:ff"
MAC_NO_RATIONALE = "11:22:33:44:55:66"

OLD_RATIONALE = {
    "v": 1,
    "trigger": "rf",
    "quiet_hours": False,
    "observed": {"signal": -91, "radio": "ng"},
    "thresholds": {"signal_dbm_max": -70},
    "breached": ["signal"],
}

NEW_RATIONALE = {
    "v": 1,
    "trigger": "rf",
    "quiet_hours": False,
    "observed": {"signal": -78, "tx_rate_kbps": 6000, "radio": "ng"},
    "thresholds": {"signal_dbm_max": -70, "tx_rate_kbps_max": 12000},
    "breached": ["signal", "tx_rate_kbps"],
}


def _seed(tmp_path: Path) -> Path:
    db_path = tmp_path / "devices_why.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(DAEMON_SCHEMA)
    conn.execute("ALTER TABLE kick_events ADD COLUMN rationale TEXT")
    now = time.time()
    # MAC_KICKED: an older kick with a distinctive rationale, then a newer one —
    # only the newest kick's rationale may surface in the devices table.
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, rationale) "
        "VALUES (?, ?, 0, 'deauth', ?)",
        (now - 600, MAC_KICKED, json.dumps(OLD_RATIONALE)),
    )
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, rationale) "
        "VALUES (?, ?, 0, 'btm', ?)",
        (now - 60, MAC_KICKED, json.dumps(NEW_RATIONALE)),
    )
    # MAC_NO_RATIONALE: kicked, but the rationale cell is NULL → dash, no crash.
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, rationale) "
        "VALUES (?, ?, 0, 'deauth', NULL)",
        (now - 120, MAC_NO_RATIONALE),
    )
    conn.commit()
    conn.close()
    return db_path


def test_devices_list_renders_last_kick_rationale(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=_seed(tmp_path))
    with TestClient(app) as client:
        response = client.get("/devices")

    assert response.status_code == 200
    text = response.text

    assert "why-summary" in text, "the devices table must render the why cell"
    assert "weak signal" in text, "the one-line summary must render"

    # Values unique to the NEWEST kick's rationale render...
    for token in ("-78", "6000", "12000"):
        assert token in text, f"newest-kick rationale value {token!r} must render"
    # ...and the OLDER kick's distinctive value must not.
    assert "-91" not in text, "only the newest real kick's rationale may surface"

    # Exactly one row carries a rationale: the NULL-rationale device renders a
    # dash, not a borrowed breakdown.
    assert text.count("why-summary") == 1, (
        "a NULL-rationale device must render a dash, not another row's rationale"
    )


def test_devices_list_tolerates_pre_rationale_schema(seeded_db: Path) -> None:
    """seeded_db's kick_events has no rationale column (pre-ADR-0015 daemon)."""
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/devices")

    assert response.status_code == 200, "missing rationale column must degrade, not 500"
    assert MAC_KICKED in response.text.lower()
