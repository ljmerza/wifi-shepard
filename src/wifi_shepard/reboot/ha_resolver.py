"""HA-delegated reboot-backend resolution (ADR-0005 AC-1/AC-2/AC-3).

Given an (already eligible) MAC, resolve a concrete reboot target:
  1. an explicit per-MAC override wins (AC-3),
  2. else match the MAC against HA's device registry and pick a reboot entity —
     a restart button is preferred, else a power switch (AC-1),
  3. else fail safe: log ``reboot_target_unresolved`` and return None (AC-2).

The HA registry is consumed through the ``HADeviceRegistry`` Protocol so the
resolver is testable against a fake; the concrete WebSocket-backed client is
wired later (ADR-0005 Phase 0/3 — verify-first).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("wifi_shepard.reboot")


@dataclass(frozen=True)
class HAEntity:
    entity_id: str
    domain: str
    device_class: str | None = None


@dataclass(frozen=True)
class RebootTarget:
    mac: str
    entity_id: str
    source: str  # "override" | "ha_button" | "ha_switch"


@runtime_checkable
class HADeviceRegistry(Protocol):
    async def entities_for_mac(self, mac: str) -> list[HAEntity] | None:
        """Entities of the HA device whose registry connections include ``mac``,
        or None when no device matches that MAC."""
        ...


def _pick_ha_entity(entities: list[HAEntity]) -> tuple[str, str] | None:
    """Restart button preferred, else a power switch (power-cycle). None if neither."""
    for entity in entities:
        if entity.domain == "button" and entity.device_class == "restart":
            return entity.entity_id, "ha_button"
    for entity in entities:
        if entity.domain == "switch":
            return entity.entity_id, "ha_switch"
    return None


async def resolve_reboot_target(
    mac: str, config: Any, registry: HADeviceRegistry
) -> RebootTarget | None:
    entities = await registry.entities_for_mac(mac)
    if entities:
        picked = _pick_ha_entity(entities)
        if picked is not None:
            entity_id, source = picked
            return RebootTarget(mac=mac, entity_id=entity_id, source=source)
    # Fail safe: an eligible MAC we can't resolve gets no action — never a guess.
    logger.warning("reboot_target_unresolved", extra={"mac": mac})
    return None
