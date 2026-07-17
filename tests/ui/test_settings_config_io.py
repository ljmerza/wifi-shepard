"""ADR-0013 unit tests for the config read/build/write layer (config_io)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wifi_shepard_ui import config_io


@pytest.mark.parametrize(
    "value,expected",
    [
        ("${UNIFI_PASSWORD}", "UNIFI_PASSWORD"),
        ("  ${HA_TOKEN}  ", "HA_TOKEN"),
        ("literal-secret", ""),  # not a placeholder -> never surfaced
        ("${lower_case}", ""),  # not a valid env name
        ("${BAD-NAME}", ""),
        (None, ""),
        (42, ""),
    ],
)
def test_env_name_extraction(value, expected) -> None:
    assert config_io._env_name(value) == expected


def _base_payload() -> dict:
    return {
        "scalars": {
            "scanner.dry_run": True,
            "scanner.kick_mechanism": "deauth",
            "detection.signal_dbm_max": "-70",
            "detection.tx_rate_kbps_max": "",
            "detection.retry_pct_max": "",
            "detection.ap_cu_total_min": "0",
        },
        "scalar_lists": {"detection.radios": ["ng"]},
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


def test_build_mapping_wraps_secret_and_disables_blank_detection() -> None:
    m = config_io.build_mapping(_base_payload())
    assert m["controllers"][0]["password"] == "${UNIFI_PASSWORD}"
    # blank_writes_null: cleared detection criteria become explicit null (disabled)...
    assert m["detection"]["tx_rate_kbps_max"] is None
    assert m["detection"]["retry_pct_max"] is None
    # ...but the one with a value stays an int.
    assert m["detection"]["signal_dbm_max"] == -70


def test_build_mapping_omits_blank_override_fields() -> None:
    payload = _base_payload()
    payload["object_lists"]["overrides"] = [
        {"mac": "dc:cc:e6:66:86:2b", "tx_rate_kbps_max": "6000", "retry_pct_max": ""}
    ]
    m = config_io.build_mapping(payload)
    row = m["overrides"][0]
    assert row["tx_rate_kbps_max"] == 6000
    # A blank override field is OMITTED (inherit), not written as null.
    assert "retry_pct_max" not in row


def test_build_mapping_bad_env_name_raises() -> None:
    payload = _base_payload()
    payload["object_lists"]["controllers"][0]["password"] = "not a var"
    with pytest.raises(ValueError, match="valid environment variable name"):
        config_io.build_mapping(payload)


def test_build_mapping_optional_section_toggle_off_drops_it() -> None:
    payload = _base_payload()
    payload["scalars"].update(
        {
            "home_assistant.url": "http://ha:8123",
            "home_assistant.token": "HA_TOKEN",
            "home_assistant.notify_service": "phone",
        }
    )
    # Toggle OFF despite the fields being filled -> section dropped.
    payload["section_enabled"]["home_assistant"] = False
    assert "home_assistant" not in config_io.build_mapping(payload)
    # Toggle ON -> section kept.
    payload["section_enabled"]["home_assistant"] = True
    assert config_io.build_mapping(payload)["home_assistant"]["token"] == "${HA_TOKEN}"


def test_write_config_creates_fresh_file_when_absent(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    config_io.write_config(cfg, config_io.build_mapping(_base_payload()))
    assert cfg.exists()
    text = cfg.read_text()
    assert "${UNIFI_PASSWORD}" in text
    assert "signal_dbm_max: -70" in text


def test_write_config_atomic_no_partial_on_reuse(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    config_io.write_config(cfg, config_io.build_mapping(_base_payload()))
    # No leftover temp file beside it.
    assert not (tmp_path / "config.yaml.tmp").exists()
