"""ADR-0005 AC-5: OUI alone confers neither eligibility nor a reboot backend.

The MAC OUI identifies only the chip vendor, never the firmware. A device that
looks like IoT (an ESP32 from the incident) but was NOT opted in is not
reboot-eligible, and the system makes no backend assumption from the OUI.
"""

from __future__ import annotations

from wifi_shepard.config import build_config
from wifi_shepard.reboot.eligibility import is_reboot_eligible


def test_ac_5_iot_looking_mac_not_opted_in_is_not_eligible() -> None:
    # An ESP32 WLED MAC from the incident — looks like IoT, but a *different* MAC
    # was the one opted in. OUI similarity must grant nothing.
    iot_mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True, eligible=["aa:bb:cc:dd:ee:ff"]))
    assert is_reboot_eligible(iot_mac, cfg) is False


def test_ac_5_oui_grants_no_eligibility_when_nothing_opted_in() -> None:
    iot_mac = "08:f9:e0:ba:c4:84"
    cfg = build_config(reboot=dict(enabled=True))  # eligible list empty
    assert is_reboot_eligible(iot_mac, cfg) is False
