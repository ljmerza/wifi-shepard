"""Shared fixtures for the ADR-0013 settings-UI tests (helper module, not collected)."""

from __future__ import annotations

from pathlib import Path

from wifi_shepard_ui import config_io

# A realistic live-shaped config: a controller with an env-ref password, HA on, and
# quiet_hours / dns_thrash / dns_sources OFF (absent). The leading comment must survive
# a UI save (AC-5).
SAMPLE = """\
# operator hand comment — must survive a UI save
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: ${UNIFI_PASSWORD}
    verify_ssl: false
    port: 443
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
backoff:
  cooldowns_seconds: [300, 1800]
  max_kicks_per_hour: 3
  max_kicks_per_day: 10
  quarantine_after_kicks: 5
allowlist:
  - aa:bb:cc:dd:ee:ff
overrides:
  - mac: dc:cc:e6:66:86:2b
    tx_rate_kbps_max: 6000
home_assistant:
  url: http://homeassistant:8123
  token: ${HA_TOKEN}
  notify_service: mobile_app_pixel
"""


def write_sample(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(SAMPLE)
    return cfg


def payload_from(cfg: Path) -> dict:
    """A save payload equivalent to the current file (the shape the page's JS submits)."""
    m = config_io.read_form_model(cfg)
    return {k: m[k] for k in ("scalars", "scalar_lists", "object_lists", "section_enabled")}
