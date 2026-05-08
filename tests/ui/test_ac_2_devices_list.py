"""AC-2: GET /devices returns a sortable HTML table per MAC with kick count,
last-bad-window timestamp, current backoff state, and allowlist flag."""

from __future__ import annotations

from pathlib import Path


def test_ac_2_devices_list_html_with_kick_counts(seeded_db: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/devices")

    assert response.status_code == 200
    text = response.text
    lower = text.lower()

    # Both seeded MACs appear
    assert "aa:bb:cc:dd:ee:ff" in lower
    assert "11:22:33:44:55:66" in lower

    # Required columns surfaced
    assert "kick" in lower, "table must surface kick count"
    assert "state" in lower, "table must surface current backoff state"
    assert "allowlist" in lower, "table must surface allowlist flag"

    # MAC_A had 2 kick_events rows; the count should be visible somewhere
    # (rendered as digits in the row for that MAC)
    assert ">2<" in text or " 2 " in text or "\t2\t" in text, (
        "MAC_A's kick count of 2 must be rendered in the row"
    )

    # Sortable: header must include a sort affordance (link/button/header
    # element) for at least the kick-count column. We accept any of these
    # idioms for the AC.
    assert any(
        marker in lower
        for marker in ["sort=", "?sort", "data-sort", "sortable"]
    ), "devices table must expose a sort affordance"
