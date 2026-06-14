"""ADR-0009 AC-1..AC-6: disable-able detection criteria.

Each client criterion (signal_dbm_max / tx_rate_kbps_max / retry_pct_max) can be
turned off with YAML `null`, enabling a "signal + saturation only" mode (and any
other subset). At least one must stay enabled. The AP-saturation gate and radio
filter are unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import make_client
from wifi_shepard.config import build_config, load_config_from_path
from wifi_shepard.resolution import apply_quiet_hours
from wifi_shepard.scorer import is_bad_state

RADIOS = ("ng",)


def _th(signal=-70, tx_rate=12000, retry=30, ap_cu=60):
    return {
        "signal_dbm_max": signal,
        "tx_rate_kbps_max": tx_rate,
        "retry_pct_max": retry,
        "ap_cu_total_min": ap_cu,
    }


def test_ac1_signal_plus_saturation_only_ignores_rate_and_retry():
    # Weak signal, but fast + low-retry -> spared while all three criteria are active.
    weak_but_fast = [
        make_client(signal=-75, tx_rate_kbps=60000, tx_retries=1, ap_cu_total=70) for _ in range(3)
    ]
    assert is_bad_state(weak_but_fast, _th(), RADIOS) is False
    # Disable rate + retry -> signal + saturation alone flags it.
    sig_only = _th(tx_rate=None, retry=None)
    assert is_bad_state(weak_but_fast, sig_only, RADIOS) is True
    # A strong-signal client on the same saturated AP is still spared.
    strong = [
        make_client(signal=-50, tx_rate_kbps=60000, tx_retries=1, ap_cu_total=70) for _ in range(3)
    ]
    assert is_bad_state(strong, sig_only, RADIOS) is False


def test_ac2_all_criteria_disabled_never_acts():
    # Fail safe: radio + saturation alone must not flag anything.
    none_active = _th(signal=None, tx_rate=None, retry=None)
    saturated = [make_client(ap_cu_total=99) for _ in range(5)]
    assert is_bad_state(saturated, none_active, RADIOS) is False


def test_ac3_omitted_criterion_keeps_active_default(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("detection:\n  signal_dbm_max: -70\n  retry_pct_max: 30\n")  # tx_rate omitted
    cfg = load_config_from_path(p)
    assert cfg.detection.tx_rate_kbps_max == 12000, "omitted key keeps the active default"
    assert cfg.detection.signal_dbm_max == -70
    assert cfg.detection.retry_pct_max == 30


def test_ac4_explicit_null_disables_and_all_null_is_rejected(tmp_path):
    p = tmp_path / "sig_only.yaml"
    p.write_text(
        "detection:\n  tx_rate_kbps_max: null\n  retry_pct_max: null\n  signal_dbm_max: -70\n"
    )
    cfg = load_config_from_path(p)
    assert cfg.detection.tx_rate_kbps_max is None
    assert cfg.detection.retry_pct_max is None
    assert cfg.detection.signal_dbm_max == -70

    allnull = tmp_path / "allnull.yaml"
    allnull.write_text(
        "detection:\n  tx_rate_kbps_max: null\n  retry_pct_max: null\n  signal_dbm_max: null\n"
    )
    with pytest.raises(ValueError, match="at least one client criterion"):
        load_config_from_path(allnull)

    # Same guard at the builder layer.
    with pytest.raises(ValueError, match="at least one client criterion"):
        build_config(tx_rate_kbps_max=None, retry_pct_max=None, signal_dbm_max=None)


def test_ac5_quiet_hours_leaves_disabled_criterion_disabled():
    qh = SimpleNamespace(tx_rate_kbps_max=2000, retry_pct_max=50, signal_dbm_max=None)
    thresholds = _th(tx_rate=None)  # tx_rate disabled
    out = apply_quiet_hours(thresholds, qh)
    assert out["tx_rate_kbps_max"] is None, "disabled criterion stays disabled (no TypeError)"
    assert out["retry_pct_max"] == 50, "active criterion still tightened to the stricter value"
    assert out["signal_dbm_max"] == -70


def test_ac6_partial_disable_still_requires_remaining_criteria():
    th = _th(retry=None)  # signal + tx_rate active, retry disabled
    # Weak but fast: tx_rate criterion not violated -> spared even though retry is off.
    weak_fast = [make_client(signal=-80, tx_rate_kbps=60000, ap_cu_total=70) for _ in range(3)]
    assert is_bad_state(weak_fast, th, RADIOS) is False
    # Weak AND slow: both active criteria violated -> flagged regardless of retries
    # (tx_retries=0 would never trip the retry criterion, but it's disabled).
    weak_slow = [
        make_client(
            signal=-80, tx_rate_kbps=6000, tx_retries=0, wifi_tx_attempts=100, ap_cu_total=70
        )
        for _ in range(3)
    ]
    assert is_bad_state(weak_slow, th, RADIOS) is True
