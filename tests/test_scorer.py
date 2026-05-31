"""Unit tests for the scorer's bad-state predicate and Scorer.ingest.

The core detection heuristic previously had no isolated test (it was only
exercised through integration paths). ADR-0007 Implementation Plan calls for it.
"""

from __future__ import annotations

from wifi_shepard.config import build_config
from wifi_shepard.scorer import Scorer, is_bad_state

THRESHOLDS = {"tx_rate_kbps_max": 12000, "retry_pct_max": 30, "signal_dbm_max": -70}
RADIOS = ("ng",)


def _sample(**kw):
    from tests.conftest import make_client

    return make_client(**kw)


def test_is_bad_state_all_conditions_met():
    s = _sample(radio="ng", signal=-80, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100)
    assert is_bad_state([s], THRESHOLDS, RADIOS) is True


def test_is_bad_state_empty_window_is_not_bad():
    assert is_bad_state([], THRESHOLDS, RADIOS) is False


def test_is_bad_state_good_signal_is_not_bad():
    s = _sample(signal=-60, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100)
    assert is_bad_state([s], THRESHOLDS, RADIOS) is False


def test_is_bad_state_high_tx_rate_is_not_bad():
    s = _sample(signal=-80, tx_rate_kbps=20000, tx_retries=60, wifi_tx_attempts=100)
    assert is_bad_state([s], THRESHOLDS, RADIOS) is False


def test_is_bad_state_low_retry_pct_is_not_bad():
    s = _sample(signal=-80, tx_rate_kbps=4000, tx_retries=10, wifi_tx_attempts=100)
    assert is_bad_state([s], THRESHOLDS, RADIOS) is False


def test_is_bad_state_wrong_radio_is_not_bad():
    s = _sample(radio="na", signal=-80, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100)
    assert is_bad_state([s], THRESHOLDS, RADIOS) is False


def test_is_bad_state_zero_attempts_is_not_bad():
    # No attempts means the retry ratio is undefined; treat as not-bad rather than
    # dividing by zero.
    s = _sample(signal=-80, tx_rate_kbps=4000, tx_retries=0, wifi_tx_attempts=0)
    assert is_bad_state([s], THRESHOLDS, RADIOS) is False


def test_ingest_allowlisted_mac_short_circuits():
    mac = "aa:bb:cc:dd:ee:ff"
    config = build_config(window_samples=1, allowlist=[mac])
    scorer = Scorer(config)
    assert scorer.ingest(_sample(mac=mac, signal=-80, tx_rate_kbps=4000, tx_retries=60)) is None


def test_ingest_returns_none_until_window_fills():
    config = build_config(window_samples=3)
    scorer = Scorer(config)
    bad = dict(signal=-80, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100)
    assert scorer.ingest(_sample(**bad)) is None  # 1 of 3
    assert scorer.ingest(_sample(**bad)) is None  # 2 of 3
    assert scorer.ingest(_sample(**bad)) is not None  # 3 of 3 -> evaluated and bad


def test_ingest_flags_a_bad_device_with_resolved_thresholds():
    config = build_config(window_samples=1)
    scorer = Scorer(config)
    decision = scorer.ingest(_sample(signal=-80, tx_rate_kbps=4000, tx_retries=60))
    assert decision == {"tx_rate_kbps_max": 12000, "retry_pct_max": 30, "signal_dbm_max": -70}
