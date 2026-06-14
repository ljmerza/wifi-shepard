"""Devices page surfaces the controller-reported device name alongside the MAC."""

from __future__ import annotations

from pathlib import Path


def test_devices_list_shows_device_name(seeded_db: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/devices")

    assert response.status_code == 200
    lower = response.text.lower()
    # Name column header + the seeded device name render.
    assert "name" in lower, "devices table must expose a Name column"
    assert "wled-kitchen" in lower, "MAC_A's reported name must render"


def test_devices_sort_by_name_orders_unnamed_first(seeded_db: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/devices?sort=name")

    assert response.status_code == 200
    lower = response.text.lower()
    # MAC_B has no name ("" sorts first); MAC_A is "wled-kitchen".
    pos_b = lower.find("11:22:33:44:55:66")
    pos_a = lower.find("aa:bb:cc:dd:ee:ff")
    assert pos_b > 0 and pos_a > 0
    assert pos_b < pos_a, "sort=name must list the unnamed device before the named one"
