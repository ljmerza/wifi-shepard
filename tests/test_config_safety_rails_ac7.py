"""ADR-0004 AC-7: fail-closed config validation for safety_rails.*.

Negative integers, non-integer types, or unknown sub-keys in safety_rails must
cause a clear ValueError at config parse time so the daemon doesn't half-run.
Matches the ADR-0001 §Decision fail-closed posture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wifi_shepard.config import build_config, load_config_from_path


def test_build_config_accepts_default_no_safety_rails() -> None:
    """Default config (no safety_rails kwargs) → both limits off (0)."""
    cfg = build_config()
    assert cfg.safety_rails.min_seconds_between_kicks == 0
    assert cfg.safety_rails.max_kicks_per_ap_per_window == 0
    assert cfg.safety_rails.per_ap_window_seconds == 600  # default window value


def test_build_config_accepts_positive_values() -> None:
    cfg = build_config(
        safety_rails=dict(
            min_seconds_between_kicks=5,
            max_kicks_per_ap_per_window=3,
            per_ap_window_seconds=900,
        ),
    )
    assert cfg.safety_rails.min_seconds_between_kicks == 5
    assert cfg.safety_rails.max_kicks_per_ap_per_window == 3
    assert cfg.safety_rails.per_ap_window_seconds == 900


def test_negative_min_seconds_between_kicks_rejected() -> None:
    with pytest.raises(ValueError, match="min_seconds_between_kicks"):
        build_config(safety_rails=dict(min_seconds_between_kicks=-1))


def test_negative_max_kicks_per_ap_rejected() -> None:
    with pytest.raises(ValueError, match="max_kicks_per_ap_per_window"):
        build_config(safety_rails=dict(max_kicks_per_ap_per_window=-2))


def test_negative_per_ap_window_seconds_rejected() -> None:
    with pytest.raises(ValueError, match="per_ap_window_seconds"):
        build_config(safety_rails=dict(per_ap_window_seconds=-30))


def test_zero_per_ap_window_seconds_rejected_when_cap_active() -> None:
    """A non-zero per-AP cap with a zero window is meaningless and bug-prone
    (every kick would be immediately out-of-window). Reject explicitly."""
    with pytest.raises(ValueError, match="per_ap_window_seconds"):
        build_config(
            safety_rails=dict(
                max_kicks_per_ap_per_window=3,
                per_ap_window_seconds=0,
            )
        )


def test_non_integer_min_seconds_rejected() -> None:
    with pytest.raises((ValueError, TypeError)):
        build_config(safety_rails=dict(min_seconds_between_kicks="many"))  # type: ignore[arg-type]


def test_yaml_safety_rails_block_parses(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
safety_rails:
  min_seconds_between_kicks: 5
  max_kicks_per_ap_per_window: 3
  per_ap_window_seconds: 600
""".strip()
    )
    cfg = load_config_from_path(cfg_path)
    assert cfg.safety_rails.min_seconds_between_kicks == 5
    assert cfg.safety_rails.max_kicks_per_ap_per_window == 3
    assert cfg.safety_rails.per_ap_window_seconds == 600


def test_yaml_missing_safety_rails_block_uses_defaults(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("scanner:\n  dry_run: true\n")
    cfg = load_config_from_path(cfg_path)
    assert cfg.safety_rails.min_seconds_between_kicks == 0
    assert cfg.safety_rails.max_kicks_per_ap_per_window == 0
    assert cfg.safety_rails.per_ap_window_seconds == 600


def test_yaml_negative_value_fails_closed(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
safety_rails:
  min_seconds_between_kicks: -5
""".strip()
    )
    with pytest.raises(ValueError, match="min_seconds_between_kicks"):
        load_config_from_path(cfg_path)
