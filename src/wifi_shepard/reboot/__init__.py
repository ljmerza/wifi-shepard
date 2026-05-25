"""Reboot remediation surface (ADR-0005 / ADR-0006).

ADR-0005 scope: device identification + reboot-backend selection. Reboot
eligibility and HA-delegated target resolution live here; the reboot *action*
(the ``Rebooter`` and scheduling) arrives with ADR-0006.
"""

from __future__ import annotations


def normalize_mac(mac: str) -> str:
    """Canonical MAC form for case-insensitive comparison across config surfaces."""
    return mac.strip().lower()
