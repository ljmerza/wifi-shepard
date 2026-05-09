from __future__ import annotations

import pytest

_CONFIG_WITH_CONTROLLERS = """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: secret
home_assistant:
  url: http://example.invalid:8123
  token: dummy-token
  notify_service: dummy
scanner:
  poll_interval_seconds: 60
  window_samples: 5
  log_level: info
  log_format: human
  dry_run: true
detection:
  tx_rate_kbps_max: 12000
  retry_pct_max: 30
  signal_dbm_max: -70
  radios: [ng]
backoff:
  quarantine_after_kicks: 5
allowlist: []
overrides: []
"""

_CONFIG_WITHOUT_CONTROLLERS = """
home_assistant:
  url: http://example.invalid:8123
  token: dummy-token
  notify_service: dummy
scanner:
  poll_interval_seconds: 60
  window_samples: 5
  log_level: info
  log_format: human
  dry_run: true
detection:
  tx_rate_kbps_max: 12000
  retry_pct_max: 30
  signal_dbm_max: -70
  radios: [ng]
backoff:
  quarantine_after_kicks: 5
allowlist: []
overrides: []
"""


def test_daemon_auto_builds_controllers_from_config_when_kwarg_omitted(temp_db_path, tmp_path):
    from wifi_shepard.controllers import UniFiController
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_CONFIG_WITH_CONTROLLERS)

    daemon = build_daemon(config_path=cfg_path, db_path=temp_db_path)

    assert len(daemon.controllers) == 1
    controller = daemon.controllers[0]
    assert isinstance(controller, UniFiController), (
        "auto-build path must use the build_controller factory and yield a real backend"
    )
    assert controller.host == "192.168.1.1"
    assert controller.username == "shepard"
    assert controller.password == "secret"
    assert controller.name == "home"


def test_daemon_raises_when_no_controllers_in_config_and_no_kwarg(temp_db_path, tmp_path):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_CONFIG_WITHOUT_CONTROLLERS)

    with pytest.raises(ValueError, match="no controllers configured"):
        build_daemon(config_path=cfg_path, db_path=temp_db_path)


def test_daemon_kwarg_takes_precedence_over_config_controllers(temp_db_path, tmp_path):
    """Explicit injection wins — used by tests and any in-process embedding."""
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_CONFIG_WITH_CONTROLLERS)

    sentinel = object()
    daemon = build_daemon(config_path=cfg_path, db_path=temp_db_path, controllers=[sentinel])

    assert daemon.controllers == [sentinel], (
        "controllers kwarg must short-circuit the build_controller factory path"
    )
