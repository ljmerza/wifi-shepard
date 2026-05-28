"""Rebooter Protocol (ADR-0006).

A reboot is not a Controller action — a UniFi controller cannot reboot a client.
The Rebooter is the surface the proactive scheduler (and, later, the reactive
escalation path) invokes to power-cycle a device, given an ADR-0005 RebootTarget.

The concrete Home-Assistant-backed implementation (button.press / outlet
power-cycle via call_service) is deferred to its own PR, mirroring how ADR-0005
deferred the concrete HA device-registry client. Tests use a fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from wifi_shepard.reboot.ha_resolver import RebootTarget


@runtime_checkable
class Rebooter(Protocol):
    async def reboot(self, target: RebootTarget) -> None:
        """Power-cycle the device behind ``target`` (its resolved HA entity)."""
        ...
