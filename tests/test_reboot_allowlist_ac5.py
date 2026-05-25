"""ADR-0006 AC-5: allowlist + opt-in are absolute for reboot.

A MAC in allowlist: (even if also opted into reboot.eligible) and a MAC not in
reboot.eligible: at all must never be rebooted via the proactive path. This is
the proactive arm of AC-5; the reactive arm rides with the deferred AC-6..AC-9.
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

ALLOWLISTED = "08:f9:e0:ba:c4:84"
NOT_ELIGIBLE = "08:f9:e0:ba:c6:48"


def _button(entity_id: str) -> HAEntity:
    return HAEntity(entity_id=entity_id, domain="button", device_class="restart")


@pytest.mark.asyncio
async def test_ac_5_allowlisted_and_non_eligible_macs_never_reboot(temp_db_path) -> None:
    rebooter = FakeRebooter()
    db = Database(temp_db_path)
    await db.connect()
    try:
        # ALLOWLISTED is opted into eligible AND allowlisted (allowlist wins).
        config = build_config(
            reboot=dict(
                enabled=True,
                dry_run=False,
                eligible=[ALLOWLISTED],
                proactive=dict(enabled=True, schedule="03:30"),
            ),
            allowlist=[ALLOWLISTED],
        )
        # Registry would resolve a target for both — proving the gate, not a miss.
        registry = FakeHARegistry(
            entities_by_mac={
                ALLOWLISTED: [_button("button.a_restart")],
                NOT_ELIGIBLE: [_button("button.b_restart")],
            }
        )
        scheduler = RebootScheduler(config=config, registry=registry, rebooter=rebooter, db=db)

        await scheduler.attempt(ALLOWLISTED)
        await scheduler.attempt(NOT_ELIGIBLE)
        # The full proactive sweep must also produce nothing.
        from datetime import datetime

        await scheduler.run_due(datetime(2026, 5, 25, 3, 30))

        assert rebooter.calls == [], (
            f"AC-5: allowlisted/non-eligible MACs must never reboot; got {rebooter.calls}"
        )
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM reboot_events")
            (count,) = await cur.fetchone()
        assert count == 0, f"AC-5: no reboot_events rows expected; got {count}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac_5_allowlisted_mac_not_previewed_in_dry_run(temp_db_path, caplog) -> None:
    """An allowlisted MAC must produce nothing even on the dry-run preview path —
    no would_reboot line, no dry_run audit row. 'No reboot under any path' is
    absolute (allowlist wins), so an ineligible MAC is dropped before preview."""
    rebooter = FakeRebooter()
    db = Database(temp_db_path)
    await db.connect()
    try:
        config = build_config(
            reboot=dict(
                enabled=True,
                dry_run=True,  # preview path
                eligible=[ALLOWLISTED],
                proactive=dict(enabled=True, schedule="03:30"),
            ),
            allowlist=[ALLOWLISTED],
        )
        registry = FakeHARegistry(entities_by_mac={ALLOWLISTED: [_button("button.a_restart")]})
        scheduler = RebootScheduler(config=config, registry=registry, rebooter=rebooter, db=db)

        with caplog.at_level(logging.INFO, logger="wifi_shepard.reboot"):
            await scheduler.attempt(ALLOWLISTED)

        assert not [r for r in caplog.records if r.getMessage() == "would_reboot"], (
            "AC-5: an allowlisted MAC must not be previewed with would_reboot"
        )
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM reboot_events")
            (count,) = await cur.fetchone()
        assert count == 0, f"AC-5: dry-run must write no row for an allowlisted MAC; got {count}"
    finally:
        await db.close()
