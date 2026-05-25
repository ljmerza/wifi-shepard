"""ADR-0006 AC-12: SIGHUP reload applies new schedule/cooldown in place while
in-memory cooldown state (last-reboot timestamps) is NOT purged.

Mirrors the ADR-0004 AC-8 posture (update_config rewires thresholds but keeps
in-flight rate-limit state) — here for the reboot scheduler's cooldown.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeHARegistry, FakeRebooter
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.reboot.ha_resolver import HAEntity
from wifi_shepard.reboot.scheduler import RebootScheduler

MAC = "08:f9:e0:ba:c4:84"


def _config(*, schedule: str, per_device_seconds: int) -> object:
    return build_config(
        reboot=dict(
            enabled=True,
            dry_run=False,
            eligible=[MAC],
            proactive=dict(enabled=True, schedule=schedule),
            cooldown=dict(per_device_seconds=per_device_seconds, max_per_device_per_day=100),
        )
    )


@pytest.mark.asyncio
async def test_ac_12_sighup_updates_schedule_cooldown_preserving_state(temp_db_path, caplog) -> None:
    clock = [100.0]
    registry = FakeHARegistry(
        entities_by_mac={MAC: [HAEntity("button.x", "button", "restart")]}
    )
    db = Database(temp_db_path)
    await db.connect()
    try:
        scheduler = RebootScheduler(
            config=_config(schedule="03:30", per_device_seconds=3600),
            registry=registry,
            rebooter=FakeRebooter(),
            db=db,
            now_fn=lambda: clock[0],
        )

        await scheduler.attempt(MAC)  # fires at t=100, records last-reboot
        assert len(scheduler.rebooter.calls) == 1

        # SIGHUP-style reload: new schedule + a different cooldown window.
        scheduler.update_config(_config(schedule="04:00", per_device_seconds=7200))

        # New values are active.
        assert scheduler.config.reboot.proactive.schedule == "04:00", (
            "AC-12: reload must apply the new schedule"
        )
        assert scheduler.cooldown.per_device_seconds == 7200, (
            "AC-12: reload must apply the new cooldown window"
        )
        # In-flight cooldown state (last-reboot timestamp) is NOT purged.
        assert scheduler.cooldown._last_reboot_at.get(MAC) == 100.0, (
            "AC-12: reload must not purge in-memory last-reboot timestamps"
        )

        # Behavioral proof: 30s after the prior reboot, still within the new
        # window, the next attempt is deferred (state carried over the reload).
        clock[0] = 130.0
        with caplog.at_level(logging.INFO, logger="wifi_shepard.reboot"):
            await scheduler.attempt(MAC)
        assert len(scheduler.rebooter.calls) == 1, (
            "AC-12: preserved cooldown must still gate the next reboot post-reload"
        )
        deferred = [r for r in caplog.records if r.getMessage() == "reboot_deferred"]
        assert any(getattr(r, "reason", None) == "cooldown" for r in deferred), (
            "AC-12: the post-reload attempt must defer with reason='cooldown'"
        )
    finally:
        await db.close()
