"""ADR-0011 amendment: the Settings UI exposes a password field per Pi-hole instance
(env-var name, like every secret), read and round-tripped alongside the URL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wifi_shepard.config import load_config_from_path


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    monkeypatch.setenv("PIHOLE_GYM", "gympw")
    monkeypatch.setenv("PIHOLE_BONUS", "bonuspw")


def _client(tmp_path: Path, cfg: Path):
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    return TestClient(create_app(db_path=tmp_path / "absent.db", config_path=cfg))


def _cfg_with_per_instance_passwords(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "scanner: {dry_run: true}\n"
        "detection:\n  signal_dbm_max: -70\n  dns_thrash: {same_domain_queries_max: 200}\n"
        "dns_sources:\n"
        "  - type: pihole\n"
        "    instances:\n"
        "      - url: http://192.168.1.186\n        password: ${PIHOLE_GYM}\n"
        "      - url: http://192.168.1.189\n        password: ${PIHOLE_BONUS}\n"
    )
    return cfg


def test_get_renders_per_instance_password_as_env_name(tmp_path: Path) -> None:
    cfg = _cfg_with_per_instance_passwords(tmp_path)
    body = _client(tmp_path, cfg).get("/settings").text
    # Env NAMES shown, not the ${...} placeholder or the resolved secret.
    assert 'value="PIHOLE_GYM"' in body
    assert 'value="PIHOLE_BONUS"' in body
    assert "${PIHOLE_GYM}" not in body
    assert "gympw" not in body and "bonuspw" not in body
    # The per-instance password field's help text is present (its unique example
    # env name; apostrophes elsewhere get HTML-escaped).
    assert "PIHOLE_GYM_PASSWORD" in body


def test_save_roundtrips_per_instance_passwords(tmp_path: Path) -> None:
    from wifi_shepard_ui import config_io

    cfg = _cfg_with_per_instance_passwords(tmp_path)
    model = config_io.read_form_model(cfg)
    payload = {k: model[k] for k in ("scalars", "scalar_lists", "object_lists", "section_enabled")}

    r = _client(tmp_path, cfg).post("/settings", json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True

    text = cfg.read_text()
    assert "${PIHOLE_GYM}" in text and "${PIHOLE_BONUS}" in text
    parsed = load_config_from_path(cfg)
    insts = parsed.dns_sources[0].instances
    assert parsed.dns_sources[0].password is None  # no shared default
    assert insts[0].password == "gympw" and insts[1].password == "bonuspw"
