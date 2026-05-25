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
from wifi_shepard.config import build_config, load_config_from_path
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


def _reboot(**extra) -> dict:
    base = dict(enabled=True, eligible=[MAC])
    base.update(extra)
    return base


def test_ac_11_non_hhmm_schedule_rejected() -> None:
    with pytest.raises(ValueError, match="schedule"):
        build_config(reboot=_reboot(proactive=dict(enabled=True, schedule="25:99")))


def test_ac_11_negative_cooldown_rejected() -> None:
    with pytest.raises(ValueError, match="per_device_seconds"):
        build_config(reboot=_reboot(cooldown=dict(per_device_seconds=-1)))


def test_ac_11_bool_cooldown_rejected() -> None:
    # YAML parses `yes`/`no` as Python bool (an int subclass). Reject explicitly,
    # mirroring ADR-0004 AC-7's bool-as-int guard.
    with pytest.raises(ValueError, match="max_per_device_per_day"):
        build_config(reboot=_reboot(cooldown=dict(max_per_device_per_day=True)))


def test_ac_11_unknown_probe_method_rejected() -> None:
    with pytest.raises(ValueError, match="method"):
        build_config(reboot=_reboot(reactive=dict(probe=dict(method="telepathy"))))


def test_ac_11_yaml_bad_schedule_fails_closed(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'reboot:\n  enabled: true\n  eligible:\n    - {MAC}\n'
        f'  proactive:\n    enabled: true\n    schedule: "bedtime"\n'
    )
    with pytest.raises(ValueError, match="schedule"):
        load_config_from_path(cfg)
