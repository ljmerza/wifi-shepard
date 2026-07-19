"""GET /devices/{mac} shows summary stat tiles above the event table:
state, real-kick count (+dry-run note), last kick, last seen, latest signal."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MAC_A = "AA:BB:CC:DD:EE:FF"  # seeded: named samples, 1 dry-run + 1 real kick
MAC_B = "11:22:33:44:55:66"  # seeded: healthy, unnamed, zero kicks


def test_summary_tiles_render_for_kicked_device(seeded_db: Path) -> None:
    """MAC_A was really kicked 90s ago (cooldown 300s) → KICKED, 1 real kick,
    1 dry-run; its newest sample is -78 dBm on the 2.4 GHz radio."""
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get(f"/devices/{MAC_A}")

    assert response.status_code == 200
    text = response.text
    for label in ["State", "Kicks", "Last kick", "Last seen", "Signal"]:
        assert label in text, f"summary tile label {label!r} must render"
    assert "badge-kicked" in text, "state tile must render the derived KICKED badge"
    assert "+1 dry-run" in text, "dry-run kicks must surface as a note, not in the kick count"
    assert "dBm" in text, "signal tile must render the newest sample's dBm value"
    assert "2.4 GHz" in text, "signal tile must note the newest sample's radio band"
    assert "wled-kitchen" in text, "controller-reported name must render in the heading"


def test_summary_state_normal_for_healthy_device(seeded_db: Path) -> None:
    """MAC_B has samples but zero kicks → NORMAL badge, no dry-run note."""
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get(f"/devices/{MAC_B}")

    assert response.status_code == 200
    text = response.text
    assert "badge-normal" in text
    assert "dry-run</div>" not in text, "no dry-run note when the device has no dry-run kicks"
    assert "5 GHz" in text, "MAC_B's newest sample is on the 5 GHz radio"


def test_summary_counts_real_kicks_only(make_db) -> None:
    """Mirrors list_devices: dry-runs must not inflate kick_count or push a
    never-actually-kicked MAC toward QUARANTINE. MAC matching is
    case-insensitive; the allowlist flag matches case-insensitively too."""
    import time

    from wifi_shepard_ui import views

    def seed(conn: sqlite3.Connection, now: float) -> None:
        for i in range(5):
            conn.execute(
                "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 1)",
                (now - 60 * i, MAC_A),
            )
        conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)",
            (now - 30, MAC_A),
        )

    db_path = make_db(seed)
    conn = sqlite3.connect(db_path)
    try:
        summary = views.device_summary(conn, mac=MAC_A.lower(), allowlist={MAC_A}, now=time.time())
    finally:
        conn.close()

    assert summary.kick_count == 1
    assert summary.dry_run_count == 5
    assert summary.state == "KICKED", "5 dry-runs + 1 real kick must not reach QUARANTINE"
    assert summary.allowlisted, "allowlist match must be case-insensitive"
    assert summary.last_seen_ts is None, "no client_samples → never seen"
    assert summary.signal is None


def test_unknown_mac_keeps_empty_state(seeded_db: Path) -> None:
    """A MAC with no rows keeps the plain empty-state page — no zero-tiles."""
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/devices/00:00:00:00:00:99")

    assert response.status_code == 200
    assert "No events recorded" in response.text
    assert "Last seen" not in response.text, "summary tiles must not render for an unknown MAC"


def test_missing_db_renders_without_tiles(tmp_path: Path) -> None:
    """AC-8 parity: the history route must still 200 with no DB file at all."""
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=tmp_path / "no_such.db")
    with TestClient(app) as client:
        response = client.get(f"/devices/{MAC_A}")

    assert response.status_code == 200
    assert "No events recorded" in response.text
