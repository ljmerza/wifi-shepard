"""ADR-0007 AC-6/AC-7: quiet-hours stricter-threshold gating + fail-closed config.

AC-6 exercises the scorer with an injected wall clock (UTC window 23:00–07:00).
AC-7 asserts the loader rejects the unsupported ap_cu_total_min, a bad zone, an
unknown threshold key, and a malformed time.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conftest import make_client

# 02:00 UTC is inside the 23:00–07:00 window; 12:00 UTC is outside.
INSIDE = datetime(2026, 1, 1, 2, 0, tzinfo=UTC).timestamp()
OUTSIDE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).timestamp()


def _config_with_quiet_hours():
    from wifi_shepard.config import build_config

    return build_config(
        window_samples=1,
        quiet_hours={
            "start": "23:00",
            "end": "07:00",
            "timezone": "UTC",
            "override_threshold": {"tx_rate_kbps_max": 2000, "retry_pct_max": 50},
        },
    )


def test_ac6_quiet_hours_tightens_thresholds():
    from wifi_shepard.scorer import Scorer

    config = _config_with_quiet_hours()
    # Default make_client: tx_rate 6000, retry 50%, signal -75 — bad under the
    # normal thresholds, but tx_rate 6000 is NOT < 2000, so NOT bad under the
    # stricter quiet-hours thresholds.
    inside = Scorer(config, wall_now_fn=lambda: INSIDE)
    assert inside.ingest(make_client()) is None, "inside quiet hours: stricter thresholds spare it"

    outside = Scorer(config, wall_now_fn=lambda: OUTSIDE)
    assert outside.ingest(make_client()) is not None, "outside: normal thresholds apply"


def test_ac6_quiet_hours_still_kicks_a_truly_bad_device():
    from wifi_shepard.scorer import Scorer

    config = _config_with_quiet_hours()
    # tx_rate 1000 < 2000, retry 60% > 50, signal -85 < -70 -> bad even under the
    # stricter quiet-hours thresholds.
    truly_bad = make_client(tx_rate_kbps=1000, tx_retries=60, wifi_tx_attempts=100, signal=-85)
    inside = Scorer(config, wall_now_fn=lambda: INSIDE)
    assert inside.ingest(truly_bad) is not None, "inside: a truly-bad device is still flagged"


def test_ac7_ap_cu_total_min_is_rejected():
    from wifi_shepard.config import build_config

    with pytest.raises(ValueError, match="ap_cu_total_min is not yet supported"):
        build_config(
            quiet_hours={
                "start": "23:00",
                "end": "07:00",
                "timezone": "UTC",
                "override_threshold": {"ap_cu_total_min": 80},
            }
        )


def test_ac7_bad_timezone_is_rejected():
    from wifi_shepard.config import build_config

    with pytest.raises(ValueError, match="valid IANA zone"):
        build_config(quiet_hours={"start": "23:00", "end": "07:00", "timezone": "Not/AZone"})


def test_ac7_unknown_threshold_key_is_rejected():
    from wifi_shepard.config import build_config

    with pytest.raises(ValueError, match="not a recognized threshold"):
        build_config(
            quiet_hours={
                "start": "23:00",
                "end": "07:00",
                "timezone": "UTC",
                "override_threshold": {"bogus": 1},
            }
        )


def test_ac7_bad_time_is_rejected():
    from wifi_shepard.config import build_config

    with pytest.raises(ValueError, match="HH:MM"):
        build_config(quiet_hours={"start": "25:00", "end": "07:00", "timezone": "UTC"})


def test_ac7_valid_quiet_hours_parses():
    from wifi_shepard.config import build_config

    cfg = build_config(
        quiet_hours={
            "start": "23:00",
            "end": "07:00",
            "timezone": "UTC",
            "override_threshold": {"tx_rate_kbps_max": 2000, "retry_pct_max": 50},
        }
    )
    assert cfg.quiet_hours is not None
    assert cfg.quiet_hours.tx_rate_kbps_max == 2000
    assert cfg.quiet_hours.retry_pct_max == 50
    assert cfg.quiet_hours.signal_dbm_max is None  # unset field stays None
