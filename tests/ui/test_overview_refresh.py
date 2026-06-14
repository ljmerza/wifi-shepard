"""Overview auto-refresh: a <meta http-equiv="refresh"> tag whose interval comes
from WIFI_SHEPARD_UI_REFRESH_SECONDS (default 60s, 0 disables)."""

from __future__ import annotations

from pathlib import Path


def _overview_html(db_path: Path, monkeypatch, env_value: str | None) -> str:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    if env_value is None:
        monkeypatch.delenv("WIFI_SHEPARD_UI_REFRESH_SECONDS", raising=False)
    else:
        monkeypatch.setenv("WIFI_SHEPARD_UI_REFRESH_SECONDS", env_value)
    # The interval is snapshotted at create_app() time, so build the app after
    # setting the env (mirrors a container restart picking up the value).
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    return response.text


def test_overview_default_refresh_is_60s(seeded_db: Path, monkeypatch) -> None:
    html = _overview_html(seeded_db, monkeypatch, None)
    assert '<meta http-equiv="refresh" content="60">' in html


def test_overview_refresh_interval_from_env(seeded_db: Path, monkeypatch) -> None:
    assert '<meta http-equiv="refresh" content="120">' in _overview_html(
        seeded_db, monkeypatch, "120"
    )


def test_overview_refresh_disabled_when_zero(seeded_db: Path, monkeypatch) -> None:
    assert 'http-equiv="refresh"' not in _overview_html(seeded_db, monkeypatch, "0")


def test_overview_refresh_bad_value_falls_back_to_default(seeded_db: Path, monkeypatch) -> None:
    assert '<meta http-equiv="refresh" content="60">' in _overview_html(
        seeded_db, monkeypatch, "not-a-number"
    )
