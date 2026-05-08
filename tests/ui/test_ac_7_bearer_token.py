"""AC-7: When WIFI_SHEPARD_UI_TOKEN is set, all routes require a matching
Authorization: Bearer header; when unset, no auth is enforced."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_ac_7_token_unset_allows_all_requests(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)

    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/devices").status_code == 200


def test_ac_7_token_set_blocks_unauthenticated_and_wrong_token(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIFI_SHEPARD_UI_TOKEN", "s3cret")

    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        # No header
        assert client.get("/").status_code == 401
        # Wrong scheme
        assert client.get("/", headers={"Authorization": "Basic s3cret"}).status_code == 401
        # Right token
        assert client.get("/", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        # Wrong token
        assert client.get("/", headers={"Authorization": "Bearer nope"}).status_code == 401
