"""Shared fixtures for the ADR-0014 per-device settings tests (helper module, not collected).

The sample is deliberately *sparse* — no ``backoff:``, ``safety_rails:``, ``reboot:``,
or ``detection.inactivity:`` block — so a surgical per-device write can be told apart
from a full round-trip save that materializes every schema default (AC-2).
"""

from __future__ import annotations

from pathlib import Path

# Already allowlisted in SAMPLE; used to prove a neighbour entry survives an edit.
ALLOWLISTED_MAC = "aa:bb:cc:dd:ee:ff"
# Has an overrides[] row in SAMPLE, including the cosmetic `name:` label (AC-8).
OVERRIDE_MAC = "dc:cc:e6:66:86:2b"
# Absent from SAMPLE entirely — a device being configured before it is ever seen.
NEW_MAC = "34:ea:e7:aa:bb:cc"

SAMPLE = """\
# operator hand comment — must survive a per-device save
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: ${UNIFI_PASSWORD}
scanner:
  poll_interval_seconds: 60
  window_samples: 5
  dry_run: true
detection:
  tx_rate_kbps_max: 12000
  retry_pct_max: 30
  signal_dbm_max: -70
  radios: [ng]
  ap_cu_total_min: 60
allowlist:
  - aa:bb:cc:dd:ee:ff
overrides:
  - mac: dc:cc:e6:66:86:2b
    name: "leonardo s22"
    tx_rate_kbps_max: 6000
    signal_dbm_max: -65
"""

# Sections absent from SAMPLE. A surgical write must not conjure them into existence.
ABSENT_SECTIONS = ("backoff:", "safety_rails:", "reboot:", "inactivity:")


def write_device_sample(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(SAMPLE)
    return cfg


def device_client(tmp_path: Path, cfg: Path):
    """A TestClient over the sidecar with no DB (reads degrade to empty) and a
    writable config at ``cfg``."""
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    return TestClient(create_app(db_path=tmp_path / "absent.db", config_path=cfg))
