"""ADR-0013 AC-4 / AC-5: POST /settings validates with the daemon's own parser and
writes a comment/placeholder-preserving atomic round-trip; invalid input is rejected
and the file is left byte-for-byte unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.ui._settings_data import payload_from, write_sample
from wifi_shepard.config import load_config_from_path


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    # For the daemon's re-parse of the written file (interpolation).
    monkeypatch.setenv("UNIFI_PASSWORD", "x")
    monkeypatch.setenv("HA_TOKEN", "y")


def _client(tmp_path: Path, cfg: Path):
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    return TestClient(create_app(db_path=tmp_path / "absent.db", config_path=cfg))


def test_ac_5_valid_save_preserves_comment_placeholder_and_applies_change(tmp_path: Path) -> None:
    cfg = write_sample(tmp_path)
    payload = payload_from(cfg)
    payload["scalars"]["detection.signal_dbm_max"] = "-75"  # -70 -> -75

    r = _client(tmp_path, cfg).post("/settings", json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True

    text = cfg.read_text()
    assert "# operator hand comment" in text  # comment preserved
    assert "${UNIFI_PASSWORD}" in text  # secret placeholder preserved (not resolved)
    # The daemon parses it and sees the change.
    cfg_obj = load_config_from_path(cfg)
    assert cfg_obj.detection.signal_dbm_max == -75
    assert cfg_obj.home_assistant.token == "y"  # placeholder resolved only in the daemon


def test_ac_4_disable_a_criterion_writes_null(tmp_path: Path) -> None:
    cfg = write_sample(tmp_path)
    payload = payload_from(cfg)
    payload["scalars"]["detection.retry_pct_max"] = ""  # clear -> disable (ADR-0009)

    r = _client(tmp_path, cfg).post("/settings", json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert load_config_from_path(cfg).detection.retry_pct_max is None


def test_ac_4_invalid_number_rejected_file_unchanged(tmp_path: Path) -> None:
    cfg = write_sample(tmp_path)
    before = cfg.read_bytes()
    payload = payload_from(cfg)
    payload["scalars"]["detection.signal_dbm_max"] = "not-a-number"

    r = _client(tmp_path, cfg).post("/settings", json=payload)
    assert r.status_code == 400
    assert "whole number" in r.json()["error"]
    assert cfg.read_bytes() == before  # untouched


def test_ac_4_all_null_detection_rejected_by_daemon_validator(tmp_path: Path) -> None:
    cfg = write_sample(tmp_path)
    before = cfg.read_bytes()
    payload = payload_from(cfg)
    for k in ("tx_rate_kbps_max", "retry_pct_max", "signal_dbm_max"):
        payload["scalars"][f"detection.{k}"] = ""

    r = _client(tmp_path, cfg).post("/settings", json=payload)
    assert r.status_code == 400
    assert "at least one client criterion" in r.json()["error"]  # the daemon's own message
    assert cfg.read_bytes() == before


def test_ac_5_enabling_optional_section_and_reparse(tmp_path: Path) -> None:
    cfg = write_sample(tmp_path)
    payload = payload_from(cfg)
    # Turn quiet_hours ON via its explicit toggle; its defaulted times apply.
    payload["section_enabled"]["quiet_hours"] = True

    r = _client(tmp_path, cfg).post("/settings", json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True
    qh = load_config_from_path(cfg).quiet_hours
    assert qh is not None and qh.start == "23:00" and qh.end == "07:00"


def test_ac_5_roundtrip_no_change_keeps_off_sections_off(tmp_path: Path) -> None:
    # Saving an unchanged config must not spuriously enable quiet_hours / dns_thrash.
    cfg = write_sample(tmp_path)
    r = _client(tmp_path, cfg).post("/settings", json=payload_from(cfg))
    assert r.status_code == 200
    cfg_obj = load_config_from_path(cfg)
    assert cfg_obj.quiet_hours is None
    assert cfg_obj.detection.dns_thrash is None
    assert cfg_obj.dns_sources == ()


def test_quiet_hours_override_thresholds_roundtrip(tmp_path: Path) -> None:
    # The quiet_hours override thresholds are flat on the dataclass but nested under
    # `override_threshold:` in the YAML — the UI must read and write them there.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "scanner: {dry_run: true}\n"
        "detection: {signal_dbm_max: -70}\n"
        "quiet_hours:\n"
        "  start: '23:00'\n  end: '07:00'\n  timezone: America/Chicago\n"
        "  override_threshold: {tx_rate_kbps_max: 2000, retry_pct_max: 50}\n"
    )
    client = _client(tmp_path, cfg)

    # Read: the nested values are pre-filled on the flat form fields.
    body = client.get("/settings").text
    assert 'value="2000"' in body and 'value="50"' in body

    # Save (change 2000 -> 1500) writes back under override_threshold and parses.
    payload = payload_from(cfg)
    payload["scalars"]["quiet_hours.tx_rate_kbps_max"] = "1500"
    r = client.post("/settings", json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True
    qh = load_config_from_path(cfg).quiet_hours
    assert qh.tx_rate_kbps_max == 1500 and qh.retry_pct_max == 50
    assert "override_threshold:" in cfg.read_text()  # written in the right place


def test_fresh_deploy_creates_file(tmp_path: Path) -> None:
    absent = tmp_path / "config.yaml"
    # Minimal valid config: one controller + one detection criterion + HA off.
    payload = {
        "scalars": {
            "scanner.dry_run": True,
            "scanner.kick_mechanism": "deauth",
            "detection.signal_dbm_max": "-70",
            "detection.tx_rate_kbps_max": "",
            "detection.retry_pct_max": "",
            "detection.ap_cu_total_min": "0",
        },
        "scalar_lists": {"detection.radios": ["ng"], "allowlist": []},
        "object_lists": {
            "controllers": [
                {
                    "type": "unifi",
                    "name": "home",
                    "host": "10.0.0.1",
                    "username": "u",
                    "password": "UNIFI_PASSWORD",
                    "verify_ssl": False,
                }
            ]
        },
        "section_enabled": {"quiet_hours": False, "home_assistant": False, "dns_sources": False},
    }
    os.environ["UNIFI_PASSWORD"] = "x"
    r = _client(tmp_path, absent).post("/settings", json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert absent.exists()
    assert load_config_from_path(absent).detection.signal_dbm_max == -70
