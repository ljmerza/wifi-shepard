"""AC-8: When /data/state.db is absent (fresh deploy), GET / renders an
empty-state page with HTTP 200 instead of crashing."""

from __future__ import annotations

from pathlib import Path


def test_ac_8_missing_db_renders_empty_state(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    nonexistent = tmp_path / "no_such.db"
    assert not nonexistent.exists(), "precondition: db file must not exist"

    app = create_app(db_path=nonexistent)
    with TestClient(app) as client:
        response = client.get("/")
        devices_response = client.get("/devices")

    assert response.status_code == 200
    assert devices_response.status_code == 200

    lower = response.text.lower()
    # Must signal the empty state with a no-data hint, not crash or
    # show a stack trace. We accept any of these idioms.
    assert any(marker in lower for marker in ["no data", "empty", "0 ", "nothing", "no clients"]), (
        "empty-state page must clearly indicate no data is available yet"
    )
