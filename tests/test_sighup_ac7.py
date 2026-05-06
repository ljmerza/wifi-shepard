from __future__ import annotations

import asyncio
import os
import signal

import pytest


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
async def test_ac_7_sighup_reloads_valid_yaml_keeps_old_on_invalid(temp_db_path, tmp_path):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_minimal_config_yaml(poll_interval=10))

    daemon = build_daemon(config_path=cfg_path, db_path=temp_db_path, controllers=[])
    daemon_task = asyncio.create_task(daemon.run())

    try:
        await asyncio.wait_for(daemon.first_cycle_started.wait(), timeout=5)
        assert daemon.config.scanner.poll_interval_seconds == 10

        cfg_path.write_text(_minimal_config_yaml(poll_interval=20))
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.wait_for(daemon.config_reloaded.wait(), timeout=5)
        daemon.config_reloaded.clear()
        assert daemon.config.scanner.poll_interval_seconds == 20, (
            "valid SIGHUP must apply new config to next scan cycle"
        )

        cfg_path.write_text(": :: not valid :: yaml")
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.sleep(0.2)
        assert not daemon.config_reloaded.is_set(), "config_reloaded must NOT fire on parse failure"
        assert daemon.config.scanner.poll_interval_seconds == 20, (
            "invalid SIGHUP must keep previous config"
        )
    finally:
        daemon.shutdown()
        await asyncio.wait_for(daemon_task, timeout=5)
