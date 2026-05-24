"""ADR-0005 AC-2: an eligible MAC not resolvable in HA and with no override
yields no target — the daemon logs reboot_target_unresolved and never guesses.
"""

from __future__ import annotations

import logging

from tests.conftest import FakeHARegistry
from wifi_shepard.config import build_config
from wifi_shepard.reboot.ha_resolver import HAEntity, resolve_reboot_target


async def test_ac_2_unresolved_logs_and_returns_none(caplog) -> None:
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True, eligible=[mac]))
    registry = FakeHARegistry(entities_by_mac={})  # no HA device matches this MAC

    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        target = await resolve_reboot_target(mac, cfg, registry)

    assert target is None, "an unresolved MAC must not produce a guessed target"
    unresolved = [
        r
        for r in caplog.records
        if r.getMessage() == "reboot_target_unresolved" and getattr(r, "mac", None) == mac
    ]
    assert unresolved, "must log reboot_target_unresolved with the mac"


async def test_ac_2_no_suitable_entity_is_unresolved(caplog) -> None:
    # The device matches the MAC but exposes neither a restart button nor a switch.
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True, eligible=[mac]))
    registry = FakeHARegistry(
        entities_by_mac={mac: [HAEntity(entity_id="sensor.fridge_rssi", domain="sensor")]}
    )

    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        target = await resolve_reboot_target(mac, cfg, registry)

    assert target is None, "a device with no rebootable entity is unresolved, not guessed"
    assert any(
        r.getMessage() == "reboot_target_unresolved" and getattr(r, "mac", None) == mac
        for r in caplog.records
    ), "the no-suitable-entity path must also log reboot_target_unresolved"
