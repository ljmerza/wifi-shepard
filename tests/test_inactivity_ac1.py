"""ADR-0010 AC-1: detection.inactivity parses with defaults (off); invalid types /
negative ints / window_samples<1-while-enabled / malformed MACs fail closed with a
clear error; an absent block is zero behavior change.
"""

from __future__ import annotations

import pytest

from wifi_shepard.config import build_config, load_config_from_path


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_absent_block_yields_off_defaults(tmp_path):
    # No detection.inactivity: block at all → InactivityConfig defaults, off.
    cfg = load_config_from_path(_write(tmp_path, "detection:\n  signal_dbm_max: -70\n"))
    inact = cfg.detection.inactivity
    assert inact.enabled is False
    assert inact.min_bytes_per_window == 1024
    assert inact.window_samples == 30
    assert inact.macs == ()


def test_full_block_parses_and_canonicalizes_macs(tmp_path):
    cfg = load_config_from_path(
        _write(
            tmp_path,
            """
detection:
  inactivity:
    enabled: true
    min_bytes_per_window: 4096
    window_samples: 15
    macs:
      - AA:BB:CC:DD:EE:FF
      - 34:EA:E7:11:22:33
""",
        )
    )
    inact = cfg.detection.inactivity
    assert inact.enabled is True
    assert inact.min_bytes_per_window == 4096
    assert inact.window_samples == 15
    # _require_mac canonicalizes (strip + lowercase).
    assert inact.macs == ("aa:bb:cc:dd:ee:ff", "34:ea:e7:11:22:33")


def test_enabled_true_empty_macs_is_legal_but_inert(tmp_path):
    cfg = load_config_from_path(
        _write(tmp_path, "detection:\n  inactivity:\n    enabled: true\n    macs: []\n")
    )
    assert cfg.detection.inactivity.enabled is True
    assert cfg.detection.inactivity.macs == ()


def test_non_bool_enabled_rejected(tmp_path):
    cfg = _write(tmp_path, 'detection:\n  inactivity:\n    enabled: "maybe"\n')
    with pytest.raises(ValueError, match="detection.inactivity.enabled must be a boolean"):
        load_config_from_path(cfg)


def test_negative_min_bytes_rejected(tmp_path):
    cfg = _write(tmp_path, "detection:\n  inactivity:\n    min_bytes_per_window: -1\n")
    with pytest.raises(ValueError, match="detection.inactivity.min_bytes_per_window must be >= 0"):
        load_config_from_path(cfg)


def test_non_int_window_samples_rejected(tmp_path):
    cfg = _write(tmp_path, "detection:\n  inactivity:\n    window_samples: 1.5\n")
    with pytest.raises(ValueError, match="detection.inactivity.window_samples"):
        load_config_from_path(cfg)


def test_window_samples_zero_while_enabled_rejected(tmp_path):
    cfg = _write(
        tmp_path,
        "detection:\n  inactivity:\n    enabled: true\n    window_samples: 0\n",
    )
    with pytest.raises(ValueError, match=r"window_samples must be >= 1 when inactivity"):
        load_config_from_path(cfg)


def test_window_samples_zero_while_disabled_is_allowed(tmp_path):
    # Disabled + window_samples 0 is inert, not a misconfig — no raise.
    cfg = load_config_from_path(
        _write(tmp_path, "detection:\n  inactivity:\n    enabled: false\n    window_samples: 0\n")
    )
    assert cfg.detection.inactivity.window_samples == 0


def test_malformed_mac_rejected(tmp_path):
    cfg = _write(tmp_path, "detection:\n  inactivity:\n    macs:\n      - not-a-mac\n")
    with pytest.raises(ValueError, match=r"detection.inactivity.macs\[0\]"):
        load_config_from_path(cfg)


def test_macs_scalar_rejected_not_split_into_chars(tmp_path):
    cfg = _write(tmp_path, "detection:\n  inactivity:\n    macs: aa:bb:cc:dd:ee:ff\n")
    with pytest.raises(ValueError, match="detection.inactivity.macs"):
        load_config_from_path(cfg)


def test_build_config_same_guards():
    # The builder layer enforces the same window_samples>=1-while-enabled guard.
    with pytest.raises(ValueError, match=r"window_samples must be >= 1 when inactivity"):
        build_config(inactivity=dict(enabled=True, window_samples=0))
    # ...and defaults through cleanly when omitted.
    cfg = build_config()
    assert cfg.detection.inactivity.enabled is False
