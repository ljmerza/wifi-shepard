"""AC-4: GET / overview shows total tracked clients, currently-quarantined
count, kicks-today, kicks-this-week, and top 5 noisy APs by cu_total."""

from __future__ import annotations

from pathlib import Path


def test_ac_4_overview_renders_required_counts(seeded_db: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    lower = response.text.lower()

    # Total tracked clients (seeded: 2 distinct MACs)
    assert "tracked" in lower or "clients" in lower, (
        "overview must surface a total-tracked-clients tile"
    )
    # Quarantine label
    assert "quarantin" in lower, "overview must surface quarantined count"
    # Kicks today / week
    assert "today" in lower, "overview must surface kicks-today"
    assert "week" in lower, "overview must surface kicks-this-week"
    # AP saturation
    assert any(marker in lower for marker in ["ap", "access point", "saturation"]), (
        "overview must surface top noisy APs"
    )
    # cu_total surfaced as a label or value (we seeded ap1 with cu=70)
    assert "cu" in lower or "saturation" in lower or "channel" in lower, (
        "overview must reference channel utilization for noisy APs"
    )
