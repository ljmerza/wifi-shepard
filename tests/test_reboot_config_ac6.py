"""ADR-0005 AC-6: reboot disabled (or no reboot: block) → no MAC is reboot-eligible.

With reboot.enabled false the daemon attempts no reboot resolution, so behavior is
identical to the pre-reboot baseline. Tested at the eligibility seam, since
resolution is gated by eligibility.
"""

from __future__ import annotations

from wifi_shepard.config import RebootConfig, build_config
from wifi_shepard.reboot.eligibility import is_reboot_eligible


def test_ac_6_default_config_has_reboot_disabled() -> None:
    cfg = build_config()
    assert isinstance(cfg.reboot, RebootConfig)
    assert cfg.reboot.enabled is False


def test_ac_6_disabled_reboot_makes_no_opted_in_mac_eligible() -> None:
    mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=False, eligible=[mac]))
    assert is_reboot_eligible(mac, cfg) is False


def test_ac_6_no_reboot_block_makes_no_mac_eligible() -> None:
    cfg = build_config()  # no reboot kwarg at all
    assert is_reboot_eligible("08:f9:e0:ba:c4:84", cfg) is False
