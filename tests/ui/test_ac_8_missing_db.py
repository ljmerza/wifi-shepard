"""AC-8: When /data/state.db is absent (fresh deploy), GET / renders an
empty-state page with HTTP 200 instead of crashing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


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
    assert any(
        marker in lower for marker in ["no ap data", "no ap saturation", "no clients", "no data"]
    ), "empty-state copy must explain why no data is shown"


def test_ac_8_missing_tables_renders_empty_state(tmp_path: Path) -> None:
    """An empty-but-existing DB file (daemon mid-startup, schema not yet
    created) must also yield the empty-state page, not a 500."""
    path = tmp_path / "empty.db"
    # Touch a valid but empty SQLite file — no tables.
    sqlite3.connect(path).close()

    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=path)
    with TestClient(app) as client:
        response = client.get("/")
        devices_response = client.get("/devices")

    assert response.status_code == 200
    assert devices_response.status_code == 200
    assert 'class="empty"' in response.text


def test_ac_8_real_operational_error_surfaces_as_500(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for review #2: a non-empty-state OperationalError (e.g.,
    'database is locked') must NOT silently render the empty-state page —
    it must propagate so the operator sees a 500 + a log line, not a lie."""
    import wifi_shepard_ui.views as views_mod

    def _boom(conn, *, now):  # signature compatible with views.overview
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(views_mod, "overview", _boom)

    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    # raise_server_exceptions=False makes TestClient surface the 500 instead
    # of re-raising the underlying OperationalError to the test.
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/")

    assert response.status_code == 500, (
        "non-empty-state OperationalError must surface as 500, "
        "not be swallowed into an empty-state 200"
    )
