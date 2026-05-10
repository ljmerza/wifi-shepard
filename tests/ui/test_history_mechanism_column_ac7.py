"""ADR-0003 AC-7: device-history surface shows the kick mechanism.

GET /devices/{mac} must:
- render the mechanism (deauth / btm / deauth_fallback) for each kick row.
- keep dry-run rows visually distinguished (preserve ADR-0002 AC-3).
- visually group rows that share an attempt_group UUID (so a BTM+fallback
  pair reads as one logical kick).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# The schema this seeder pins matches the live wifi_shepard.db.SCHEMA_KICK_EVENTS
# AFTER ADR-0003's migration. tests/ui/conftest.py's DAEMON_SCHEMA must be in
# sync — that's part of AC-7 GREEN.
NEW_SCHEMA_NEEDED = """
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

MAC = "AA:BB:CC:DD:EE:FF"
ATTEMPT_GROUP = "00000000-0000-4000-8000-000000000001"


def _seed_btm_with_fallback(conn: sqlite3.Connection, now: float) -> None:
    # A speculative BTM that didn't roam, followed by a deauth_fallback under
    # the same attempt_group. Plus a third standalone deauth and one client
    # sample so the timeline isn't empty.
    conn.execute(
        "INSERT INTO client_samples "
        "(ts, mac, signal, tx_rate_kbps, tx_retries, "
        " wifi_tx_attempts, radio, ap_id, ap_cu_total) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now - 200, MAC, -78, 6000, 50, 100, "ng", "ap1", 70),
    )
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, attempt_group) "
        "VALUES (?, ?, 0, 'btm', ?)",
        (now - 180, MAC, ATTEMPT_GROUP),
    )
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, attempt_group) "
        "VALUES (?, ?, 0, 'deauth_fallback', ?)",
        (now - 120, MAC, ATTEMPT_GROUP),
    )
    # One older standalone deauth (its own attempt_group) so the test exercises
    # the "different group → no visual grouping with the pair above" case.
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, attempt_group) "
        "VALUES (?, ?, 0, 'deauth', ?)",
        (now - 600, MAC, "11111111-1111-4111-8111-111111111111"),
    )
    # Dry-run row also under the new schema (mechanism = 'btm', what the
    # speculative path would have done if dry_run had been off).
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, attempt_group) "
        "VALUES (?, ?, 1, 'btm', ?)",
        (now - 700, MAC, "22222222-2222-4222-8222-222222222222"),
    )


def _seed_btm_with_fallback_curried():
    """make_db expects a (conn, now) -> None seeder; the symbol cannot be a
    closure with a fixture inside, so this returns a plain function."""
    return _seed_btm_with_fallback


def test_ac_7_history_renders_mechanism_and_groups_attempt_group_pair(make_db) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    db_path: Path = make_db(_seed_btm_with_fallback)

    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        response = client.get(f"/devices/{MAC}")
    assert response.status_code == 200
    text = response.text
    lower = text.lower()

    # Each mechanism string from the seeded data must appear in the rendered HTML.
    assert "btm" in lower, "AC-7: 'btm' mechanism must render somewhere in the timeline"
    assert "deauth_fallback" in lower, (
        "AC-7: 'deauth_fallback' mechanism must render to distinguish fallback rows"
    )
    # The standalone deauth row must also surface its mechanism. The token
    # 'deauth' substring matches both 'deauth' and 'deauth_fallback', so just
    # check >=1 occurrences (the assertion above already covers fallback).
    assert lower.count("deauth") >= 2, (
        "AC-7: both standalone deauth and deauth_fallback rows must render their mechanism"
    )

    # Dry-run row must still be visually distinguished (ADR-0002 AC-3 contract).
    assert "dry-run" in lower, (
        "AC-7: dry-run rows must remain visually distinguished from real kicks"
    )

    # Visual grouping: rows sharing an attempt_group must surface that linkage
    # in the markup. The simplest discoverable signal is rendering the
    # attempt_group (or a stable-prefix derived from it) on each row so DOM
    # CSS / operator inspection can correlate. We require the actual UUID
    # to be present in the output for the pair.
    assert ATTEMPT_GROUP in text, (
        f"AC-7: the BTM+deauth_fallback pair's attempt_group ({ATTEMPT_GROUP}) "
        "must render in the timeline so the two rows are visually correlatable"
    )
    # And it must appear at least twice — once per row of the pair — so that
    # a reader can see they share the group.
    assert text.count(ATTEMPT_GROUP) >= 2, (
        f"AC-7: attempt_group {ATTEMPT_GROUP} must render on BOTH rows of "
        f"the BTM+deauth_fallback pair; got {text.count(ATTEMPT_GROUP)} occurrences"
    )
