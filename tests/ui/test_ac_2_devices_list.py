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
        sorted_response = client.get("/devices?sort=kicks")

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

    # Specific kick counts: MAC_A has 1 REAL kick (dry-run rows excluded
    # to match overview()'s semantics), MAC_B has 0.
    # Look for the count inside a <td> to avoid false positives elsewhere.
    assert ">1</td>" in text, "MAC_A's real-kick count of 1 must render in a table cell"
    assert ">0</td>" in text, "MAC_B's kick count of 0 must render in a table cell"

    # Sortable: header must include a sort affordance.
    assert any(marker in lower for marker in ["sort=", "?sort", "data-sort", "sortable"]), (
        "devices table must expose a sort affordance"
    )

    # ?sort=kicks must put MAC_A (1 real kick; the dry-run is excluded) above
    # MAC_B (0 kicks). This proves sort_devices() actually reorders rows —
    # without it the test would still pass on a no-op sorter.
    assert sorted_response.status_code == 200
    sorted_text = sorted_response.text.lower()
    pos_a = sorted_text.find("aa:bb:cc:dd:ee:ff")
    pos_b = sorted_text.find("11:22:33:44:55:66")
    assert pos_a > 0 and pos_b > 0
    assert pos_a < pos_b, (
        "?sort=kicks must list MAC_A (1 real kick) before MAC_B (0 kicks); "
        f"got positions {pos_a} vs {pos_b}"
    )
