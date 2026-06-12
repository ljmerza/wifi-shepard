"""The scan loop must survive transient failures.

A long-running daemon polls a controller over the network; one blip (UniFi
restart, DNS hiccup, SQLite lock) must log `scan_cycle_failed` and resume on
the next cycle, not kill the process — mirroring the reboot-scheduler tick
guard in main._run_scheduler.
"""

from __future__ import annotations

import asyncio


class FlakyController:
    """Raises on the first poll, succeeds afterwards — a transient network blip."""

    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0
        self.second_cycle = asyncio.Event()

    async def list_wireless_clients(self) -> list:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("simulated transient controller failure")
        self.second_cycle.set()
        return []

    async def list_aps(self) -> list:
        return []

    async def get_ap_radio_stats(self, ap_id: str) -> list:
        return []

    async def force_reconnect_client(self, mac: str) -> None:
        return None

    async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None:
        return None

    async def login(self) -> None:
        return None

    async def close(self) -> None:
        return None


_CONFIG = """
controllers: []
scanner:
  poll_interval_seconds: 1
  window_samples: 5
  dry_run: true
"""


async def test_scan_loop_survives_transient_controller_failure(temp_db_path, tmp_path, caplog):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_CONFIG)

    flaky = FlakyController()
    daemon = build_daemon(config_path=cfg_path, db_path=temp_db_path, controllers=[flaky])
    daemon_task = asyncio.create_task(daemon.run())

    # First cycle raises inside run_once; the daemon must absorb it…
    await asyncio.wait_for(daemon.first_cycle_started.wait(), timeout=5)
    assert not daemon_task.done(), "a transient controller failure must not kill the daemon"
    assert any(r.message == "scan_cycle_failed" for r in caplog.records)

    # …and poll the controller again on the next cycle.
    await asyncio.wait_for(flaky.second_cycle.wait(), timeout=10)

    daemon.shutdown()
    exit_code = await asyncio.wait_for(daemon_task, timeout=5)
    assert exit_code == 0, "shutdown after a transient failure must still exit cleanly"
    assert flaky.calls >= 2
