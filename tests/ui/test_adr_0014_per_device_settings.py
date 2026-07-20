"""ADR-0014: per-device settings editing — a device-centric write surface over the
same config.yaml.

One test per acceptance criterion. The route under test is
``POST /devices/{mac}/settings`` with partial-payload semantics (an absent key leaves
that setting unchanged).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._device_data import (
    ALLOWLISTED_MAC,
    NEW_MAC,
    device_client,
    write_device_sample,
)
from wifi_shepard.config import load_config_from_path


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    # The daemon's re-parse of the written file interpolates ${UNIFI_PASSWORD}.
    monkeypatch.setenv("UNIFI_PASSWORD", "x")


def test_ac_1_allowlist_toggle_adds_removes_and_is_idempotent(tmp_path: Path) -> None:
    cfg = write_device_sample(tmp_path)
    client = device_client(tmp_path, cfg)

    # Add — the MAC lands in allowlist:, normalized to lowercase.
    r = client.post(f"/devices/{NEW_MAC.upper()}/settings", json={"allowlisted": True})
    assert r.status_code == 200, r.text
    assert NEW_MAC in load_config_from_path(cfg).allowlist

    # Idempotent — a repeat add leaves the file byte-for-byte unchanged, no duplicate.
    after_add = cfg.read_bytes()
    r = client.post(f"/devices/{NEW_MAC}/settings", json={"allowlisted": True})
    assert r.status_code == 200, r.text
    assert cfg.read_bytes() == after_add
    assert list(load_config_from_path(cfg).allowlist).count(NEW_MAC) == 1

    # Remove — only the targeted MAC goes; the pre-existing entry survives.
    r = client.post(f"/devices/{NEW_MAC}/settings", json={"allowlisted": False})
    assert r.status_code == 200, r.text
    allowlist = load_config_from_path(cfg).allowlist
    assert NEW_MAC not in allowlist
    assert ALLOWLISTED_MAC in allowlist
