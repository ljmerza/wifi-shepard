"""ADR-0005 AC-1: an eligible MAC present in HA's device registry resolves to a
reboot entity (restart button preferred, else power switch) with NO per-device
backend/address declared in config.
"""

from __future__ import annotations

from tests.conftest import FakeHARegistry
from wifi_shepard.config import build_config
from wifi_shepard.reboot.ha_resolver import HAEntity, resolve_reboot_target


async def test_ac_1_restart_button_preferred() -> None:
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True, eligible=[mac]))  # no overrides
    registry = FakeHARegistry(
        entities_by_mac={
            mac: [
                HAEntity(entity_id="switch.fridge_plug", domain="switch"),
                HAEntity(
                    entity_id="button.fridge_restart",
                    domain="button",
                    device_class="restart",
                ),
            ]
        }
    )

    target = await resolve_reboot_target(mac, cfg, registry)

    assert target is not None, "an eligible MAC matched in HA must resolve to a target"
    assert target.entity_id == "button.fridge_restart", "restart button is preferred"
    assert target.source == "ha_button"


async def test_ac_1_falls_back_to_power_switch_when_no_restart_button() -> None:
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True, eligible=[mac]))
    registry = FakeHARegistry(
        entities_by_mac={
            mac: [
                HAEntity(entity_id="switch.fridge_nightlight", domain="switch"),  # feature toggle
                HAEntity(entity_id="switch.fridge_plug", domain="switch", device_class="outlet"),
            ]
        }
    )

    target = await resolve_reboot_target(mac, cfg, registry)

    assert target is not None
    assert target.entity_id == "switch.fridge_plug", "absent a button, use the outlet switch"
    assert target.source == "ha_switch"
