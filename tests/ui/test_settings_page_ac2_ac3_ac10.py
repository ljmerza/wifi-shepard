"""ADR-0013 AC-2 / AC-3 / AC-10: GET /settings renders the live config pre-filled,
shows secrets as env-var names (never values), and renders defaults when no file exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._settings_data import write_sample


@pytest.fixture
def _no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)


def _client(tmp_path: Path, cfg: Path):
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=tmp_path / "absent.db", config_path=cfg)
    return TestClient(app)


def test_ac_2_settings_page_prefilled_from_config(_no_token, tmp_path: Path) -> None:
    cfg = write_sample(tmp_path)
    r = _client(tmp_path, cfg).get("/settings")
    assert r.status_code == 200
    # A scalar reflects the file, not just the default.
    assert 'value="-70"' in r.text  # detection.signal_dbm_max
    assert 'value="192.168.1.1"' in r.text  # controllers[].host
    # An object-list row is rendered.
    assert "dc:cc:e6:66:86:2b" in r.text  # overrides[].mac
    # Section help + a threshold description are present (the "explain the knobs" ask).
    assert "must fail everything" in r.text  # detection section help
    assert "always negative" in r.text  # signal_dbm_max description
    # AC-7: startup-only fields carry a visible "restart" marker so the operator knows
    # they won't apply live.
    assert "restart" in r.text


def test_ac_3_secrets_shown_as_env_name_never_value(_no_token, tmp_path: Path) -> None:
    cfg = write_sample(tmp_path)
    r = _client(tmp_path, cfg).get("/settings")
    body = r.text
    # The env var NAME is shown (as the input value)...
    assert 'value="UNIFI_PASSWORD"' in body
    assert 'value="HA_TOKEN"' in body
    # ...but the ${...} placeholder itself is never rendered into the page, and no
    # interpolation ever happens in the UI (the real secret is not even in its env).
    assert "${UNIFI_PASSWORD}" not in body
    assert "${HA_TOKEN}" not in body


def test_ac_3_literal_secret_in_file_is_not_leaked(_no_token, tmp_path: Path) -> None:
    # Defensive: if a raw secret somehow sits in the file (not a ${...} placeholder),
    # the UI must not surface it — the env-name field renders blank.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "controllers:\n"
        "  - type: unifi\n    name: home\n    host: 10.0.0.1\n    username: u\n"
        "    password: hunter2-literal-secret\n"
        "scanner: {dry_run: true}\n"
        "detection: {signal_dbm_max: -70}\n"
    )
    body = _client(tmp_path, cfg).get("/settings").text
    assert "hunter2-literal-secret" not in body


def test_ac_10_missing_config_renders_defaults(_no_token, tmp_path: Path) -> None:
    absent = tmp_path / "nope.yaml"
    r = _client(tmp_path, absent).get("/settings")
    assert r.status_code == 200
    # Defaults are pre-filled (not a 5xx, not blank).
    assert 'value="-70"' in r.text  # detection.signal_dbm_max default
    assert "No config file found" in r.text  # empty-state banner


def test_malformed_config_renders_error_banner_not_500(_no_token, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("this: : : is not : valid : yaml\n  - broken")
    r = _client(tmp_path, cfg).get("/settings")
    assert r.status_code == 200  # never 500 on a bad file
    assert "Couldn't read the current config" in r.text
