"""AC-3: GET /devices/{mac} returns a chronological timeline merging
client_samples and kick_events, with dry-run rows visually distinguished."""

from __future__ import annotations

from pathlib import Path


def test_ac_3_device_history_chronological(seeded_db: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/devices/AA:BB:CC:DD:EE:FF")

    assert response.status_code == 200
    text = response.text
    lower = text.lower()

    # Dry-run rows must be visually distinguished from real kicks
    assert "dry-run kick" in lower, "dry-run kick rows must be labeled distinctly from real kicks"
    # Sample data (signal/RSSI) from client_samples must also appear
    assert any(marker in lower for marker in ["signal", "rssi", "dbm"]), (
        "history must include client_samples context (signal/RSSI)"
    )

    # Specific seeded values: signals were -72/-75/-78 dBm. At least one must
    # render — proves the route actually reads client_samples, not just a stub.
    assert any(s in text for s in ["-72", "-75", "-78"]), (
        "at least one seeded signal value (-72/-75/-78) must render"
    )

    # Newest-first ordering: real kick (ts-90, newer) renders above dry-run
    # kick (ts-150, older). This proves device_history() actually sorts ts
    # DESC; a no-op sorter would not satisfy this.
    real_pos = lower.find(">kick<")  # the bare-"kick" cell is the real kick
    dry_pos = lower.find("dry-run kick")
    assert real_pos > 0 and dry_pos > 0
    assert real_pos < dry_pos, (
        "newest-first: real kick (newer) must render above dry-run kick (older); "
        f"got real@{real_pos} dry@{dry_pos}"
    )
