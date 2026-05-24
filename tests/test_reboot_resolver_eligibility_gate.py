"""Regression (ADR-0005 self-review): resolve_reboot_target self-enforces
eligibility. A disabled or non-opted-in MAC resolves to no target — even when an
explicit override exists — and HA is never consulted. Guards against a caller
reaching past the eligibility gate (the resolver must not be a footgun for the
ADR-0006 reboot executor that will consume it).
"""

from __future__ import annotations

from tests.conftest import FakeHARegistry
from wifi_shepard.config import build_config
from wifi_shepard.reboot.ha_resolver import resolve_reboot_target


async def test_disabled_reboot_resolves_nothing_even_with_override() -> None:
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(
        reboot=dict(
            enabled=False,
            eligible=[mac],
            overrides=[{"mac": mac, "ha_entity": "switch.kitchen_stove_plug"}],
        )
    )
    registry = FakeHARegistry()

    assert await resolve_reboot_target(mac, cfg, registry) is None
    assert registry.calls == []


async def test_non_opted_in_mac_resolves_nothing() -> None:
    cfg = build_config(reboot=dict(enabled=True, eligible=["aa:bb:cc:dd:ee:ff"]))
    registry = FakeHARegistry()

    assert await resolve_reboot_target("08:f9:e0:ba:c4:84", cfg, registry) is None
    assert registry.calls == []
