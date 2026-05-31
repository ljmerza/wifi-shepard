"""ADR-0008 AC-1..AC-6: AP-saturation gate (detection.ap_cu_total_min).

Only kick a misbehaving client when its AP's total channel utilization meets the
threshold (PLAN.md §3, "only act on saturated APs"). The gate is one more
per-sample condition in is_bad_state; ap_cu_total_min is a resolved-thresholds
field (per-MAC override > global). The quiet-hours override_threshold key stays
deferred (ADR-0008 Constraints) and is covered by ADR-0007's AC-7 test.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_client
from wifi_shepard.config import build_config
from wifi_shepard.resolution import resolve_thresholds
from wifi_shepard.scorer import is_bad_state

RADIOS = ("ng",)


def _thresholds(ap_cu_total_min: int) -> dict:
    # Trips every *other* bad-state criterion for a default make_client (signal
    # -75 < -70, tx_rate 6000 < 12000, retry 50% > 30), so the AP-saturation gate
    # is the deciding factor.
    return {
        "tx_rate_kbps_max": 12000,
        "retry_pct_max": 30,
        "signal_dbm_max": -70,
        "ap_cu_total_min": ap_cu_total_min,
    }


def test_ac1_gate_passes_when_saturated_blocks_when_idle():
    saturated = [make_client(ap_cu_total=70) for _ in range(3)]
    assert is_bad_state(saturated, _thresholds(60), RADIOS) is True
    # Boundary: CU exactly at the floor passes (>= semantics) -> kills a `<=` mutant.
    at_floor = [make_client(ap_cu_total=60) for _ in range(3)]
    assert is_bad_state(at_floor, _thresholds(60), RADIOS) is True
    # Every other criterion trips, but the AP is below the floor -> no kick.
    idle = [make_client(ap_cu_total=50) for _ in range(3)]
    assert is_bad_state(idle, _thresholds(60), RADIOS) is False


def test_ac2_one_unsaturated_sample_spares_the_window():
    # 5-sample window, all bad on the client criteria, but one sample's AP dipped
    # below the floor -> the per-sample gate spares it (as a single good signal
    # sample already does).
    window = [make_client(ap_cu_total=70) for _ in range(5)]
    window[2] = make_client(ap_cu_total=30)
    assert is_bad_state(window, _thresholds(60), RADIOS) is False


def test_ac3_per_mac_override_beats_global():
    x = "aa:bb:cc:dd:ee:01"
    other = "aa:bb:cc:dd:ee:02"
    config = build_config(
        ap_cu_total_min=60,
        overrides=[{"mac": x, "ap_cu_total_min": 80}],
    )
    assert resolve_thresholds(x, config)["ap_cu_total_min"] == 80
    assert resolve_thresholds(other, config)["ap_cu_total_min"] == 60
    # The resolved floor actually drives the gate: a window at CU 70 is below X's
    # 80 floor (spared) but above the global 60 (flagged for other MACs).
    at_70 = [make_client(ap_cu_total=70) for _ in range(3)]
    assert is_bad_state(at_70, resolve_thresholds(x, config), RADIOS) is False
    assert is_bad_state(at_70, resolve_thresholds(other, config), RADIOS) is True


def test_ac4_omitted_defaults_off_and_is_a_noop():
    config = build_config()  # ap_cu_total_min omitted
    assert config.detection.ap_cu_total_min == 0
    # Gate off (min 0) -> even a near-idle AP doesn't spare a client that trips
    # every other criterion; existing behavior preserved.
    thresholds = resolve_thresholds("aa:bb:cc:dd:ee:ff", config)
    assert is_bad_state([make_client(ap_cu_total=5)], thresholds, RADIOS) is True


def test_ac5_zero_cu_fails_closed_under_a_floor():
    # ap_cu_total == 0 is the UniFi "no CU reported" default; under a real floor
    # it must fail closed (no kick), not be treated as actionable.
    assert is_bad_state([make_client(ap_cu_total=0)], _thresholds(60), RADIOS) is False


def test_ac6_config_parses_valid_and_rejects_invalid():
    assert build_config(ap_cu_total_min=75).detection.ap_cu_total_min == 75
    with pytest.raises(ValueError, match="ap_cu_total_min"):
        build_config(ap_cu_total_min=-5)
    with pytest.raises(ValueError, match="ap_cu_total_min"):
        build_config(ap_cu_total_min="high")  # non-int
