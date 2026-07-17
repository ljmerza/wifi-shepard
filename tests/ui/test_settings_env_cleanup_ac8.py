"""ADR-0013 AC-8: non-secret connection fields move into config.yaml (editable in the
UI); only secrets remain env vars; the parallel WIFI_SHEPARD_UI_ALLOWLIST env is gone
and the UI reads the authoritative allowlist from config.yaml.
"""

from __future__ import annotations

from pathlib import Path

from wifi_shepard_ui import config_io

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_env_example_has_only_secrets() -> None:
    text = (REPO_ROOT / "env.example").read_text()
    # Secrets remain.
    assert "UNIFI_PASSWORD=" in text
    assert "HA_TOKEN=" in text
    assert "PIHOLE_PASSWORD=" in text
    # Non-secret connection vars are gone (now literals in config.yaml).
    for gone in ("UNIFI_HOST=", "UNIFI_USERNAME=", "UNIFI_SITE=", "UNIFI_PORT="):
        assert gone not in text, f"{gone} should have moved into config.yaml"


def test_config_example_uses_literals_for_nonsecrets_but_env_ref_for_password() -> None:
    text = (REPO_ROOT / "config.example.yaml").read_text()
    assert "password: ${UNIFI_PASSWORD}" in text  # secret stays an env reference
    # Non-secret fields are plain literals, not ${...} references.
    assert "${UNIFI_HOST}" not in text
    assert "${UNIFI_USERNAME}" not in text
    assert "${UNIFI_SITE}" not in text
    assert "host: 192.168.1.1" in text
    assert "username: shepard" in text


def test_no_ui_allowlist_env_in_source() -> None:
    src = REPO_ROOT / "src" / "wifi_shepard_ui"
    for py in src.rglob("*.py"):
        assert "WIFI_SHEPARD_UI_ALLOWLIST" not in py.read_text(), (
            f"{py} still references the obsolete WIFI_SHEPARD_UI_ALLOWLIST env"
        )


def test_read_allowlist_from_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "detection: {signal_dbm_max: -70}\n"
        "scanner: {dry_run: true}\n"
        "allowlist:\n  - AA:BB:CC:DD:EE:FF\n  - 11:22:33:44:55:66\n"
    )
    got = config_io.read_allowlist(cfg)
    # Lowercased for the UI's case-insensitive match.
    assert got == {"aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"}


def test_read_allowlist_missing_file_is_empty(tmp_path: Path) -> None:
    assert config_io.read_allowlist(tmp_path / "nope.yaml") == set()


def test_devices_page_marks_allowlisted_from_config(seeded_db: Path, tmp_path: Path, monkeypatch):
    # The seeded DB has MAC_A = AA:BB:CC:DD:EE:FF; an allowlist entry in config.yaml
    # must drive the "allowlisted" flag (no env involved).
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "detection: {signal_dbm_max: -70}\nscanner: {dry_run: true}\n"
        "allowlist:\n  - aa:bb:cc:dd:ee:ff\n"
    )
    client = TestClient(create_app(db_path=seeded_db, config_path=cfg))
    r = client.get("/devices")
    assert r.status_code == 200
    assert "aa:bb:cc:dd:ee:ff" in r.text.lower()
