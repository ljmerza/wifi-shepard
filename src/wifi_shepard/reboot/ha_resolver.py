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
from typing import TYPE_CHECKING, Protocol

from wifi_shepard.reboot import normalize_mac
from wifi_shepard.reboot.eligibility import is_reboot_eligible

if TYPE_CHECKING:
    from wifi_shepard.config import Config

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


class HADeviceRegistry(Protocol):
    async def entities_for_mac(self, mac: str) -> list[HAEntity] | None:
        """Entities of the HA device whose registry connections include ``mac``,
        or None when no device matches that MAC."""
        ...


def _pick_ha_entity(entities: list[HAEntity]) -> tuple[str, str] | None:
    """Restart button preferred, else a smart-plug outlet switch (power-cycle).

    The switch branch requires ``device_class == "outlet"`` (HA's smart-plug
    class) rather than any switch: a device's other switches are feature toggles
    (e.g. a WLED sync toggle), and power-cycling one of those is wrong. Erring
    strict is the safe direction — an unmatched device is unresolved (no action),
    and the operator can still point an explicit override at it. The exact switch
    shape is Phase-0 verify-first per ADR-0005 Constraints note 2.
    """
    for entity in entities:
        if entity.domain == "button" and entity.device_class == "restart":
            return entity.entity_id, "ha_button"
    for entity in entities:
        if entity.domain == "switch" and entity.device_class == "outlet":
            return entity.entity_id, "ha_switch"
    return None


async def resolve_reboot_target(
    mac: str, config: Config, registry: HADeviceRegistry
) -> RebootTarget | None:
    # Self-defending gate: a disabled or non-opted-in MAC resolves to nothing,
    # even via an override. Resolution is never attempted for an ineligible MAC
    # (ADR-0005 AC-6) — callers can't bypass this by reaching past eligibility.
    if not is_reboot_eligible(mac, config):
        return None

    # Explicit override wins and short-circuits before consulting HA (AC-3).
    target = normalize_mac(mac)
    for override in config.reboot.overrides:
        if normalize_mac(override.mac) == target and override.ha_entity:
            return RebootTarget(mac=mac, entity_id=override.ha_entity, source="override")

    # Transport failures (HA unreachable, WS drop) are the concrete registry
    # client's job to translate into "no match" so resolution degrades to the
    # unresolved path (ADR-0005 Risks: HA unreachable → no action). This abstract
    # resolver deliberately does NOT blanket-catch here — swallowing every
    # exception would hide real bugs; the deferred WS client (ADR-0006) owns it.
    entities = await registry.entities_for_mac(mac)
    if entities:
        picked = _pick_ha_entity(entities)
        if picked is not None:
            entity_id, source = picked
            return RebootTarget(mac=mac, entity_id=entity_id, source=source)
    # Fail safe: an eligible MAC we can't resolve gets no action — never a guess.
    logger.warning("reboot_target_unresolved", extra={"mac": mac})
    return None
