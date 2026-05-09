from __future__ import annotations

import asyncio
import os
import signal

import pytest


class CountingController:
    """Minimal Controller implementation with login/close counters.

    FakeController in conftest has no login() method, so the daemon's login
    for-loop (main.py:64-68) is never exercised by other tests.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.logged_in = 0
        self.closed = False

    async def list_wireless_clients(self) -> list:
        return []

    async def list_aps(self) -> list:
        return []

    async def get_ap_radio_stats(self, ap_id: str) -> list:
        return []

    async def force_reconnect_client(self, mac: str) -> None:
        pass

    async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None:
        return None

    async def login(self) -> None:
        self.logged_in += 1

    async def close(self) -> None:
        self.closed = True


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
async def test_daemon_logs_in_and_closes_each_controller(temp_db_path, tmp_path):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_minimal_config_yaml(poll_interval=10))

    c1 = CountingController("home")
    c2 = CountingController("garage")
    daemon = build_daemon(config_path=cfg_path, db_path=temp_db_path, controllers=[c1, c2])
    daemon_task = asyncio.create_task(daemon.run())

    await asyncio.wait_for(daemon.first_cycle_started.wait(), timeout=5)

    assert c1.logged_in == 1, "controller 1 must be logged in exactly once before scan"
    assert c2.logged_in == 1, "controller 2 must be logged in exactly once before scan"

    os.kill(os.getpid(), signal.SIGTERM)
    exit_code = await asyncio.wait_for(daemon_task, timeout=5)

    assert exit_code == 0
    assert c1.closed, "controller 1 must close on shutdown"
    assert c2.closed, "controller 2 must close on shutdown"
