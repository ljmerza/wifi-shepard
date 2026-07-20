"""ADR-0013 AC-9: the settings save route is auth-gated (401 without a valid bearer
token when one is set) and header-carried + JSON-only (CSRF-safe); every OTHER route
stays GET-only (the read-only fence is amended to a single-path allowlist, not lifted).
"""

from __future__ import annotations

from pathlib import Path

from tests.ui._settings_data import payload_from, write_sample


def _make_app(tmp_path: Path, cfg: Path):
    from wifi_shepard_ui.app import create_app

    return create_app(db_path=tmp_path / "absent.db", config_path=cfg)


def test_ac_9_token_set_blocks_unauthenticated_save(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WIFI_SHEPARD_UI_TOKEN", "s3cret")
    monkeypatch.setenv("UNIFI_PASSWORD", "x")
    monkeypatch.setenv("HA_TOKEN", "y")
    from fastapi.testclient import TestClient

    cfg = write_sample(tmp_path)
    before = cfg.read_bytes()
    client = TestClient(_make_app(tmp_path, cfg))
    payload = payload_from(cfg)
    payload["scalars"]["detection.signal_dbm_max"] = "-75"

    # No token -> 401, file untouched.
    r = client.post("/settings", json=payload)
    assert r.status_code == 401
    assert cfg.read_bytes() == before

    # Wrong token -> 401.
    r = client.post("/settings", json=payload, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert cfg.read_bytes() == before

    # Right token (header-carried) -> save succeeds.
    r = client.post("/settings", json=payload, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert cfg.read_bytes() != before


def test_ac_9_non_json_body_rejected(tmp_path: Path, monkeypatch) -> None:
    # A form-encoded (browser-forgeable "simple") POST is rejected — the save is
    # JSON-only, which also forces a CORS preflight cross-site, so it can't be forged.
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    from fastapi.testclient import TestClient

    cfg = write_sample(tmp_path)
    before = cfg.read_bytes()
    client = TestClient(_make_app(tmp_path, cfg))
    r = client.post("/settings", data={"detection.signal_dbm_max": "-99"})
    assert r.status_code == 415
    assert cfg.read_bytes() == before

    # text/plain is the other body a cross-site <form> can emit without a preflight.
    # It parses as JSON, so the content type is what has to be refused (ADR-0014).
    r = client.post(
        "/settings",
        content='{"scalars": {}}',
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 415
    assert cfg.read_bytes() == before


def test_ac_9_only_settings_route_allows_writes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    cfg = write_sample(tmp_path)
    app = _make_app(tmp_path, cfg)

    # ADR-0014 widened the fence from one path to two: the settings save and the
    # per-device save. Everything else stays GET-only.
    allowed = {"/settings", "/devices/{mac}/settings"}
    write_methods = {"POST", "PUT", "DELETE", "PATCH"}
    for route in app.routes:
        methods = {m.upper() for m in (getattr(route, "methods", None) or set())}
        if methods & write_methods:
            assert getattr(route, "path", None) in allowed, (
                f"unexpected write route: {getattr(route, 'path', route)} {methods}"
            )


def test_ac_9_read_routes_reject_post(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    from fastapi.testclient import TestClient

    cfg = write_sample(tmp_path)
    client = TestClient(_make_app(tmp_path, cfg))
    # GET-only routes reject a POST with 405 Method Not Allowed.
    assert client.post("/devices").status_code == 405
    assert client.post("/dns").status_code == 405
