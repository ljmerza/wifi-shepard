"""ADR-0005 AC-7: fail-closed validation of the reboot: block.

A non-MAC string in eligible, an unknown resolver, or an override missing its
reboot target (or its MAC) must raise a clear ValueError at config parse time.
Mirrors the ADR-0001 fail-closed posture and ADR-0004 AC-7.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wifi_shepard.config import build_config, load_config_from_path


def test_ac_7_non_mac_in_eligible_rejected() -> None:
    with pytest.raises(ValueError, match="eligible"):
        build_config(reboot=dict(enabled=True, eligible=["not-a-mac"]))


def test_ac_7_unknown_resolver_rejected() -> None:
    with pytest.raises(ValueError, match="resolver"):
        build_config(reboot=dict(enabled=True, resolver="telepathy"))


def test_ac_7_override_missing_target_rejected() -> None:
    with pytest.raises(ValueError, match="overrides"):
        build_config(
            reboot=dict(enabled=True, overrides=[{"mac": "08:f9:e0:ba:c4:84"}]),
        )


def test_ac_7_override_missing_mac_rejected() -> None:
    with pytest.raises(ValueError, match="overrides"):
        build_config(reboot=dict(enabled=True, overrides=[{"ha_entity": "switch.foo"}]))


def test_ac_7_yaml_unknown_resolver_fails_closed(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("reboot:\n  enabled: true\n  resolver: telepathy\n")
    with pytest.raises(ValueError, match="resolver"):
        load_config_from_path(cfg_path)
