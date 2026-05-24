"""ADR-0005 AC-4: an allowlisted MAC is never reboot-eligible, even if opted in.

Allowlist always wins over reboot.eligible. A MAC present in both surfaces is a
contradiction the operator should see, so config load emits a warning; eligibility
still resolves to False.
"""

from __future__ import annotations

import logging

from wifi_shepard.config import build_config
from wifi_shepard.reboot.eligibility import is_reboot_eligible


def test_ac_4_allowlisted_mac_never_eligible_even_if_opted_in() -> None:
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True, eligible=[mac]), allowlist=[mac])
    assert is_reboot_eligible(mac, cfg) is False, "allowlist must win over reboot.eligible"


def test_ac_4_eligible_only_mac_is_eligible() -> None:
    # Control: the same config shape, but the MAC is opted in and NOT allowlisted.
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True, eligible=[mac]))
    assert is_reboot_eligible(mac, cfg) is True


def test_ac_4_overlap_emits_config_load_warning(caplog) -> None:
    mac = "08:f9:e0:ba:c4:84"
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(reboot=dict(enabled=True, eligible=[mac]), allowlist=[mac])

    warnings = [
        r
        for r in caplog.records
        if r.getMessage() == "reboot_eligible_in_allowlist" and getattr(r, "mac", None) == mac
    ]
    assert warnings, "allowlist ∩ reboot.eligible overlap must emit a config-load warning"
