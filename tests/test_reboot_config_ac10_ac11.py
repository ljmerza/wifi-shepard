"""ADR-0006 AC-10 + AC-11: default-off behavior and fail-closed config.

AC-10 — no reboot: block (or reboot.enabled:false): the daemon starts no
  scheduler task, writes no reboot_events rows, and behaves as the pre-reboot
  baseline. With proactive enabled and a backend wired, a scheduler is built.

AC-11 — an invalid reboot: shape (non-HH:MM schedule, negative cooldown.*,
  bool cooldown, unknown probe.method) raises a ValueError naming the field and
  exits — fail-closed, matching ADR-0004 AC-7 / ADR-0005 AC-7.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import aiosqlite
import pytest

from tests.conftest import FakeController, FakeHARegistry, FakeRebooter
from wifi_shepard.reboot.ha_resolver import HAEntity

MAC = "08:f9:e0:ba:c4:84"

_BASE_YAML = """
controllers: []
scanner:
  poll_interval_seconds: 10
  window_samples: 5
  dry_run: true
detection:
  tx_rate_kbps_max: 12000
  retry_pct_max: 30
  signal_dbm_max: -70
  radios: [ng]
allowlist: []
overrides: []
"""

_REBOOT_ON_YAML = f"""
reboot:
  enabled: true
  dry_run: true
  eligible:
    - {MAC}
  proactive:
    enabled: true
    schedule: "03:30"
"""


@pytest.mark.asyncio
async def test_ac_10_no_reboot_block_starts_no_scheduler(temp_db_path, tmp_path) -> None:
    from wifi_shepard.main import build_daemon

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE_YAML)
    daemon = build_daemon(
        config_path=cfg, db_path=temp_db_path, controllers=[FakeController()]
    )

    assert daemon._scheduler is None, "AC-10: no reboot block must build no scheduler"

    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.first_cycle_started.wait(), timeout=5)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, timeout=5) == 0

    async with aiosqlite.connect(temp_db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM reboot_events")
        (count,) = await cur.fetchone()
    assert count == 0, f"AC-10: baseline must write no reboot_events rows; got {count}"


@pytest.mark.asyncio
async def test_ac_10_proactive_enabled_with_backend_builds_scheduler(
    temp_db_path, tmp_path
) -> None:
    from wifi_shepard.main import build_daemon

    cfg = tmp_path / "config.yaml"
    cfg.write_text(_BASE_YAML + _REBOOT_ON_YAML)
    registry = FakeHARegistry(
        entities_by_mac={MAC: [HAEntity("button.x", "button", "restart")]}
    )
    daemon = build_daemon(
        config_path=cfg,
        db_path=temp_db_path,
        controllers=[FakeController()],
        rebooter=FakeRebooter(),
        registry=registry,
    )
    assert daemon._scheduler is not None, (
        "AC-10: proactive.enabled with a backend wired must build a scheduler"
    )
