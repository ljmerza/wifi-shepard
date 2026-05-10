"""ADR-0003 AC-5: per-MAC kick_mechanism override beats global default.

Mirrors the shape of tests/test_threshold_resolution_ac6.py — the same
override > global resolution rule from ADR-0001 AC-6 must apply to the new
kick_mechanism field.
"""

from __future__ import annotations


def test_ac_5_per_mac_kick_mechanism_override_beats_global():
    from wifi_shepard.config import build_config
    from wifi_shepard.scorer import resolve_kick_mechanism

    overridden_mac = "dc:cc:e6:66:86:2b"
    other_mac = "11:22:33:44:55:66"

    # Global default is "btm"; one MAC overrides to "deauth".
    config = build_config(
        kick_mechanism="btm",
        overrides=[{"mac": overridden_mac, "kick_mechanism": "deauth"}],
    )

    assert resolve_kick_mechanism(overridden_mac, config) == "deauth", (
        "AC-5: per-MAC override must win over global default for kick_mechanism"
    )
    assert resolve_kick_mechanism(other_mac, config) == "btm", (
        "AC-5: non-overridden MAC must use the global kick_mechanism default"
    )


def test_ac_5_global_default_is_deauth_when_unset():
    """Default kick_mechanism is 'deauth' (preserves ADR-0001 MVP behavior)."""
    from wifi_shepard.config import build_config
    from wifi_shepard.scorer import resolve_kick_mechanism

    config = build_config()
    assert resolve_kick_mechanism("aa:bb:cc:dd:ee:ff", config) == "deauth", (
        "AC-5: when no global kick_mechanism is set, default must be 'deauth' to preserve MVP"
    )


def test_ac_5_yaml_round_trip_threads_kick_mechanism_through_loader(tmp_path):
    """Round-trip through load_config_from_path: a YAML scanner.kick_mechanism: auto
    must reach config.scanner.kick_mechanism and resolve_kick_mechanism. Without this
    test, build_config(kick_mechanism=...) would pass tests while the production
    YAML→Config path silently dropped the field."""
    from pathlib import Path

    from wifi_shepard.config import load_config_from_path
    from wifi_shepard.scorer import resolve_kick_mechanism

    yaml_text = """
scanner:
  kick_mechanism: auto
  dry_run: false

overrides:
  - mac: dc:cc:e6:66:86:2b
    kick_mechanism: deauth
"""
    config_path: Path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)
    config = load_config_from_path(config_path)

    assert config.scanner.kick_mechanism == "auto", (
        "load_config_from_path must thread scanner.kick_mechanism through to ScannerConfig; "
        f"got {config.scanner.kick_mechanism!r}"
    )
    assert resolve_kick_mechanism("dc:cc:e6:66:86:2b", config) == "deauth"
    assert resolve_kick_mechanism("11:22:33:44:55:66", config) == "auto"
