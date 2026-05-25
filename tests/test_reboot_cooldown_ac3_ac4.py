"""ADR-0006 AC-3 + AC-4: per-device reboot cooldown and daily cap.

AC-3 — per_device_seconds=3600: a second reboot of the same MAC before
  t+3600 is deferred with reason='cooldown' and retry_after_seconds; no
  Rebooter call, no (fired) reboot_events row.

AC-4 — max_per_device_per_day=4: a 5th reboot of a MAC already rebooted 4
  times in the window is deferred with reason='daily_cap'.

Both reuse the ADR-0004 injected-clock pattern (now_fn returns a mutable holder)
so cooldown windows advance deterministically.
"""

from __future__ import annotations

import logging

import aiosqlite
import pytest

from tests.conftest import FakeHARegistry, FakeRebooter
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.reboot.ha_resolver import HAEntity
from wifi_shepard.reboot.scheduler import RebootScheduler

MAC = "08:f9:e0:ba:c4:84"


def _registry() -> FakeHARegistry:
    return FakeHARegistry(
        entities_by_mac={
            MAC: [HAEntity(entity_id="button.fridge_restart", domain="button", device_class="restart")]
        }
    )


def _scheduler(db, clock, *, per_device_seconds: int, max_per_day: int) -> RebootScheduler:
    config = build_config(
        reboot=dict(
            enabled=True,
            dry_run=False,
            eligible=[MAC],
            proactive=dict(enabled=True, schedule="03:30"),
            cooldown=dict(
                per_device_seconds=per_device_seconds,
                max_per_device_per_day=max_per_day,
            ),
        )
    )
    return RebootScheduler(
        config=config,
        registry=_registry(),
        rebooter=FakeRebooter(),
        db=db,
        now_fn=lambda: clock[0],
    )


async def _fired_rows(db_path) -> list:
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("SELECT mac FROM reboot_events WHERE dry_run = 0 ORDER BY id")
        return await cur.fetchall()


@pytest.mark.asyncio
async def test_ac_3_second_reboot_within_cooldown_is_deferred(temp_db_path, caplog) -> None:
    clock = [100.0]
    db = Database(temp_db_path)
    await db.connect()
    try:
        scheduler = _scheduler(db, clock, per_device_seconds=3600, max_per_day=100)

        await scheduler.attempt(MAC)  # fires at t=100
        assert len(scheduler.rebooter.calls) == 1

        clock[0] = 130.0  # 30s later, well within the 3600s cooldown
        with caplog.at_level(logging.INFO, logger="wifi_shepard.reboot"):
            await scheduler.attempt(MAC)

        # No second Rebooter call.
        assert len(scheduler.rebooter.calls) == 1, (
            f"AC-3: reboot within cooldown must not fire; got {scheduler.rebooter.calls}"
        )

        deferred = [r for r in caplog.records if r.getMessage() == "reboot_deferred"]
        assert len(deferred) == 1, f"AC-3: expected one reboot_deferred line; got {len(deferred)}"
        rec = deferred[0]
        assert getattr(rec, "reason", None) == "cooldown", (
            f"AC-3: reboot_deferred.reason must be 'cooldown'; got {getattr(rec, 'reason', None)!r}"
        )
        retry = getattr(rec, "retry_after_seconds", None)
        assert retry is not None and retry > 0, (
            f"AC-3: reboot_deferred must include retry_after_seconds; got {retry!r}"
        )

        # Only the first (fired) row exists — the deferred attempt writes none.
        assert await _fired_rows(temp_db_path) == [(MAC,)], (
            "AC-3: deferred reboot must not write a fired reboot_events row"
        )
    finally:
        await db.close()
