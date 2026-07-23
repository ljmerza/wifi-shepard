"""ADR-0015 AC-10: partial-deploy safe — a DB without the rationale column.

A new sidecar image against an old daemon DB (kick_events with no `rationale`
column) must render HTTP 200, not 500. The column is absent from the required-
columns fence, and device_history exposes rationale=None for such rows.
"""

from __future__ import annotations

from tests.ui.conftest import MAC_A


def test_ac_10_rationale_not_in_required_columns() -> None:
    from wifi_shepard_ui import views

    assert "rationale" not in views._REQUIRED_KICK_EVENTS_COLUMNS, (
        "AC-10: rationale must stay OUT of the required-columns fence so an old "
        "daemon DB (no rationale column) does not hard-fail the sidecar at startup"
    )


def test_ac_10_device_page_200_and_rationale_none_without_column(seeded_db) -> None:
    """seeded_db uses the shared DAEMON_SCHEMA, which has NO rationale column."""
    import sqlite3

    from fastapi.testclient import TestClient

    from wifi_shepard_ui import views
    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get(f"/devices/{MAC_A}")
    assert response.status_code == 200, (
        "AC-10: the device page must render 200 against a DB missing the rationale column"
    )

    conn = sqlite3.connect(seeded_db)
    try:
        events = views.device_history(conn, mac=MAC_A)
    finally:
        conn.close()
    assert events, "precondition: MAC_A has seeded history"
    for ev in events:
        assert ev.rationale is None, (
            "AC-10: every HistoryEvent must expose rationale=None when the column is absent; "
            f"got {ev.rationale!r}"
        )
