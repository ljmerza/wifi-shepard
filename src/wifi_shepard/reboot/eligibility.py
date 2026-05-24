"""Reboot eligibility (ADR-0005 AC-4/AC-5/AC-6).

A MAC is reboot-eligible only when reboot is enabled, the MAC was explicitly
opted in (``reboot.eligible``), and it is not allowlisted (the allowlist always
wins). The OUI confers nothing — eligibility requires an explicit opt-in, so a
laptop or phone that shares a vendor block with an IoT device is never eligible.
"""

from __future__ import annotations

from typing import Any

from wifi_shepard.reboot import normalize_mac


def is_reboot_eligible(mac: str, config: Any) -> bool:
    if not config.reboot.enabled:
        return False
    target = normalize_mac(mac)
    if target in {normalize_mac(m) for m in config.allowlist}:
        return False  # allowlist always wins (ADR-0005 AC-4)
    eligible = {normalize_mac(m) for m in config.reboot.eligible}
    return target in eligible
