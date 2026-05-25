"""ADR-0006 AC-1 + AC-2: proactive reboot scheduling.

AC-1 — proactive.enabled, schedule "03:30", an eligible MAC with a resolvable
  ADR-0005 target, dry_run=false: when the clock reaches 03:30 the Rebooter is
  invoked exactly once, a reboot_events row (mode='proactive', dry_run=0) is
  written, and a structured reboot_fired line is emitted.

AC-2 — dry_run=true: the schedule firing emits would_reboot per eligible MAC,
  makes NO Rebooter call, and writes a reboot_events row with dry_run=1 (audit
  symmetry with the fired path, matching ADR-0004 AC-6).
"""

from __future__ import annotations

import logging
from datetime import datetime

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
            MAC: [
                HAEntity(entity_id="button.fridge_restart", domain="button", device_class="restart"),
            ]
        }
    )


def _config(*, dry_run: bool) -> object:
    return build_config(
        reboot=dict(
            enabled=True,
            dry_run=dry_run,
            eligible=[MAC],
            proactive=dict(enabled=True, schedule="03:30"),
        )
    )


@pytest.mark.asyncio
async def test_ac_1_proactive_schedule_fires_rebooter_once(temp_db_path, caplog) -> None:
    rebooter = FakeRebooter()
    db = Database(temp_db_path)
    await db.connect()
    try:
        scheduler = RebootScheduler(
            config=_config(dry_run=False),
            registry=_registry(),
            rebooter=rebooter,
            db=db,
        )

        with caplog.at_level(logging.INFO, logger="wifi_shepard.reboot"):
            await scheduler.run_due(datetime(2026, 5, 25, 3, 30))

        # Rebooter invoked exactly once, with the resolved restart-button target.
        assert len(rebooter.calls) == 1, (
            f"AC-1: schedule must invoke the Rebooter once; got {rebooter.calls}"
        )
        assert rebooter.calls[0].entity_id == "button.fridge_restart"

        # A second run at the same 03:30 must NOT fire again (exactly once per day).
        await scheduler.run_due(datetime(2026, 5, 25, 3, 30))
        assert len(rebooter.calls) == 1, (
            f"AC-1: schedule must fire exactly once per day; got {rebooter.calls}"
        )

        # reboot_events row: mode='proactive', not a dry run.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac, mode, dry_run FROM reboot_events ORDER BY id"
            )
            rows = await cur.fetchall()
        assert rows == [(MAC, "proactive", 0)], (
            f"AC-1: one proactive fired reboot_events row expected; got {rows}"
        )

        # Structured log line.
        fired = [r for r in caplog.records if r.getMessage() == "reboot_fired"]
        assert any(getattr(r, "mac", None) == MAC for r in fired), (
            "AC-1: a reboot_fired line naming the MAC must be emitted"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac_2_dry_run_logs_would_reboot_and_writes_audit_row(temp_db_path, caplog) -> None:
    rebooter = FakeRebooter()
    db = Database(temp_db_path)
    await db.connect()
    try:
        scheduler = RebootScheduler(
            config=_config(dry_run=True),
            registry=_registry(),
            rebooter=rebooter,
            db=db,
        )

        with caplog.at_level(logging.INFO, logger="wifi_shepard.reboot"):
            await scheduler.run_due(datetime(2026, 5, 25, 3, 30))

        # No network call in dry-run (mirrors would_kick).
        assert rebooter.calls == [], (
            f"AC-2: dry_run must make NO Rebooter call; got {rebooter.calls}"
        )

        # would_reboot line for the eligible MAC.
        would = [r for r in caplog.records if r.getMessage() == "would_reboot"]
        assert any(getattr(r, "mac", None) == MAC for r in would), (
            "AC-2: a would_reboot line naming the eligible MAC must be emitted"
        )

        # Audit symmetry: a reboot_events row is still written, flagged dry_run=1.
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac, mode, dry_run FROM reboot_events ORDER BY id"
            )
            rows = await cur.fetchall()
        assert rows == [(MAC, "proactive", 1)], (
            f"AC-2: a dry_run=1 proactive reboot_events row expected; got {rows}"
        )
    finally:
        await db.close()
