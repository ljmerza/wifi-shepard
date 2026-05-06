from __future__ import annotations

import asyncio
import os
import signal

import pytest

from tests.conftest import FakeController


def _minimal_config_yaml(poll_interval: int = 10) -> str:
    return f"""
controllers: []
home_assistant:
  url: http://example.invalid:8123
  token: dummy-token
  notify_service: dummy
scanner:
  poll_interval_seconds: {poll_interval}
  window_samples: 5
  log_level: info
  log_format: human
  dry_run: true
detection:
  tx_rate_kbps_max: 12000
  retry_pct_max: 30
  signal_dbm_max: -70
  radios: [ng]
  ap_cu_total_min: 60
backoff:
  cooldowns_seconds: [300]
  max_kicks_per_hour: 3
  max_kicks_per_day: 10
  quarantine_after_kicks: 5
allowlist: []
overrides: []
"""


@pytest.mark.asyncio
async def test_ac_8_sigterm_clean_shutdown(temp_db_path, tmp_path):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_minimal_config_yaml(poll_interval=10))

    fake = FakeController()
    daemon = build_daemon(config_path=cfg_path, db_path=temp_db_path, controllers=[fake])
    daemon_task = asyncio.create_task(daemon.run())

    await asyncio.wait_for(daemon.first_cycle_started.wait(), timeout=5)

    os.kill(os.getpid(), signal.SIGTERM)
    exit_code = await asyncio.wait_for(daemon_task, timeout=5)

    assert exit_code == 0, f"clean SIGTERM must exit code 0, got {exit_code}"
    assert daemon.db.closed, "database must close on shutdown"
    assert fake.closed, "controller must close on shutdown"
