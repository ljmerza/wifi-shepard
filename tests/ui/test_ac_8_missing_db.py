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

    text = response.text
    lower = text.lower()
    # Must render the explicit empty-state paragraph (.empty CSS class)
    # so a future template change that drops the wording is caught.
    assert 'class="empty"' in text, (
        "empty-state page must render the .empty paragraph, not a fallback"
    )
    # And the human-readable copy that explains what's missing.
    assert any(marker in lower for marker in ["no ap saturation", "no clients", "no data"]), (
        "empty-state copy must explain why no data is shown"
    )
