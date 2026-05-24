"""ADR-0005 AC-3: an explicit per-MAC ha_entity override wins over HA
auto-resolution, and the registry is not consulted for the overridden MAC.
Other eligible MACs still auto-resolve (override > auto, mirroring ADR-0001 AC-6).
"""

from __future__ import annotations

from tests.conftest import FakeHARegistry
from wifi_shepard.config import build_config
from wifi_shepard.reboot.ha_resolver import HAEntity, resolve_reboot_target


async def test_ac_3_override_wins_and_registry_not_consulted() -> None:
    x = "08:f9:e0:ba:c6:48"
    cfg = build_config(
        reboot=dict(
            enabled=True,
            eligible=[x],
            overrides=[{"mac": x, "ha_entity": "switch.kitchen_stove_plug"}],
        )
    )
    registry = FakeHARegistry(
        entities_by_mac={x: [HAEntity("button.x_restart", "button", "restart")]}
    )

    target = await resolve_reboot_target(x, cfg, registry)

    assert target is not None
    assert target.entity_id == "switch.kitchen_stove_plug", "override target must win"
    assert target.source == "override"
    assert registry.calls == [], "override must short-circuit before consulting HA"


async def test_ac_3_other_mac_still_auto_resolves() -> None:
    x = "08:f9:e0:ba:c6:48"
    y = "08:f9:e0:ba:c4:84"
    cfg = build_config(
        reboot=dict(
            enabled=True,
            eligible=[x, y],
            overrides=[{"mac": x, "ha_entity": "switch.kitchen_stove_plug"}],
        )
    )
    registry = FakeHARegistry(
        entities_by_mac={y: [HAEntity("button.y_restart", "button", "restart")]}
    )

    target = await resolve_reboot_target(y, cfg, registry)

    assert target is not None
    assert target.entity_id == "button.y_restart", "a non-overridden MAC uses HA auto-resolution"
    assert registry.calls == [y]
