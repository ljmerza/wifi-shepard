"""ADR-0005 config-load advisories (self-review follow-ups).

Two advisory warnings emitted at config load — neither blocks, both nudge the
operator toward a likely misconfiguration:

  * `reboot_eligible_non_espressif_oui` (Fork B / OUI pre-filter): an eligible
    MAC whose OUI is not a known Espressif block is likely a typo onto a
    laptop/phone. Suppressed for an allowlisted MAC, where the allowlist∩eligible
    warning is the salient signal.
  * `reboot_override_mac_not_eligible`: an override target for a MAC never opted
    into `eligible` is dead config (resolution gates on eligibility first).
"""

from __future__ import annotations

import logging

from wifi_shepard.config import build_config

_ESPRESSIF = "08:f9:e0:ba:c4:84"  # real Espressif OUI (08:f9:e0)
_ESPRESSIF_2 = "08:f9:e0:ba:c6:48"
_NON_ESPRESSIF = "aa:bb:cc:dd:ee:ff"  # locally-administered; never an IEEE OUI


def _messages_for(caplog, message: str) -> list[str]:
    return [getattr(r, "mac", None) for r in caplog.records if r.getMessage() == message]


def test_non_espressif_eligible_mac_warns(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(reboot=dict(enabled=True, eligible=[_NON_ESPRESSIF]))
    assert _NON_ESPRESSIF in _messages_for(caplog, "reboot_eligible_non_espressif_oui")


def test_espressif_eligible_mac_does_not_warn(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(reboot=dict(enabled=True, eligible=[_ESPRESSIF]))
    assert _messages_for(caplog, "reboot_eligible_non_espressif_oui") == []


def test_oui_warning_suppressed_for_allowlisted_mac(caplog) -> None:
    # A non-Espressif MAC in both eligible and allowlist: the allowlist warning is
    # the salient signal, so the OUI warning must NOT also fire for it.
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(
            reboot=dict(enabled=True, eligible=[_NON_ESPRESSIF]),
            allowlist=[_NON_ESPRESSIF],
        )
    assert _NON_ESPRESSIF in _messages_for(caplog, "reboot_eligible_in_allowlist")
    assert _messages_for(caplog, "reboot_eligible_non_espressif_oui") == []


def test_override_mac_not_in_eligible_warns(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(
            reboot=dict(
                enabled=True,
                eligible=[_ESPRESSIF],
                overrides=[{"mac": _ESPRESSIF_2, "ha_entity": "switch.foo"}],
            )
        )
    assert _ESPRESSIF_2 in _messages_for(caplog, "reboot_override_mac_not_eligible")


def test_override_mac_in_eligible_does_not_warn(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(
            reboot=dict(
                enabled=True,
                eligible=[_ESPRESSIF],
                overrides=[{"mac": _ESPRESSIF, "ha_entity": "switch.foo"}],
            )
        )
    assert _messages_for(caplog, "reboot_override_mac_not_eligible") == []
